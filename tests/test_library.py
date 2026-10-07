"""Differential test: sound library (CsoundLib), edit buffer (CeditBuf) and host chunks.

The original library/edit buffer of the emulated plugin and the C++ objects are driven in
lockstep through the same operations; after every operation the complete state (512 programs,
library header and clean flag; edit buffer slots, ring index, extension buffers, bank/program,
flags) and every output (file images, chunks, SysEx dumps, names, return values, edit buffer
events) must be identical.
"""
import ctypes
import os
import random
import struct
import sys

import test_program as tp
from harness import started_host
from vsthost import effGetChunk, effSetChunk, effSetProgram

PROG = 0x21C
NPROG = 512
LIB_GLOBAL = 0x494314

# CsoundLib
LIB_LOAD_LIBRARY = 0x455534   # (lib, data, size) -> bool
LIB_SAVE_LIBRARY = 0x455708   # (lib, var ptr, var size) -> bool
LIB_LOAD_BANK = 0x455884      # (lib, data, size; bank) -> bool
LIB_SAVE_BANK = 0x455998      # (lib, bank, var ptr; var size) -> bool
LIB_INIT = 0x4550B0           # (lib, all)
LIB_INIT_BANK = 0x4550F4      # (lib, bank)
LIB_IMPORT_SYX = 0x455A8C     # (lib, data, size; start, check) -> bool
LIB_EXPORT_SYX = 0x455B70     # (lib, start, var ptr) -> size
LIB_WRITE = 0x455DA8          # (lib, index, prog) -> bool
LIB_NAME = 0x455CAC           # (lib, index, var AnsiString)
LIB_RESTORE = 0x45512C        # CsoundLib_v005 (factory + SQ8L_backup.dat)
LIB_BACKUP = 0x4551F8         # CsoundLib_v006 (writes SQ8L_backup.dat)

# CeditBuf
EB_INIT = 0x460554
EB_RESET_ZONE = 0x4605AC
EB_SET_BANK = 0x460630
EB_GET_NAME = 0x4606B4
EB_SET_NAME = 0x46072C
EB_EVENT = 0x460998
EB_SET_CHUNK = 0x460B40
EB_GET_CHUNK = 0x460B84
EB_IMPORT_SYX = 0x460C68
EB_EXPORT_SYX = 0x460D10
EB_SELECT = 0x460DB8
EB_WRITE = 0x460E74
EB_COMPARE = 0x460F2C
EB_COMPARE_OFF = 0x46104C
PARAM_SET = 0x45FB30

EFF_SET_PROGRAM_NAME, EFF_GET_PROGRAM_NAME, EFF_GET_PROGRAM_NAME_INDEXED = 4, 5, 29

EB_FIELDS = [(4, 0xA90 + 4 - 4), (0xA98, 0x6C), (0xB04, 0x6C), (0xB70, 8), (0xB84, 2), (0xB88, 4)]


class Ctx:
    def __init__(self):
        self.api = tp.api()
        tp.API = self.api
        self.h = started_host()
        self.e = self.h.emu
        self.lib = self.e.u32(LIB_GLOBAL)
        self.eb = self.e.u32(tp.master_ptr(self.h) + 0xFF8)
        self.pe = self.e.u32(self.eb + 0xB7C)
        self.clib = self.api.sq8l_lib_new(None, 0)
        self.ceb = self.api.sq8l_eb_new(self.clib)
        self.events = []
        self.e.trace(EB_EVENT, self._on_event, lambda emu, ctx: None)
        self.fail = {}
        self.count = {}

    def _on_event(self, emu):
        r = emu.regs()
        code, arg = r["edx"], r["ecx"]
        slot = -1
        if code in (4, 5):
            slot = (arg - self.eb - 4) // PROG
        self.events += [code, slot]

    def call(self, addr, *args):
        return self.e.call(addr, *args, conv="register")

    # ------------------------------------------------------------ state
    def emu_lib(self):
        e = self.e
        return e.read(self.lib + 0x20, NPROG * PROG), e.read(self.lib + 0x43820, 0x3C), e.u8(self.lib + 0xC)

    def cxx_lib(self):
        a = self.api
        progs = ctypes.string_at(ctypes.cast(a.sq8l_lib_programs(self.clib), ctypes.c_void_p).value, NPROG * PROG)
        hdr = ctypes.string_at(ctypes.cast(a.sq8l_lib_header(self.clib), ctypes.c_void_p).value, 0x3C)
        return progs, hdr, a.sq8l_lib_clean(self.clib)

    def sync_lib(self):
        progs, hdr, clean = self.emu_lib()
        ctypes.memmove(ctypes.cast(self.api.sq8l_lib_programs(self.clib), ctypes.c_void_p).value, progs, len(progs))
        self.api.sq8l_lib_set_state(self.clib, hdr, clean)

    def emu_eb(self):
        img = bytearray(0xB8C)
        for off, n in EB_FIELDS:
            img[off:off + n] = self.e.read(self.eb + off, n)
        return bytes(img)

    def cxx_eb(self):
        b = tp.buf(0xB8C)
        self.api.sq8l_eb_save_state(self.ceb, b)
        img = bytearray(0xB8C)
        for off, n in EB_FIELDS:
            img[off:off + n] = b.raw[off:off + n]
        return bytes(img)

    def sync_eb(self):
        self.api.sq8l_eb_load_state(self.ceb, self.emu_eb())
        self.cxx_events()

    def cxx_events(self):
        out = (ctypes.c_int32 * 4096)()
        n = self.api.sq8l_eb_events(self.ceb, out, 4096)
        return list(out[:n])

    # ------------------------------------------------------------ checking
    def check(self, what, want, got):
        self.count[what] = self.count.get(what, 0) + 1
        if want != got:
            self.fail[what] = self.fail.get(what, 0) + 1
            if self.fail[what] <= 3:
                print(f"  MISMATCH {what}: {describe(want, got)}")
            return False
        return True

    def check_state(self, what, eb=True):
        okl = self.check(what + " [library state]", self.emu_lib(), self.cxx_lib())
        if not okl:
            self.sync_lib()
        if eb:
            events = self.cxx_events()
            if not self.check(what + " [edit buffer state]", self.emu_eb(), self.cxx_eb()):
                self.sync_eb()
            self.check(what + " [events]", self.events, events)
        self.events = []

    def report(self):
        ok = True
        groups = {}
        for k, n in self.count.items():
            g = k.split(" [")[0]
            groups.setdefault(g, [0, 0])
            groups[g][0] += n
            groups[g][1] += self.fail.get(k, 0)
        for g, (n, f) in groups.items():
            print(f"{g}: {n} checks", "OK" if not f else f"{f} MISMATCHES")
            ok &= f == 0
        return ok


def describe(want, got):
    """Short description of the first difference."""
    if isinstance(want, tuple) and isinstance(got, tuple):
        for i, (a, b) in enumerate(zip(want, got)):
            if a != b:
                return f"item {i}: " + describe(a, b)
    if isinstance(want, (bytes, bytearray)) and isinstance(got, (bytes, bytearray)):
        if len(want) != len(got):
            return f"length {len(want)} != {len(got)}"
        d = [i for i in range(len(want)) if want[i] != got[i]]
        return f"{len(d)} bytes differ, first at {d[0]:#x}: want {want[d[0]:d[0]+8].hex()} got {got[d[0]:d[0]+8].hex()}"
    return f"want {str(want)[:100]} got {str(got)[:100]}"


def rand_bytes(rng, n):
    return bytes(rng.randrange(256) for _ in range(n))


def old_program(rng, name):
    r = bytearray(rand_bytes(rng, 0x220))
    for i in range(0, 0x200):          # keep values small like real old records
        r[i] = rng.choice([0, 1, 2, 0x3F, 0xFF, rng.randrange(256)])
    r[0x200:0x202] = b"\x01\x00"
    n = name.encode()[:15]
    r[0x202] = len(n)
    r[0x203:0x203 + len(n)] = n
    return bytes(r)


def lib_tests(c, rng):
    e, api = c.e, c.api
    mark = e.scratch_top

    # library contents after DLL load: header, 512 programs, flag
    c.check("startup library (factory C/D, A/B = copies)", c.emu_lib(), c.cxx_lib())

    def emu_save_library():
        v = e.scratch(8)
        ok, _ = c.call(LIB_SAVE_LIBRARY, c.lib, v, v + 4)
        return ok & 0xFF, e.read(e.u32(v), e.u32(v + 4))

    def cxx_save(fn, *args):
        out = tp.buf(0x22000)
        n = fn(c.clib, *args, out, 0x22000)
        return out.raw[:n]

    def emu_load(addr, data, *extra):
        a = e.scratch(data) if data else 0
        r, _ = c.call(addr, c.lib, a, len(data), *extra)
        e.reset_scratch(mark)
        return r & 0xFF

    for rep in range(2):
        want = emu_save_library()
        got = (1, cxx_save(api.sq8l_lib_save_library))
        c.check("save library", want, got)
        c.check_state("save library", eb=False)
        e.reset_scratch(mark)

    for bank in (-3, -1, 0, 1, 2, 3, 4, 7):
        v = e.scratch(8)
        ok, _ = c.call(LIB_SAVE_BANK, c.lib, bank & 0xFFFFFFFF, v, v + 4)
        want = (ok & 0xFF, e.read(e.u32(v), e.u32(v + 4)) if ok & 0xFF else b"")
        got = cxx_save(api.sq8l_lib_save_bank, bank)
        c.check("save bank", want, (int(bool(got)), got))
        c.check_state("save bank", eb=False)
        e.reset_scratch(mark)

    # scramble a few slots so later loads are visible, write programs (protected or not)
    for k in range(60):
        idx = rng.choice([rng.randrange(-5, 520), rng.randrange(0, 256), rng.randrange(256, 512)])
        p = rand_bytes(rng, PROG)
        a = e.scratch(p)
        r, _ = c.call(LIB_WRITE, c.lib, idx & 0xFFFFFFFF, a)
        c.check("write program", r & 0xFF, api.sq8l_lib_write_program(c.clib, idx, p))
        e.reset_scratch(mark)
    c.check_state("write program", eb=False)

    # names (AnsiString from the ShortString at +2, full length byte)
    out = ctypes.create_string_buffer(300)
    for idx in list(range(-2, 514)):
        v = e.scratch(4)
        c.call(LIB_NAME, c.lib, idx & 0xFFFFFFFF, v)
        s = e.u32(v)
        want = e.read(s, e.u32(s - 4)) if s else b""
        n = api.sq8l_lib_program_name(c.clib, idx, out, 300)
        c.check("program names", want, out.raw[:n])
        e.reset_scratch(mark)

    # library file images: valid (various counts), old format, broken
    base = cxx_save(api.sq8l_lib_save_library)
    files = []
    for count in (0x100, 0, 5, 0x80, 0x200, -1):
        f = bytearray(base)
        f[0x14:0x18] = struct.pack("<i", count)
        f[0x18:0x20] = b"\x05HELLO\x00\x00"
        for i in range(0, 256, 17):
            f[0x3C + i * PROG + 3] ^= 0x55
        files.append(bytes(f))
    bad_magic = bytearray(base)
    bad_magic[5] = ord("X")
    files.append(bytes(bad_magic))
    files.append(struct.pack("<I", 3) + base[4:])
    old = struct.pack("<I", 1) + bytes(0x10) + b"".join(old_program(rng, f"OLD{i}") for i in range(128))
    files.append(old)
    for f in files:
        c.check("load library", emu_load(LIB_LOAD_LIBRARY, f), api.sq8l_lib_load_library(c.clib, f, len(f)))
        c.check_state("load library", eb=False)

    # bank files into banks -1..4 (factory banks are allowed at this level)
    bank_img = cxx_save(api.sq8l_lib_save_bank, 1)
    old_bank = struct.pack("<I", 1) + bytes(0x10) + b"".join(old_program(rng, f"OB{i}") for i in range(128))
    for bank in (-1, 0, 1, 2, 3, 4):
        for f in (bank_img, old_bank, bytes(bad_magic[:0x10E3C])):
            g = bytearray(f)
            if f is bank_img:
                g[0x14:0x18] = struct.pack("<i", rng.choice([0x80, 3, 0x100]))
            g = bytes(g)
            c.check("load bank", emu_load(LIB_LOAD_BANK, g, bank & 0xFFFFFFFF),
                    api.sq8l_lib_load_bank(c.clib, g, len(g), bank))
            c.check_state("load bank", eb=False)

    # init bank / library
    for bank in (-1, 1, 3, 5):
        c.call(LIB_INIT_BANK, c.lib, bank & 0xFFFFFFFF)
        api.sq8l_lib_init_bank(c.clib, bank)
        c.check_state("init bank", eb=False)

    # SysEx bank export/import
    for start in (0, 37, 128, 300, 384, 472, 473, 500, -1):
        v = e.scratch(4)
        n, _ = c.call(LIB_EXPORT_SYX, c.lib, start & 0xFFFFFFFF, v)
        want = e.read(e.u32(v), n)
        got = cxx_save(api.sq8l_lib_export_sysex_bank, start)
        if start + 40 > 512 or start < 0:  # partially filled: the rest is uninitialised memory
            k = 5 + 0xCC * max(0, min(40, 512 - start) if start >= 0 else 0)
            want, got = want[:k], got[:k]
        c.check("export SysEx bank", want, got)
        c.check_state("export SysEx bank", eb=False)
        e.reset_scratch(mark)
    dump = cxx_save(api.sq8l_lib_export_sysex_bank, 384)
    for start, check, data in ((0, 1, dump), (128, 1, dump), (200, 0, dump), (480, 1, dump), (-3, 1, dump),
                               (10, 1, b"\xF0\x0F\x02\x00\x01" + dump[5:]), (10, 0, b"\x00" * 8165),
                               (20, 1, dump[:8164]), (50, 0, rand_bytes(rng, 8200))):
        a = e.scratch(data)
        r, _ = c.call(LIB_IMPORT_SYX, c.lib, a, len(data), start & 0xFFFFFFFF, check)
        c.check("import SysEx bank", r & 0xFF, api.sq8l_lib_import_sysex_bank(c.clib, data, len(data), start, check))
        c.check_state("import SysEx bank", eb=False)
        e.reset_scratch(mark)

    # init library (user banks) and full init
    for all_ in (0, 1):
        c.call(LIB_INIT, c.lib, all_)
        api.sq8l_lib_init_library(c.clib, all_)
        c.check_state("init library", eb=False)


def backup_tests(c, rng):
    """CsoundLib_v006 writes SQ8L_backup.dat into the sandbox; CsoundLib_v005 restores it."""
    e, api = c.e, c.api
    path = os.path.join(c.h.sandbox, "C", "VST", "SQ8L", "SQ8L_backup.dat")
    # make the user banks distinguishable
    for k in range(20):
        idx = rng.randrange(256)
        p = rand_bytes(rng, PROG)
        a = e.scratch(p)
        c.call(LIB_WRITE, c.lib, idx, a)
        api.sq8l_lib_write_program(c.clib, idx, p)
    if os.path.exists(path):
        os.remove(path)
    c.call(LIB_BACKUP, c.lib)
    data = open(path, "rb").read()
    out = tp.buf(0x22000)
    n = api.sq8l_lib_save_backup(c.clib, out, 0x22000)
    c.check("backup file written by the original", data, out.raw[:n])
    c.check_state("backup save", eb=False)

    variants = [("valid", data), ("old format", struct.pack("<I", 1) + bytes(0x10) +
                                  b"".join(old_program(rng, f"BK{i}") for i in range(128))),
                ("corrupt", b"garbage" * 10), ("missing", None)]
    for name, blob in variants:
        if blob is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            open(path, "wb").write(blob)
        c.call(LIB_RESTORE, c.lib)
        api.sq8l_lib_restore_backup(c.clib, blob, len(blob) if blob else 0)
        c.check_state(f"restore backup ({name})", eb=False)
        if name in ("valid", "missing"):
            # what a fresh library (DLL load) restores from the same file
            lib2 = api.sq8l_lib_new(blob, len(blob)) if blob else api.sq8l_lib_new(None, 0)
            progs = ctypes.string_at(ctypes.cast(api.sq8l_lib_programs(lib2), ctypes.c_void_p).value, NPROG * PROG)
            hdr = ctypes.string_at(ctypes.cast(api.sq8l_lib_header(lib2), ctypes.c_void_p).value, 0x3C)
            c.check(f"restore backup ({name})", c.emu_lib(), (progs, hdr, api.sq8l_lib_clean(lib2)))
            api.sq8l_lib_free(lib2)
    if os.path.exists(path):
        os.remove(path)


def eb_tests(c, rng):
    e, api = c.e, c.api
    mark = e.scratch_top
    c.sync_lib()
    c.sync_eb()
    c.events = []
    c.check_state("initial edit buffer")

    def ansistr(s):
        b = s.encode("latin1")
        return e.scratch(struct.pack("<iI", -1, len(b)) + b + b"\0") + 8 if b else 0

    def op_select():
        prog = rng.choice([rng.randrange(128), rng.randrange(128), -1, 130, 1000])
        bank = rng.choice([0, 1, 2, 3, -1, 4])
        r, _ = c.call(EB_SELECT, c.eb, prog & 0xFFFFFFFF, bank & 0xFFFFFFFF)
        c.check("select program", r & 0xFF, api.sq8l_eb_select(c.ceb, prog, bank))
        c.check_state("select program")

    def op_set_program():
        idx = rng.choice([rng.randrange(512), rng.randrange(-3, 600)])
        c.h.dispatch(effSetProgram, value=idx & 0xFFFFFFFF)
        api.sq8l_eb_select_index(c.ceb, idx)
        c.check_state("effSetProgram")

    def op_write():
        prog = rng.choice([rng.randrange(128), -1, 200])
        bank = rng.choice([0, 1, 2, 3, -1, 5])
        r, _ = c.call(EB_WRITE, c.eb, prog & 0xFFFFFFFF, bank & 0xFFFFFFFF)
        c.check("write program", r & 0xFF, api.sq8l_eb_write(c.ceb, prog, bank))
        c.check_state("write program")

    def op_compare():
        if rng.random() < 0.6:
            prog, bank = rng.randrange(128), rng.choice([0, 1, 2, 3, -1, 9])
            c.call(EB_COMPARE, c.eb, prog, bank & 0xFFFFFFFF)
            api.sq8l_eb_compare(c.ceb, prog, bank)
            c.check_state("compare on")
        else:
            c.call(EB_COMPARE_OFF, c.eb)
            api.sq8l_eb_compare_off(c.ceb)
            c.check_state("compare off")

    def op_init():
        c.call(EB_INIT, c.eb)
        api.sq8l_eb_init_program(c.ceb)
        c.check_state("init program")

    def op_zone():
        z = rng.choice([-1, 0, 1, 2])
        c.call(EB_RESET_ZONE, c.eb, z & 0xFFFFFFFF)
        api.sq8l_eb_reset_ext_zone(c.ceb, z)
        c.check_state("reset ext zone")

    def op_bank():
        b = rng.choice([-2, 0, 1, 2, 3, 4, 9])
        c.call(EB_SET_BANK, c.eb, b & 0xFFFFFFFF)
        api.sq8l_eb_set_bank(c.ceb, b)
        c.check_state("set bank")

    def op_name():
        which = rng.choice([0, 1, 2])
        s = "".join(chr(rng.randrange(32, 127)) for _ in range(rng.randrange(0, 22)))
        c.call(EB_SET_NAME, c.eb, which, ansistr(s))
        api.sq8l_eb_set_name(c.ceb, which, s.encode("latin1"), len(s))
        for w in (0, 1):
            v = e.scratch(4)
            c.call(EB_GET_NAME, c.eb, w, v)
            p = e.u32(v)
            out = ctypes.create_string_buffer(300)
            n = api.sq8l_eb_name(c.ceb, w, out, 300)
            c.check("names", e.read(p, e.u32(p - 4)) if p else b"", out.raw[:n])
        c.check_state("set name")
        e.reset_scratch(mark)

    def emu_chunk():
        v = e.scratch(4)
        n, _ = c.call(EB_GET_CHUNK, c.eb, v)
        return e.read(e.u32(v), n)

    def cxx_chunk():
        out = tp.buf(0x300)
        n = api.sq8l_eb_get_chunk(c.ceb, out, 0x300)
        return out.raw[:n]

    def op_get_chunk():
        c.check("getChunk", emu_chunk(), cxx_chunk())
        c.check_state("getChunk")
        e.reset_scratch(mark)

    def op_set_chunk():
        kind = rng.randrange(6)
        ch = bytearray(cxx_chunk())
        e.reset_scratch(mark)
        c.sync_eb()  # getChunk above changed only the C++ side (ext copy): resync
        if kind == 0:   # chunk of a library program, other program number / modified flag
            p = ctypes.string_at(ctypes.cast(api.sq8l_lib_programs(c.clib), ctypes.c_void_p).value
                                 + rng.randrange(512) * PROG, PROG)
            ch[0x1F:] = p
            ch[0x18:0x1A] = struct.pack("<h", rng.randrange(-200, 300))
            ch[0x1A] = rng.randrange(2)
        elif kind == 1:
            ch[0x1F:] = rand_bytes(rng, PROG)
        elif kind == 2:  # bad magic / type
            ch[rng.choice([0, 4, 5, 9])] ^= 1
        elif kind == 3:  # pre-0.90 chunk (old program record)
            ch = bytearray(old_program(rng, "OLDCHUNK"))
            if rng.random() < 0.3:
                ch[0x200] = 2
        elif kind == 4:  # unexpected size
            ch = ch + b"\0"
        data = bytes(ch)
        a = e.scratch(data)
        r, _ = c.call(EB_SET_CHUNK, c.eb, a, len(data))
        c.check("setChunk", r, api.sq8l_eb_set_chunk(c.ceb, data, len(data)))
        c.check_state("setChunk")
        e.reset_scratch(mark)

    def op_sysex():
        if rng.random() < 0.5:
            v = e.scratch(4)
            n, _ = c.call(EB_EXPORT_SYX, c.eb, v)
            want = e.read(e.u32(v), n)
            out = tp.buf(0x100)
            m = api.sq8l_eb_export_sysex(c.ceb, out, 0x100)
            c.check("export SysEx program", want, out.raw[:m])
            c.check_state("export SysEx program")
        else:
            out = tp.buf(0x100)
            api.sq8l_eb_export_sysex(c.ceb, out, 0x100)
            data = bytearray(out.raw[:210])
            p = ctypes.string_at(ctypes.cast(api.sq8l_lib_programs(c.clib), ctypes.c_void_p).value
                                 + rng.randrange(512) * PROG, PROG)
            nyb = ctypes.create_string_buffer(0xCC)
            api.sq8l_sysex_to_nybbles(p, nyb, 0)
            data[5:5 + 0xCC] = nyb.raw
            kind = rng.randrange(5)
            if kind == 1:
                data[4] = 2
            elif kind == 2:
                data[1] = 0x10
            elif kind == 3:
                data = data[:0xD0]
            check = rng.randrange(2)
            a = e.scratch(bytes(data))
            r, _ = c.call(EB_IMPORT_SYX, c.eb, a, len(data), check)
            c.check("import SysEx program", r & 0xFF,
                    api.sq8l_eb_import_sysex(c.ceb, bytes(data), len(data), check))
            c.check_state("import SysEx program")
        e.reset_scratch(mark)

    def op_param():
        i = rng.randrange(398)
        v = rng.choice([0, 1, -1, 63, rng.randrange(-300, 300)])
        c.call(PARAM_SET, c.pe, i, v & 0xFFFFFFFF)
        api.sq8l_eb_set_param(c.ceb, i, v)
        c.check_state("set parameter")

    ops = [op_select] * 6 + [op_set_program] * 4 + [op_write] * 3 + [op_compare] * 4 + [op_init, op_zone, op_bank] \
        + [op_name] * 2 + [op_get_chunk] * 3 + [op_set_chunk] * 4 + [op_sysex] * 3 + [op_param] * 3
    for k in range(5000):
        rng.choice(ops)()


def vst_tests(c, rng):
    """Host chunk through the VST dispatcher of the original."""
    e, api = c.e, c.api
    mark = e.scratch_top
    c.sync_lib()
    c.sync_eb()
    c.events = []
    for k in range(40):
        idx = rng.randrange(512)
        c.h.dispatch(effSetProgram, value=idx)
        api.sq8l_eb_select_index(c.ceb, idx)
        v = e.scratch(4)
        n = c.h.dispatch(effGetChunk, ptr=v)
        out = tp.buf(0x300)
        m = api.sq8l_eb_get_chunk(c.ceb, out, 0x300)
        c.check("effGetChunk after effSetProgram", e.read(e.u32(v), n), out.raw[:m])
        c.check_state("effGetChunk")
        # setChunk with the chunk of another program; the next effSetProgram is then ignored
        other = bytearray(out.raw[:m])
        p = ctypes.string_at(ctypes.cast(api.sq8l_lib_programs(c.clib), ctypes.c_void_p).value
                             + rng.randrange(512) * PROG, PROG)
        other[0x1F:] = p
        a = e.scratch(bytes(other))
        r = c.h.dispatch(effSetChunk, value=len(other), ptr=a)
        api.sq8l_eb_set_chunk(c.ceb, bytes(other), len(other))
        c.check("effSetChunk result", r, 0)
        c.check_state("effSetChunk")
        c.h.dispatch(effSetProgram, value=rng.randrange(512))  # swallowed by CSynth (+0xb4)
        c.check_state("effSetProgram after effSetChunk (ignored)")
        e.reset_scratch(mark)

    # program names seen by the host
    out = ctypes.create_string_buffer(300)
    for idx in list(range(512)) + [-1, 512]:
        s = c.h.string_op(EFF_GET_PROGRAM_NAME_INDEXED, idx)
        n = api.sq8l_lib_program_name(c.clib, idx, out, 300)
        # StrPCopy into the host buffer: the host sees the name up to the first NUL
        c.check("effGetProgramNameIndexed", s.encode("latin1"), out.raw[:n].split(b"\0")[0])
    for k in range(20):
        name = "".join(chr(rng.randrange(32, 127)) for _ in range(rng.randrange(0, 24)))
        a = e.scratch(name.encode("latin1") + b"\0")
        c.h.dispatch(EFF_SET_PROGRAM_NAME, ptr=a)
        api.sq8l_eb_set_name(c.ceb, 0, name.encode("latin1"), len(name))
        n = api.sq8l_eb_name(c.ceb, 0, out, 300)
        c.check("effGetProgramName", c.h.string_op(EFF_GET_PROGRAM_NAME).encode("latin1"), out.raw[:n])
        c.check_state("effSetProgramName")
        e.reset_scratch(mark)


def startup_tests(c):
    """A new instance: CeditBuf constructor (INIT) + master constructor selectProgram(0, -1)."""
    api = c.api
    lib = api.sq8l_lib_new(None, 0)
    eb = api.sq8l_eb_new(lib)
    api.sq8l_eb_select(eb, 0, -1)
    b = tp.buf(0xB8C)
    api.sq8l_eb_save_state(eb, b)
    img = bytearray(0xB8C)
    for off, n in EB_FIELDS:
        img[off:off + n] = b.raw[off:off + n]
    c.check("startup edit buffer (INIT + select(0, -1))", c.emu_eb(), bytes(img))
    out = (ctypes.c_int32 * 16)()
    n = api.sq8l_eb_events(eb, out, 16)
    c.check("startup edit buffer events", [4, 1, 5, 2, 0, -1], list(out[:n]))
    api.sq8l_eb_free(eb)
    api.sq8l_lib_free(lib)


def main():
    rng = random.Random(4321)
    c = Ctx()
    startup_tests(c)
    lib_tests(c, rng)
    backup_tests(c, rng)
    eb_tests(c, rng)
    vst_tests(c, rng)
    ok = c.report()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
