"""Minimal Win32 user-mode emulator for running SQ8L.dll on any platform.

Loads a 32-bit PE at its preferred base into Unicorn, maps every import to a
thunk address handled in Python, and provides helpers to call guest functions.
Only the Win32 APIs actually used by SQ8L are implemented (see winapi.py).
"""
import struct
import sys

import pefile
from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_PROT
from unicorn.x86_const import (
    UC_X86_REG_EAX, UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EDX,
    UC_X86_REG_ESI, UC_X86_REG_EDI, UC_X86_REG_EBP, UC_X86_REG_ESP,
    UC_X86_REG_EIP, UC_X86_REG_GDTR, UC_X86_REG_CS, UC_X86_REG_DS,
    UC_X86_REG_ES, UC_X86_REG_FS, UC_X86_REG_GS, UC_X86_REG_SS, UC_X86_REG_FPCW,
)
from fractions import Fraction

# FPU control word of a typical MSVC-built host thread (53-bit precision, round to nearest).
HOST_FPCW = 0x027F
# What SQ8L sets inside processReplacing: host CW | 0xC00 (round toward zero).
PROCESS_FPCW = 0x0E7F


def to_ext80(x):
    """Encode a Python float (or Fraction) exactly as x87 80-bit extended."""
    x = Fraction(x)
    if x == 0:
        return bytes(10)
    sign = 0x8000 if x < 0 else 0
    x = abs(x)
    e = x.numerator.bit_length() - x.denominator.bit_length()
    if Fraction(2) ** e > x:
        e -= 1
    m = x / Fraction(2) ** e * (1 << 63)
    assert m.denominator == 1, "value not representable in 80 bits"
    return struct.pack("<QH", int(m), sign | (e + 16383))


def from_ext80(b):
    """Decode 80-bit extended to an exact Fraction."""
    m, se = struct.unpack("<QH", bytes(b[:10]))
    e = se & 0x7FFF
    if e == 0 and m == 0:
        return Fraction(0)
    v = Fraction(m, 1 << 63) * Fraction(2) ** (e - 16383)
    return -v if se & 0x8000 else v

PAGE = 0x1000

STACK_BASE, STACK_SIZE = 0x00100000, 0x00100000
HEAP_BASE, HEAP_SIZE = 0x10000000, 0x10000000
SCRATCH_BASE, SCRATCH_SIZE = 0x7D000000, 0x00100000
THUNK_BASE, THUNK_SIZE = 0x7E000000, 0x00010000
STOP_ADDR = THUNK_BASE + THUNK_SIZE - 0x10
TEB_BASE = 0x7F000000
GDT_BASE = 0x7F100000


def align(x, a=PAGE):
    return (x + a - 1) & ~(a - 1)


def _gdt_entry(base, limit, access, flags):
    v = limit & 0xFFFF
    v |= (base & 0xFFFFFF) << 16
    v |= (access & 0xFF) << 40
    v |= ((limit >> 16) & 0xF) << 48
    v |= (flags & 0xF) << 52
    v |= ((base >> 24) & 0xFF) << 56
    return struct.pack("<Q", v)


class EmuStop(Exception):
    pass


class Win32Emu:
    def __init__(self, dll_path, trace_api=False):
        self.path = dll_path
        self.pe = pefile.PE(dll_path)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.uc = Uc(UC_ARCH_X86, UC_MODE_32)
        self.trace_api = trace_api
        self.thunks = {}          # addr -> (dll, name)
        self.thunk_by_name = {}   # name -> addr
        self.callbacks = {}       # addr -> (nargs, fn, name) for host callbacks
        self.trampolines = {}     # addr -> fn(emu) returning the address to continue at
        self.chains = []          # pending guest call chains started from API handlers
        self.traces = []          # hook handles of active traces
        self.next_thunk = THUNK_BASE
        self.heap_top = HEAP_BASE
        self.heap_mapped = HEAP_BASE
        self.scratch_top = SCRATCH_BASE
        self.api = None
        self._setup_memory()
        self._load_image()
        self._setup_segments()
        self.uc.hook_add(UC_HOOK_CODE, self._on_thunk, begin=THUNK_BASE, end=THUNK_BASE + THUNK_SIZE - 1)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED | UC_HOOK_MEM_PROT, self._on_bad_mem)
        self.fault = None

    # ------------------------------------------------------------------ setup
    def _setup_memory(self):
        self.uc.mem_map(STACK_BASE, STACK_SIZE)
        self.uc.mem_map(SCRATCH_BASE, SCRATCH_SIZE)
        self.uc.mem_map(THUNK_BASE, THUNK_SIZE)
        self.uc.mem_write(THUNK_BASE, b"\xC3" * THUNK_SIZE)
        self.uc.mem_map(TEB_BASE, 0x10000)
        self.uc.mem_map(GDT_BASE, PAGE)
        self.uc.reg_write(UC_X86_REG_ESP, STACK_BASE + STACK_SIZE - 0x100)
        self.uc.reg_write(UC_X86_REG_FPCW, HOST_FPCW)

    def _load_image(self):
        pe = self.pe
        size = align(pe.OPTIONAL_HEADER.SizeOfImage)
        self.uc.mem_map(self.base, size)
        hdr = pe.__data__[: pe.OPTIONAL_HEADER.SizeOfHeaders]
        self.uc.mem_write(self.base, bytes(hdr))
        for s in pe.sections:
            data = s.get_data()
            if data:
                self.uc.mem_write(self.base + s.VirtualAddress, data[: max(s.Misc_VirtualSize, len(data))])
        for imp in pe.DIRECTORY_ENTRY_IMPORT:
            dll = imp.dll.decode().lower()
            for i in imp.imports:
                name = i.name.decode() if i.name else f"#{i.ordinal}"
                addr = self.thunk_for(dll, name)
                self.uc.mem_write(i.address, struct.pack("<I", addr))
        self.exports = {e.name.decode(): self.base + e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols}

    def _setup_segments(self):
        # Flat code/data segments plus an FS segment pointing at the TEB.
        gdt = bytearray(PAGE)
        A_P, A_DATA, A_W, A_CODE, A_R, A_DC = 0x80, 0x10, 0x02, 0x18, 0x02, 0x04
        F_32, F_G = 0x4, 0x8
        entries = {
            1: _gdt_entry(0, 0xFFFFF, A_P | A_CODE | A_R | A_DC, F_32 | F_G),          # cs
            2: _gdt_entry(0, 0xFFFFF, A_P | A_DATA | A_W | A_DC, F_32 | F_G),          # ds/es/ss/gs
            3: _gdt_entry(TEB_BASE, 0xFFFF, A_P | A_DATA | A_W | A_DC, F_32),          # fs
        }
        for i, e in entries.items():
            gdt[i * 8:(i + 1) * 8] = e
        self.uc.mem_write(GDT_BASE, bytes(gdt))
        self.uc.reg_write(UC_X86_REG_GDTR, (0, GDT_BASE, PAGE - 1, 0))
        self.uc.reg_write(UC_X86_REG_CS, 1 << 3)
        for r in (UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_GS, UC_X86_REG_SS):
            self.uc.reg_write(r, 2 << 3)
        self.uc.reg_write(UC_X86_REG_FS, 3 << 3)
        # TEB: SEH chain head = -1, self pointer at +0x18, stack bounds.
        teb = struct.pack("<III", 0xFFFFFFFF, STACK_BASE + STACK_SIZE, STACK_BASE)
        self.uc.mem_write(TEB_BASE, teb)
        self.uc.mem_write(TEB_BASE + 0x18, struct.pack("<I", TEB_BASE))
        self.uc.mem_write(TEB_BASE + 0x24, struct.pack("<I", 0x1000))  # thread id

    # ------------------------------------------------------------------ thunks
    def thunk_for(self, dll, name):
        if name in self.thunk_by_name:
            return self.thunk_by_name[name]
        addr = self.next_thunk
        self.next_thunk += 4
        self.thunks[addr] = (dll, name)
        self.thunk_by_name[name] = addr
        return addr

    def make_callback(self, name, nargs, fn, cdecl=False):
        """Create a guest-callable address that runs fn(emu, *args) in Python."""
        addr = self.next_thunk
        self.next_thunk += 4
        self.callbacks[addr] = (nargs, fn, name, cdecl)
        return addr

    def _on_thunk(self, uc, addr, size, _):
        if addr == STOP_ADDR:
            uc.emu_stop()
            return
        if addr in self.trampolines:
            uc.reg_write(UC_X86_REG_EIP, self.trampolines[addr](self))
            return
        esp = uc.reg_read(UC_X86_REG_ESP)
        ret = self.u32(esp)
        if addr in self.callbacks:
            nargs, fn, name, cdecl = self.callbacks[addr]
            args = struct.unpack(f"<{nargs}I", uc.mem_read(esp + 4, 4 * nargs)) if nargs else ()
            res = fn(self, *args)
            pop = 0 if cdecl else 4 * nargs
        else:
            dll, name = self.thunks.get(addr, ("?", f"?{addr:#x}"))
            impl = self.api.lookup(name) if self.api else None
            if impl is None:
                self.fault = f"unimplemented API {dll}!{name} called from {ret:#x}"
                uc.emu_stop()
                return
            nargs, fn = impl
            args = struct.unpack(f"<{nargs}I", uc.mem_read(esp + 4, 4 * nargs)) if nargs else ()
            try:
                res = fn(self, *args)
            except EmuStop as e:
                self.fault = str(e)
                uc.emu_stop()
                return
            if self.trace_api:
                shown = res if res is None or isinstance(res, str) else hex(res if isinstance(res, int) else res[0] & 0xFFFFFFFF)
                print(f"  [api] {name}({', '.join(hex(a) for a in args)}) -> {shown}  @ {ret:#x}", file=sys.stderr)
            pop = 4 * nargs
            if res == "REDIRECTED":  # handler changed EIP/ESP itself
                return
        if isinstance(res, tuple):  # (eax, edx)
            uc.reg_write(UC_X86_REG_EAX, res[0] & 0xFFFFFFFF)
            uc.reg_write(UC_X86_REG_EDX, res[1] & 0xFFFFFFFF)
        elif res is not None:
            uc.reg_write(UC_X86_REG_EAX, res & 0xFFFFFFFF)
        uc.reg_write(UC_X86_REG_ESP, esp + 4 + pop)
        uc.reg_write(UC_X86_REG_EIP, ret)

    # ------------------------------------------------------------------ guest calls from API handlers
    def chain_calls(self, calls, finish, pop_args):
        """From inside an API handler: run guest stdcall functions one after another
        (calls = list of (addr, [args]) or a callable(results) -> next call or None),
        then return finish(results) as the API result. pop_args = API arg count.
        Returns "REDIRECTED" so the thunk handler leaves EIP/ESP alone."""
        uc = self.uc
        esp = uc.reg_read(UC_X86_REG_ESP)
        state = {"ret": self.u32(esp), "base": esp + 4 + 4 * pop_args, "calls": calls,
                 "results": [], "finish": finish}
        if not hasattr(self, "_chain_tramp"):
            self._chain_tramp = self.next_thunk
            self.next_thunk += 4
            self.trampolines[self._chain_tramp] = self._chain_step
        self.chains.append(state)
        self._chain_next(state)
        return "REDIRECTED"

    def _chain_pending(self, state):
        calls = state["calls"]
        if callable(calls):
            return calls(state["results"])
        i = len(state["results"])
        return calls[i] if i < len(calls) else None

    def _chain_next(self, state):
        nxt = self._chain_pending(state)
        uc = self.uc
        if nxt is None:
            self.chains.pop()
            res = state["finish"](state["results"])
            if res is not None:
                uc.reg_write(UC_X86_REG_EAX, res & 0xFFFFFFFF)
            uc.reg_write(UC_X86_REG_ESP, state["base"])
            uc.reg_write(UC_X86_REG_EIP, state["ret"])
            return state["ret"]
        fn, args = nxt
        esp = state["base"] - 4 * len(args) - 4
        self.w32(esp, self._chain_tramp)
        for i, a in enumerate(args):
            self.w32(esp + 4 + 4 * i, a)
        uc.reg_write(UC_X86_REG_ESP, esp)
        uc.reg_write(UC_X86_REG_EIP, fn)
        return fn

    def _chain_step(self, emu):
        state = self.chains[-1]
        state["results"].append(self.uc.reg_read(UC_X86_REG_EAX))
        # the callee popped its args (stdcall); continue the chain
        return self._chain_next(state)

    def trace(self, entry, on_enter, on_exit):
        """Hook a guest function: on_enter(emu) -> ctx at entry, on_exit(emu, ctx)
        right after it returns (the return address is swapped for a trampoline)."""
        tramp = self.next_thunk
        self.next_thunk += 4
        pending = []

        def enter(uc, addr, size, _):
            esp = uc.reg_read(UC_X86_REG_ESP)
            pending.append((self.u32(esp), on_enter(self)))
            self.w32(esp, tramp)

        def leave(emu):
            ret, ctx = pending.pop()
            on_exit(emu, ctx)
            return ret

        self.trampolines[tramp] = leave
        h = self.uc.hook_add(UC_HOOK_CODE, enter, begin=entry, end=entry)
        # Blocks translated before the hook existed would not call it: flush them.
        self.uc.ctl_flush_tb()
        self.traces.append(h)
        return h

    def untrace(self, h):
        self.uc.hook_del(h)
        self.traces.remove(h)

    def _on_bad_mem(self, uc, access, addr, size, value, _):
        eip = uc.reg_read(UC_X86_REG_EIP)
        self.fault = f"bad memory access type={access} addr={addr:#x} size={size} at eip={eip:#x}"
        return False

    # ------------------------------------------------------------------ memory helpers
    def u8(self, a): return self.uc.mem_read(a, 1)[0]
    def u16(self, a): return struct.unpack("<H", self.uc.mem_read(a, 2))[0]
    def u32(self, a): return struct.unpack("<I", self.uc.mem_read(a, 4))[0]
    def s32(self, a): return struct.unpack("<i", self.uc.mem_read(a, 4))[0]
    def f32(self, a): return struct.unpack("<f", self.uc.mem_read(a, 4))[0]
    def f64(self, a): return struct.unpack("<d", self.uc.mem_read(a, 8))[0]
    def w32(self, a, v): self.uc.mem_write(a, struct.pack("<I", v & 0xFFFFFFFF))
    def read(self, a, n): return bytes(self.uc.mem_read(a, n))
    def write(self, a, b): self.uc.mem_write(a, bytes(b))

    def cstr(self, a, maxlen=4096):
        if not a:
            return None
        out = bytearray()
        while len(out) < maxlen:
            chunk = self.uc.mem_read(a + len(out), 64)
            i = chunk.find(b"\0")
            if i >= 0:
                out += chunk[:i]
                break
            out += chunk
        return out.decode("latin1")

    def write_cstr(self, a, s, bufsize=None):
        b = s.encode("latin1")
        if bufsize is not None:
            b = b[: max(bufsize - 1, 0)]
        self.uc.mem_write(a, b + b"\0")
        return len(b)

    def heap_alloc(self, size, zero=True):
        size = align(max(size, 16), 16)
        a = self.heap_top
        self.heap_top += size
        need = align(self.heap_top)
        if need > self.heap_mapped:
            if need > HEAP_BASE + HEAP_SIZE:
                raise EmuStop("emulated heap exhausted")
            self.uc.mem_map(self.heap_mapped, need - self.heap_mapped)
            self.heap_mapped = need
        if zero:
            self.uc.mem_write(a, b"\0" * size)
        return a

    def scratch(self, data_or_size):
        """Allocate host-owned scratch memory (never freed; reset_scratch to reuse)."""
        if isinstance(data_or_size, int):
            data = b"\0" * data_or_size
        else:
            data = bytes(data_or_size)
        a = self.scratch_top
        self.scratch_top = align(self.scratch_top + len(data) + 1, 16)
        if self.scratch_top > SCRATCH_BASE + SCRATCH_SIZE:
            raise EmuStop("scratch exhausted")
        self.uc.mem_write(a, data)
        return a

    def reset_scratch(self, mark):
        self.scratch_top = mark

    # ------------------------------------------------------------------ calling guest code
    def call(self, addr, *args, regs=None, conv="stdcall", count=0):
        """Call guest function. conv: 'stdcall'/'cdecl' (stack args) or 'register'
        (Delphi: EAX, EDX, ECX then stack). Returns (eax, edx)."""
        uc = self.uc
        saved_esp = uc.reg_read(UC_X86_REG_ESP)
        esp = saved_esp
        stack_args = list(args)
        if conv == "register":
            regargs, stack_args = stack_args[:3], stack_args[3:]
            for r, v in zip((UC_X86_REG_EAX, UC_X86_REG_EDX, UC_X86_REG_ECX), regargs):
                uc.reg_write(r, v & 0xFFFFFFFF)
            stack_args = list(stack_args)  # Delphi pushes left-to-right
            for v in stack_args:
                esp -= 4
                self.w32(esp, v)
        else:
            for v in reversed(stack_args):
                esp -= 4
                self.w32(esp, v)
        esp -= 4
        self.w32(esp, STOP_ADDR)
        uc.reg_write(UC_X86_REG_ESP, esp)
        for r, v in (regs or {}).items():
            uc.reg_write(r, v)
        self.fault = None
        try:
            uc.emu_start(addr, STOP_ADDR, count=count)
        except UcError as e:
            eip = uc.reg_read(UC_X86_REG_EIP)
            raise RuntimeError(f"emulation error {e} at eip={eip:#x}; {self.fault or ''}") from None
        if self.fault:
            raise RuntimeError(self.fault)
        eax = uc.reg_read(UC_X86_REG_EAX)
        edx = uc.reg_read(UC_X86_REG_EDX)
        uc.reg_write(UC_X86_REG_ESP, saved_esp)
        return eax, edx

    def call_fpu(self, target, eax=None, edx=None, ecx=None, push=(), fpu_in=(), fpu_out=False, fpcw=PROCESS_FPCW):
        """Call a guest routine through a generated shim.

        push: dwords pushed in order (first pushed first). fpu_in: values loaded
        with FLD (last one ends up in ST0). Returns (eax, edx, st0_fraction or None).
        """
        mark = self.scratch_top
        io = self.scratch(16 * (len(fpu_in) + 2))
        code = bytearray()
        for i, v in enumerate(fpu_in):
            self.write(io + 16 * i, to_ext80(v))
            code += b"\xDB\x2D" + struct.pack("<I", io + 16 * i)
        for reg_op, v in ((0xB8, eax), (0xBA, edx), (0xB9, ecx)):
            if v is not None:
                code += bytes([reg_op]) + struct.pack("<I", v & 0xFFFFFFFF)
        for v in push:
            code += b"\x68" + struct.pack("<I", v & 0xFFFFFFFF)
        shim = self.scratch(len(code) + 32)
        call_at = shim + len(code)
        code += b"\xE8" + struct.pack("<i", target - (call_at + 5))
        out = io + 16 * len(fpu_in)
        if fpu_out:
            code += b"\xDB\x3D" + struct.pack("<I", out)
        code += b"\xC3"
        self.write(shim, code)
        # The shim address is reused: drop any translated block cached for it.
        self.uc.ctl_remove_cache(shim, shim + len(code))
        old_cw = self.uc.reg_read(UC_X86_REG_FPCW)
        self.uc.reg_write(UC_X86_REG_FPCW, fpcw)
        try:
            eax_r, edx_r = self.call(shim)
        finally:
            self.uc.reg_write(UC_X86_REG_FPCW, old_cw)
        st0 = from_ext80(self.read(out, 10)) if fpu_out else None
        self.reset_scratch(mark)
        return eax_r, edx_r, st0

    def read_st(self, i=0):
        """Read x87 ST(i) as an exact Fraction."""
        from unicorn.x86_const import UC_X86_REG_FPSW, UC_X86_REG_FP0
        top = (self.uc.reg_read(UC_X86_REG_FPSW) >> 11) & 7
        m, se = self.uc.reg_read(UC_X86_REG_FP0 + ((top + i) & 7))
        return from_ext80(struct.pack("<QH", m, se))

    def regs(self):
        names = dict(eax=UC_X86_REG_EAX, ebx=UC_X86_REG_EBX, ecx=UC_X86_REG_ECX, edx=UC_X86_REG_EDX,
                     esi=UC_X86_REG_ESI, edi=UC_X86_REG_EDI, ebp=UC_X86_REG_EBP, esp=UC_X86_REG_ESP,
                     eip=UC_X86_REG_EIP)
        return {k: self.uc.reg_read(v) for k, v in names.items()}
