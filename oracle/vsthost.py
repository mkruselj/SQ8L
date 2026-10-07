"""Headless VST 2.x host running the original SQ8L.dll inside the emulator."""
import os
import shutil
import struct
import sys

from w32emu import Win32Emu
from winapi import WinApi

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DLL = os.path.join(ROOT, "original", "SQ8L.dll")

# AEffect field offsets (32-bit)
AE_MAGIC, AE_DISPATCHER, AE_SETPARAM, AE_GETPARAM = 0, 4, 12, 16
AE_NUMPROGRAMS, AE_NUMPARAMS, AE_NUMIN, AE_NUMOUT, AE_FLAGS = 20, 24, 28, 32, 36
AE_OBJECT, AE_UNIQUEID, AE_VERSION, AE_PROCREPL = 64, 72, 76, 80

effOpen, effClose, effSetProgram, effGetProgram = 0, 1, 2, 3
effGetProgramName = 5
effSetSampleRate, effSetBlockSize, effMainsChanged = 10, 11, 12
effGetChunk, effSetChunk, effProcessEvents = 23, 24, 25
effGetEffectName, effGetVendorString, effGetProductString = 45, 47, 48
effStartProcess, effStopProcess = 71, 72


class SQ8LHost:
    def __init__(self, sandbox=None, sample_rate=44100.0, block_size=256, verbose=False, trace_api=False, gui=False):
        self.sandbox = sandbox or os.path.join(HERE, "sandbox")
        vstdir = os.path.join(self.sandbox, "C", "VST", "SQ8L")
        os.makedirs(vstdir, exist_ok=True)
        ini = os.path.join(vstdir, "SQ8L.ini")
        if not os.path.exists(ini):
            shutil.copy(os.path.join(ROOT, "original", "SQ8L.ini"), ini)
        self.sr = sample_rate
        self.block = block_size
        self.emu = Win32Emu(DLL, trace_api=trace_api)
        self.api = WinApi(self.emu, self.sandbox, verbose=verbose)
        if gui:
            import gui as gui_module  # registers the USER32/GDI32 window system
            self.gui = gui_module.gui(self.emu)
        self.verbose = verbose
        self.master = self.emu.make_callback("audioMaster", 6, self._audio_master, cdecl=True)
        self.effect = None

    # ------------------------------------------------------------------ host callback
    def _audio_master(self, emu, effect, opcode, index, value, ptr, opt):
        r = 0
        if opcode == 1:            # audioMasterVersion
            r = 2400
        elif opcode == 6:          # audioMasterWantMidi
            r = 1
        elif opcode == 16:         # audioMasterGetSampleRate
            r = int(self.sr)
        elif opcode == 17:         # audioMasterGetBlockSize
            r = self.block
        elif opcode in (32, 33):   # vendor/product string
            emu.write_cstr(ptr, "SQ8L-oracle")
            r = 1
        elif opcode == 34:
            r = 1
        elif opcode == 38:         # language
            r = 1
        elif opcode == 41:         # audioMasterGetDirectory
            if not hasattr(self, "_dirptr"):
                self._dirptr = emu.scratch(b"C:\\VST\\\0")
            r = self._dirptr
        if self.verbose:
            print(f"   audioMaster(op={opcode}, idx={index}, val={value:#x}, ptr={ptr:#x}) -> {r}", file=sys.stderr)
        return r

    # ------------------------------------------------------------------ lifecycle
    def load(self):
        e = self.emu
        ep = e.base + e.pe.OPTIONAL_HEADER.AddressOfEntryPoint
        eax, _ = e.call(ep, e.base, 1, 0)                    # DllMain(PROCESS_ATTACH)
        if self.verbose:
            print(f"DllMain -> {eax}", file=sys.stderr)
        eff, _ = e.call(e.exports["main"], self.master, conv="cdecl")
        if not eff:
            raise RuntimeError("main() returned NULL")
        if e.u32(eff + AE_MAGIC) != 0x56737450:
            raise RuntimeError("bad AEffect magic")
        self.effect = eff
        return eff

    def info(self):
        e, eff = self.emu, self.effect
        return dict(
            numPrograms=e.s32(eff + AE_NUMPROGRAMS), numParams=e.s32(eff + AE_NUMPARAMS),
            numInputs=e.s32(eff + AE_NUMIN), numOutputs=e.s32(eff + AE_NUMOUT),
            flags=hex(e.u32(eff + AE_FLAGS)), uniqueID=e.read(eff + AE_UNIQUEID, 4)[::-1],
            version=e.s32(eff + AE_VERSION), processReplacing=hex(e.u32(eff + AE_PROCREPL)),
            dispatcher=hex(e.u32(eff + AE_DISPATCHER)), object=hex(e.u32(eff + AE_OBJECT)),
        )

    def dispatch(self, opcode, index=0, value=0, ptr=0, opt=0.0):
        e = self.emu
        fn = e.u32(self.effect + AE_DISPATCHER)
        optbits = struct.unpack("<I", struct.pack("<f", opt))[0]
        r, _ = e.call(fn, self.effect, opcode, index, value, ptr, optbits, conv="cdecl")
        return r

    def open_editor(self, parent_hwnd=None):
        """effEditOpen with a host window created in the emulated window system."""
        g = self.gui
        from gui import Window
        if parent_hwnd is None:
            parent_hwnd = 0x10020
            g.windows[parent_hwnd] = Window(parent_hwnd, "host", g.sys_proc, 0, 0, 0, 640, 480, 0x10000000, 0, "host")
        r = self.dispatch(14, ptr=parent_hwnd)
        return parent_hwnd, r

    def string_op(self, opcode, index=0, size=256):
        mark = self.emu.scratch_top
        buf = self.emu.scratch(size)
        self.dispatch(opcode, index, 0, buf)
        s = self.emu.cstr(buf)
        self.emu.reset_scratch(mark)
        return s

    def start(self):
        self.dispatch(effOpen)
        self.dispatch(effSetSampleRate, opt=self.sr)
        self.dispatch(effSetBlockSize, value=self.block)
        self.dispatch(effMainsChanged, value=1)
        self.dispatch(effStartProcess)
        # persistent audio buffers
        e = self.emu
        n = self.block
        self.outbufs = [e.heap_alloc(4 * n) for _ in range(2)]
        self.inbufs = [e.heap_alloc(4 * n) for _ in range(2)]
        self.outptrs = e.heap_alloc(8)
        self.inptrs = e.heap_alloc(8)
        e.write(self.outptrs, struct.pack("<II", *self.outbufs))
        e.write(self.inptrs, struct.pack("<II", *self.inbufs))
        self.events_buf = e.heap_alloc(8 + 4 * 256 + 32 * 256)

    def send_midi(self, events):
        """events: list of (delta_frames, bytes3)."""
        e = self.emu
        base = self.events_buf
        n = len(events)
        e.write(base, struct.pack("<iI", n, 0))
        evs = base + 8 + 4 * 256
        for i, (delta, data) in enumerate(events):
            ev = evs + 32 * i
            md = bytes(data) + bytes(4 - len(data))
            e.write(ev, struct.pack("<iiiiii", 1, 32, delta, 0, 0, 0) + md + struct.pack("<bbbb", 0, 0, 0, 0))
            e.w32(base + 8 + 4 * i, ev)
        self.dispatch(effProcessEvents, ptr=base)

    def process(self, nframes=None):
        e = self.emu
        n = nframes or self.block
        for b in self.outbufs:
            e.write(b, bytes(4 * n))
        fn = e.u32(self.effect + AE_PROCREPL)
        e.call(fn, self.effect, self.inptrs, self.outptrs, n, conv="cdecl")
        left = struct.unpack(f"<{n}f", e.read(self.outbufs[0], 4 * n))
        right = struct.unpack(f"<{n}f", e.read(self.outbufs[1], 4 * n))
        return left, right


if __name__ == "__main__":
    h = SQ8LHost(verbose="-v" in sys.argv, trace_api="-t" in sys.argv)
    h.load()
    print(h.info())
