"""Capture/replay machinery for the differential test of the master (test_master.py).

Capture (emulated original): when a master routine of interest is entered (or a
whole processEvents+process block is run), the master state, the module-visible
state (LFO parameter blocks, env "active" bytes, amp ramp counters, Cdoc tail) and
the referenced program records are snapshotted; every call the master makes into a
module (return address inside unit plugCore) is recorded with its decoded arguments,
its result and the module state the master may read afterwards; on exit the master
and filter states are snapshotted again.

Replay (C++): the entry state is loaded into sq8l::Master, the routine is run with a
forwarding VoiceModules whose calls are checked one by one against the recorded
ones (same method, voice, index and arguments) and answered with the recorded
results; finally the C++ state is compared with the exit snapshot."""
import ctypes
import struct
from fractions import Fraction

import numpy as np

from unicorn import UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_ESP, UC_X86_REG_FPCW

from harness import lib, master_ptr

MASTER_SIZE = 0x12054
VOICE_SIZE = 0xE8
FILTER_SIZE = 0x7C
PROGRAM_SIZE = 0x21C
PLUGCORE = (0x461C7C, 0x4648A4)

# ---------------------------------------------------------------- method ids (capi_master.cpp)
(M_DOC_RESET, M_DOC_NUMVOICES, M_DOC_SR, M_DOC_NOTEON, M_DOC_SETKEY, M_DOC_STOP, M_DOC_TICKODD,
 M_DOC_UPDATE, M_DOC_RENDER, M_DOC_TAIL,
 M_LFO_RATE, M_LFO_START, M_LFO_TICK,
 M_ENV_RATE, M_ENV_START, M_ENV_RELEASE, M_ENV_TICK, M_ENV_LEVEL, M_ENV_ACTIVE,
 M_AMP_SR, M_AMP_DRIVE, M_AMP_START, M_AMP_SMOOTH, M_AMP_CURVE, M_AMP_LEVEL, M_AMP_PROCESS,
 M_AMP_FADED, M_AMP_COUNTER,
 M_FOLL_RATE, M_FOLL_TIME, M_FOLL_TARGET, M_FOLL_VALUE, M_FOLL_TICK) = range(1, 34)

METHOD_NAMES = {v: k[2:].lower() for k, v in dict(globals()).items() if k.startswith("M_")}

# Entry points of the module routines the master calls: address -> (method, object kind)
MODULE_ENTRIES = {
    0x45BC18: (M_DOC_RESET, "doc"), 0x45BC20: (M_DOC_NUMVOICES, "doc"), 0x45BC4C: (M_DOC_SR, "doc"),
    0x45BCD0: (M_DOC_NOTEON, "doc"), 0x45C460: (M_DOC_SETKEY, "doc"), 0x45BD78: (M_DOC_STOP, "doc"),
    0x45C378: (M_DOC_TICKODD, "doc"), 0x45BDA4: (M_DOC_UPDATE, "doc"), 0x45C7F4: (M_DOC_RENDER, "doc"),
    0x45D020: (M_LFO_RATE, "lfo"), 0x45D2A4: (M_LFO_START, "lfo"), 0x45DB2C: (M_LFO_TICK, "lfo"),
    0x45DCB8: (M_ENV_RATE, "env"), 0x45DD2C: (M_ENV_START, "env"), 0x45DFB4: (M_ENV_RELEASE, "env"),
    0x45E210: (M_ENV_TICK, "env"), 0x45E1D4: (M_ENV_LEVEL, "env"),
    0x45E7B8: (M_AMP_SR, "amp"), 0x45E89C: (M_AMP_DRIVE, "amp"), 0x45E554: (M_AMP_START, "amp"),
    0x45E7F8: (M_AMP_SMOOTH, "amp"), 0x45E848: (M_AMP_CURVE, "amp"), 0x45E69C: (M_AMP_LEVEL, "amp"),
    0x45E8EC: (M_AMP_PROCESS, "amp"), 0x45E95C: (M_AMP_FADED, "amp"),
    0x45EBA0: (M_FOLL_RATE, "foll"), 0x45EBE8: (M_FOLL_TIME, "foll"), 0x45EC1C: (M_FOLL_TARGET, "foll"),
    0x45EC68: (M_FOLL_VALUE, "foll"), 0x45EC90: (M_FOLL_TICK, "foll"),
}
# Called every sample: hooked only while a block is captured (Tracer.set_hot).
HOT_MODULE_ENTRIES = {0x45C7F4, 0x45E8EC, 0x45E95C}
HOT_ROUTINES = {0x464410, 0x4644F4}
RESULT_EAX = {M_LFO_TICK, M_ENV_TICK, M_ENV_LEVEL, M_FOLL_TICK}
FLOAT_ARGS = {M_DOC_SR: (0,), M_LFO_RATE: (0,), M_ENV_RATE: (0,), M_AMP_SR: (0,), M_AMP_SMOOTH: (0,),
              M_AMP_LEVEL: (2,), M_AMP_PROCESS: (1, 2), M_AMP_FADED: (1, 2, 3), M_FOLL_RATE: (0,),
              M_FOLL_TIME: (0,)}

# Master routines (unit plugCore) for call statistics.
MASTER_ROUTINES = {
    0x462174: "reset", 0x462214: "setSampleRate", 0x462380: "setControlRate", 0x462488: "resetNoteStacks",
    0x4624A8: "pushNoteStack", 0x4624F4: "removeHeldKey", 0x462544: "clearVoice", 0x462570: "resetVoices",
    0x4625D4: "startVoice", 0x462C04: "stealStart", 0x462CA0: "retarget", 0x462DA4: "setKeyScaling",
    0x462DE4: "release", 0x462E30: "kill", 0x462E6C: "killProgram", 0x462E9C: "releaseProgram",
    0x462ECC: "fadeOut", 0x462F18: "fadeIn", 0x462F64: "listAdd", 0x462F90: "listRemove",
    0x46300C: "allocate", 0x4630CC: "findStealTarget", 0x463230: "editBufferEvent", 0x463328: "activeVoiceCount",
    0x463358: "setVoices", 0x4632E4: "setNumVoices", 0x463388: "noteCallback", 0x463420: "noteEvent",
    0x463714: "midiControl", 0x4637D4: "storeController", 0x463808: "modSource", 0x463890: "controlUpdate",
    0x464250: "computeMuffle", 0x4643C8: "setMuffle", 0x464410: "processVoice", 0x4644F4: "muffle",
    0x4645C8: "process", 0x4620E4: "loadOverrides",
}


# Basic blocks of the original master whose execution is counted (branch coverage).
BRANCHES = {
    # controlUpdate FUN_00463890
    0x4638AD: "ctrl.oddTick", 0x4638C9: "ctrl.oddParityNegative", 0x46393F: "ctrl.glideTick",
    0x46396A: "ctrl.bendMode>=4", 0x46397E: "ctrl.bendMode1", 0x46398E: "ctrl.bendMode2", 0x4639A2: "ctrl.bendMode3",
    0x4639C5: "ctrl.bendModeOther", 0x4639CB: "ctrl.bendOn", 0x4639F6: "ctrl.bendStore", 0x463A10: "ctrl.bendOff",
    0x463B33: "lfo.fmAdd", 0x463B45: "lfo.fmMode2", 0x463B5A: "lfo.fmMode3", 0x463BA2: "lfo.amAdd",
    0x463BBB: "lfo.amMode2", 0x463BD0: "lfo.amMode3", 0x463BFE: "lfo.levelNormal", 0x463C0C: "lfo.levelAlt",
    0x463C26: "lfo.p34Plain", 0x463C38: "lfo.p34Mod", 0x463CFB: "mat.compute", 0x463D19: "mat.zero",
    0x463DB0: "doc.smoothProg", 0x463DCA: "doc.smoothOverride", 0x463DDB: "doc.dcbProg", 0x463DF2: "doc.dcbOverride",
    0x463E11: "doc.dcb0on", 0x463E1E: "doc.dcb0off", 0x463E29: "doc.dcb1", 0x463E36: "doc.dcbOther",
    0x463FBE: "flt.smooth", 0x463FC2: "flt.immediate", 0x46417E: "muf.olderVoice", 0x46418D: "muf.owner",
    0x4641AA: "muf.prog", 0x4641C4: "muf.override", 0x4641D5: "muf.switch", 0x4641ED: "amp.smoothTick1",
    0x464224: "ctrl.tickCountInc",
    # startVoice FUN_004625d4
    0x462687: "start.fadeOutLen", 0x462699: "start.fadeOutZero", 0x4626AA: "start.fadeInIdxClamp",
    0x4626D5: "start.fadeInLen", 0x4626E7: "start.fadeInZero", 0x4626FA: "start.stolen", 0x462722: "start.legatoSteal",
    0x462733: "start.noStolen", 0x462744: "start.rec", 0x46276A: "start.noRec", 0x462790: "start.noGlideFrom",
    0x4627D7: "start.fullInit", 0x4627F6: "start.lfoReset", 0x4627FC: "start.lfoFree", 0x4628D3: "start.envCycle",
    0x4628E4: "start.envNormal", 0x462906: "start.envLegato", 0x462974: "start.newNote", 0x46297E: "start.legato",
    0x462996: "start.newPlain", 0x4629AC: "start.newSteal", 0x4629D5: "start.dca4Prog", 0x4629EC: "start.dca4Override",
    0x462A7C: "start.docStolen", 0x462AA6: "start.docNoStolen", 0x462B15: "start.glide", 0x462B97: "start.noGlide",
    0x462BBB: "start.docSetKey",
    # voice management
    0x462C3A: "steal.flag", 0x462C45: "steal.swap", 0x462CE5: "retarget.glide", 0x462D59: "retarget.noGlide",
    0x462E07: "release.do", 0x462EF9: "fadeOut.len", 0x462F0B: "fadeOut.zero", 0x462F31: "fadeIn.do",
    0x462F3D: "fadeIn.len", 0x462F51: "fadeIn.zero", 0x462F73: "listAdd.do", 0x462FAE: "listRemove.found",
    0x462FBF: "listRemove.shift", 0x463020: "alloc.single", 0x46305B: "alloc.ageZero", 0x46306B: "alloc.released",
    0x463073: "alloc.free", 0x463082: "alloc.oldest", 0x4630B7: "alloc.older", 0x4630FE: "stealTarget.busy",
    0x463123: "stealTarget.free", 0x463130: "stealTarget.older", 0x46250F: "held.found", 0x462529: "held.shift",
    0x4624CF: "push.both",
    # noteEvent FUN_00463420
    0x46345B: "note.on", 0x463482: "note.poly", 0x4634C9: "note.polyRelease", 0x4634ED: "note.polyAlloc",
    0x463527: "note.polyRetrigger", 0x46356C: "note.mono", 0x4635C4: "note.monoAlloc", 0x46360B: "note.monoSteal",
    0x46363F: "note.monoLegato", 0x46367E: "note.off", 0x4636D2: "note.offRelease", 0x4636E2: "note.offRetarget",
    # midiControl / modSource
    0x46373A: "cc.allNotesOff", 0x46376A: "cc.pitchBend", 0x46378D: "cc.polyPressure",
    0x463833: "src.outOfRange", 0x463841: "src.voice", 0x46384E: "src.voiceNil", 0x463868: "src.bend",
    0x46387A: "src.bendInactive", 0x46387E: "src.controller", 0x463887: "src.0x81",
    # processVoice FUN_00464410 / process FUN_004645c8
    0x46442D: "voice.reload", 0x46443F: "voice.ctrlUpdate", 0x464451: "voice.docStop", 0x464461: "voice.countdown",
    0x464471: "voice.envOff", 0x4644E9: "voice.tailDone", 0x4644B6: "voice.faded", 0x4644CB: "voice.fadeStep",
    0x4644DA: "voice.fadeEnd", 0x4644E0: "voice.fadeKill",
    0x464622: "proc.nullOut", 0x464650: "proc.silent", 0x464656: "proc.silentZero", 0x46467D: "proc.run",
    0x464703: "proc.finished", 0x464727: "proc.kill", 0x464749: "proc.muffle", 0x46475A: "proc.replace",
    0x464782: "proc.accumulate", 0x4647B6: "proc.noVoices",
    # setup
    0x462229: "sr.changed", 0x46224C: "sr.error", 0x462273: "sr.ok", 0x462397: "crate.min", 0x4623AE: "crate.max",
    0x4623F0: "crate.changed", 0x462411: "crate.clampCountdown", 0x4643CC: "muffle.on", 0x464404: "muffle.off",
}

def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def f32_from_bits(b):
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def f32_bits(x):
    return struct.unpack("<I", struct.pack("<f", x))[0]


def s32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def s16(x):
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


class CodeHook:
    """A Unicorn code hook at one address that can be removed and re-installed."""

    def __init__(self, emu, addr, cb, installed=True):
        self.emu, self.addr, self.cb, self.handle = emu, addr, cb, None
        if installed:
            self.install()

    def install(self, flush=True):
        if self.handle is None:
            self.handle = self.emu.uc.hook_add(UC_HOOK_CODE, self.cb, begin=self.addr, end=self.addr)
            if flush:
                # Unicorn does not re-check hooks for already translated blocks.
                self.emu.uc.ctl_flush_tb()

    def remove(self):
        if self.handle is not None:
            self.emu.uc.hook_del(self.handle)
            self.handle = None


def add_code_hook(emu, addr, cb):
    return CodeHook(emu, addr, cb)


def hook_call(emu, addr, on_enter, on_exit, installed=True):
    """Like Win32Emu.trace, but on_enter returning None skips the exit callback (no
    trampoline). Returns the (removable) entry CodeHook."""
    tramp = emu.next_thunk
    emu.next_thunk += 4
    pending = []

    def enter(uc, a, size, _):
        ctx = on_enter(emu)
        if ctx is None:
            return
        esp = uc.reg_read(UC_X86_REG_ESP)
        pending.append((emu.u32(esp), ctx))
        emu.w32(esp, tramp)

    def leave(_emu):
        ret, ctx = pending.pop()
        on_exit(emu, ctx)
        return ret

    emu.trampolines[tramp] = leave
    return CodeHook(emu, addr, enter, installed)


class Call:
    __slots__ = ("method", "voice", "index", "args", "result", "pre", "post", "ret_addr")

    def __init__(self, method, voice, index, args, ret_addr):
        self.method, self.voice, self.index, self.args = method, voice, index, args
        self.result = None
        self.pre = None
        self.post = None
        self.ret_addr = ret_addr

    def __repr__(self):
        return (f"{METHOD_NAMES[self.method]}(v={self.voice}, i={self.index}, args={self.args}, "
                f"res={self.result})")


class Capture:
    def __init__(self, kind, args):
        self.kind = kind
        self.args = args
        self.entry = None
        self.exit = None
        self.calls = []
        self.result = None
        self.cw = None


class Tracer:
    """Hooks the emulated original: records module calls and snapshots for captures."""

    def __init__(self, host, master=None, counters=True):
        """master=None: the plugin is loaded, take its master. Otherwise `master` is a
        one-element list filled later (construction test): objects are mapped lazily."""
        self.h = host
        self.e = host.emu
        self.lazy = master is not None
        self._master_ref = master
        self.master = None if self.lazy else master_ptr(host)
        self.obj = {}
        self.hot_hooks = []
        self.hot = False
        self.active = []          # captures currently recording module calls
        self.counts = {name: 0 for name in MASTER_ROUTINES.values()}
        if not self.lazy:
            self._map_objects()
        self._install_module_hooks()
        if counters:
            self._install_counters()

    # ------------------------------------------------------------ object map
    def _map_objects(self):
        e, m = self.e, self.master
        self.doc = e.u32(m + 0x1000)
        self.editbuf = e.u32(m + 0xFF8)
        self.obj = {self.doc: ("doc", -1, -1)}
        self.lfos = [[0] * 4 for _ in range(16)]
        self.envs = [[0] * 4 for _ in range(16)]
        self.filters = [[0] * 2 for _ in range(16)]
        self.amps = [0] * 16
        self.folls = [0] * 16
        for v in range(16):
            base = m + 4 + v * VOICE_SIZE
            for i in range(4):
                self.lfos[v][i] = e.u32(base + 0x54 + 4 * i)
                self.obj[self.lfos[v][i]] = ("lfo", v, i)
                self.envs[v][i] = e.u32(base + 0x64 + 4 * i)
                self.obj[self.envs[v][i]] = ("env", v, i)
            for i in range(2):
                self.filters[v][i] = e.u32(base + 0x74 + 4 * i)
            self.amps[v] = e.u32(base + 0x7C)
            self.obj[self.amps[v]] = ("amp", v, -1)
            self.folls[v] = e.u32(base + 0x8C)
            self.obj[self.folls[v]] = ("foll", v, -1)

    def voice_index(self, ptr):
        if ptr == 0:
            return -1
        off = ptr - self.master - 4
        assert off % VOICE_SIZE == 0 and 0 <= off // VOICE_SIZE < 16, hex(ptr)
        return off // VOICE_SIZE

    # ------------------------------------------------------------ snapshots
    def program_addrs(self):
        e, m = self.e, self.master
        addrs = {e.u32(self.editbuf + 0xA94)}
        for v in range(16):
            a = e.u32(m + 4 + v * VOICE_SIZE + 0x24)
            if a:
                addrs.add(a)
        addrs.discard(0)
        return addrs

    def snapshot_entry(self):
        e = self.e
        s = {
            "master": e.read(self.master, MASTER_SIZE),
            "filters": [[e.read(self.filters[v][i], FILTER_SIZE) for i in range(2)] for v in range(16)],
            "lfo": [[e.read(self.lfos[v][i] + 0xB0, 0x38) for i in range(4)] for v in range(16)],
            "env_active": [[e.u8(self.envs[v][i] + 0x60) for i in range(4)] for v in range(16)],
            "amp_counter": [e.s32(self.amps[v] + 0x14) for v in range(16)],
            "doc_tail": e.s32(self.doc + 0x209C),
            "programs": {a: e.read(a, PROGRAM_SIZE) for a in self.program_addrs()},
            "current": e.u32(self.editbuf + 0xA94),
            "number": e.u16(self.editbuf + 0xB74),
        }
        return s

    def snapshot_exit(self):
        e = self.e
        return {
            "master": e.read(self.master, MASTER_SIZE),
            "filters": [[e.read(self.filters[v][i], FILTER_SIZE) for i in range(2)] for v in range(16)],
        }

    def begin(self, kind, args):
        cap = Capture(kind, args)
        cap.entry = self.snapshot_entry()
        cap.cw = self.e.uc.reg_read(UC_X86_REG_FPCW)
        self.active.append(cap)
        return cap

    def end(self, cap):
        self.active.remove(cap)
        cap.exit = self.snapshot_exit()
        return cap

    # ------------------------------------------------------------ module hooks
    def _decode(self, method, kind, emu, esp):
        e = emu
        uc = e.uc
        eax, edx, ecx = uc.reg_read(UC_X86_REG_EAX), uc.reg_read(UC_X86_REG_EDX), uc.reg_read(UC_X86_REG_ECX)
        if self.lazy and (not self.obj or eax not in self.obj):
            self.master = self._master_ref[0]
            self._map_objects()

        def arg(i):
            return e.u32(esp + 4 * i)

        def farg(i):
            return f32_from_bits(arg(i))

        voice, index = -1, -1
        if kind == "doc":
            if method == M_DOC_RENDER:
                obj, voice = arg(1), s32(arg(2))
            else:
                obj = eax
                if method not in (M_DOC_RESET, M_DOC_NUMVOICES, M_DOC_SR):
                    voice = s32(edx)
        else:
            obj = eax
            k, voice, index = self.obj[obj]
            assert k == kind, (hex(obj), k, kind)
        if kind == "doc":
            assert obj == self.doc, hex(obj)
        a = ()
        pre = None
        if method == M_DOC_NUMVOICES:
            a = (s32(edx),)
        elif method in (M_DOC_SR, M_LFO_RATE, M_ENV_RATE, M_AMP_SR, M_AMP_SMOOTH, M_FOLL_RATE, M_FOLL_TIME):
            a = (farg(1),)
        elif method == M_DOC_NOTEON:
            a = (s32(ecx), s32(arg(3)), s32(arg(2)), s32(arg(1)))
        elif method == M_DOC_SETKEY:
            a = (s32(ecx),)
            assert arg(1) == 0
        elif method == M_DOC_UPDATE:
            pre = e.read(self.doc + 0x20A0 + voice * 0x80, 0x80)
        elif method == M_LFO_START:
            a = (s32(edx), 1 if ecx & 0xFF else 0)
            pre = e.read(obj + 0xB0, 0x38)
        elif method == M_LFO_TICK:
            pre = e.read(obj + 0xB0, 0x38)
        elif method == M_ENV_START:
            other = arg(1)
            ov = -1
            if other:
                k, ov, oi = self.obj[other]
                assert k == "env" and oi == index
            a = (edx, s32(ecx), s32(arg(4)), 1 if arg(3) & 0xFF else 0, 1 if arg(2) & 0xFF else 0, ov)
        elif method in (M_ENV_RELEASE, M_ENV_TICK):
            a = (1 if edx & 0xFF else 0,)
        elif method in (M_AMP_DRIVE, M_AMP_CURVE, M_FOLL_TARGET, M_FOLL_VALUE):
            a = (s32(edx),)
        elif method == M_AMP_START:
            a = (self.obj[edx][1] if edx else -1,)
            if edx:
                assert self.obj[edx][0] == "amp"
        elif method == M_AMP_LEVEL:
            a = (s32(edx), s32(ecx), farg(2), 1 if arg(1) & 0xFF else 0)
        elif method == M_AMP_PROCESS:
            a = (e.read_st(0), e.f32(edx), e.f32(edx + 4))
        elif method == M_AMP_FADED:
            a = (e.read_st(0), e.f32(edx), e.f32(edx + 4), farg(1))
        return voice, index, a, pre, obj, edx

    def _install_module_hooks(self):
        for addr, (method, kind) in MODULE_ENTRIES.items():
            self._hook_module(addr, method, kind)

    def _hook_module(self, addr, method, kind):
        tracer = self

        def on_enter(emu):
            if not tracer.active:
                return None
            esp = emu.uc.reg_read(UC_X86_REG_ESP)
            ret = emu.u32(esp)
            if not (PLUGCORE[0] <= ret < PLUGCORE[1]):
                return None
            voice, index, a, pre, obj, edx = tracer._decode(method, kind, emu, esp)
            c = Call(method, voice, index, a, ret)
            c.pre = pre
            for cap in tracer.active:
                cap.calls.append(c)
            return (c, obj, edx)

        def on_exit(emu, ctx):
            c, obj, edx = ctx
            if method == M_DOC_RENDER:
                c.result = emu.read_st(0)
            elif method in RESULT_EAX:
                c.result = s32(emu.uc.reg_read(UC_X86_REG_EAX))
            post = {}
            if kind == "env":
                post["env_active"] = emu.u8(obj + 0x60)
            elif kind == "amp":
                post["amp_counter"] = emu.s32(obj + 0x14)
                if method in (M_AMP_PROCESS, M_AMP_FADED):
                    post["acc"] = (emu.f32(edx), emu.f32(edx + 4))
            elif kind == "lfo":
                post["lfo"] = emu.read(obj + 0xB0, 0x38)
            elif kind == "doc":
                post["doc_tail"] = emu.s32(tracer.doc + 0x209C)
            c.post = post

        hk = hook_call(self.e, addr, on_enter, on_exit, installed=addr not in HOT_MODULE_ENTRIES)
        if addr in HOT_MODULE_ENTRIES:
            self.hot_hooks.append(hk)

    def set_hot(self, on):
        """Install/remove the hooks on per-sample routines (render, amp, processVoice)."""
        if on == self.hot:
            return
        self.hot = on
        for hk in self.hot_hooks:
            if on:
                hk.install(flush=False)
            else:
                hk.remove()
        self.e.uc.ctl_flush_tb()

    def _install_counters(self):
        """Count master routine entries, and branch probes executed inside captures (i.e.
        verified by the replay); probes remove themselves after 64 hits."""
        tracer = self
        self.branch_counts = {name: 0 for name in BRANCHES.values()}

        def mk(name):
            def cb(uc, a, size, _):
                tracer.counts[name] += 1
            return cb

        for addr, name in MASTER_ROUTINES.items():
            hk = CodeHook(self.e, addr, mk(name), installed=addr not in HOT_ROUTINES)
            if addr in HOT_ROUTINES:
                self.hot_hooks.append(hk)

        def mkb(name):
            ref = []

            def cb(uc, a, size, _):
                if tracer.active:
                    tracer.branch_counts[name] += 1
                    if tracer.branch_counts[name] >= 64:
                        ref[0].remove()
            return cb, ref

        for addr, name in BRANCHES.items():
            cb, ref = mkb(name)
            ref.append(CodeHook(self.e, addr, cb))

    # ------------------------------------------------------------ unit routine captures
    def hook_unit(self, addr, kind, decide, decode_args, decode_result=None, hot=False):
        """Capture calls of a master routine: decide(emu) -> bool, decode_args(emu) -> args,
        decode_result(emu, cap) at exit. Captures go to self.units[kind]. hot: hooked only
        while set_hot(True)."""
        tracer = self
        self.units = getattr(self, "units", {})
        self.units.setdefault(kind, [])

        def on_enter(emu):
            if not decide(emu):
                return None
            args = decode_args(emu)
            return tracer.begin(kind, args)

        def on_exit(emu, cap):
            tracer.end(cap)
            if decode_result:
                cap.result = decode_result(emu, cap)
            tracer.units[kind].append(cap)

        hk = hook_call(self.e, addr, on_enter, on_exit, installed=not hot)
        if hot:
            self.hot_hooks.append(hk)


# ---------------------------------------------------------------- replay
class Mismatch(Exception):
    pass


CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(ctypes.c_double))


def _layout():
    """(offset, size, name) of the master fields modeled by the C++ port."""
    out = []
    vf = [(0x00, 4, "active"), (0x04, 4, "age"), (0x08, 4, "ctrlCountdown"), (0x0C, 4, "tickParity"),
          (0x10, 4, "slot"), (0x14, 1, "key"), (0x18, 4, "released"), (0x1C, 4, "mono"), (0x20, 4, "noteId"),
          (0x24, 4, "program"), (0x28, 4, "stolenFrom"), (0x2C, 4, "stealFlag"), (0x30, 4, "fading"),
          (0x34, 4, "fadeKill"), (0x38, 4, "fadeRemaining"), (0x3C, 4, "fadeGain"), (0x40, 4, "fadeStep"),
          (0x44, 4, "fadeOutLength"), (0x48, 4, "fadeOutStep"), (0x4C, 4, "fadeInLength"),
          (0x50, 4, "fadeInStep"), (0x80, 4, "dca4Mode"), (0x84, 4, "glideActive"), (0x88, 4, "glideFrom"),
          (0x90, 4, "bend"), (0x94, 4, "bendActive")]
    vf += [(0x98 + 4 * i, 4, f"mod[{i}]") for i in range(16)]
    vf += [(0xD8, 4, "noteLevel"), (0xDC, 4, "panOffset"), (0xE0, 4, "tickCount"), (0xE4, 4, "fe4")]
    for v in range(16):
        for off, size, name in vf:
            out.append((4 + v * VOICE_SIZE + off, size, f"voice[{v}].{name}"))
    out.append((0xE84, 4, "numVoices"))
    for i in range(16):
        out.append((0xE88 + 8 * i, 4, f"list[{i}].voice"))
        out.append((0xE8C + 8 * i, 4, f"list[{i}].slot"))
    out.append((0xF08, 4, "listCount"))
    out += [(0xF0C + 4 * i, 4, f"slotMap[{i}]") for i in range(16)]
    out += [(0xF4C, 4, "numPlay"), (0xF50, 4, "numFade")]
    out += [(0xF54 + 2 * i, 2, f"noteStacks[{i}]") for i in range(8)]
    names = {0xF64: "volume", 0xF68: "muffleVoice", 0xF6C: "muffleAge", 0xF70: "sampleRate",
             0xF74: "invSampleRate", 0xF78: "sampleRateMs", 0xF7C: "controlRate", 0xF80: "controlStep",
             0xF84: "initialDelay", 0xF88: "error", 0xF8C: "lockCount", 0xF94: "muffleOn", 0xF98: "muffleB0",
             0xF9C: "muffleB1", 0xFA0: "muffleB2", 0xFA4: "muffleA1", 0xFA8: "muffleA2",
             0xFE4: "ovrVoiceSteal", 0xFE8: "ovrDca4", 0xFEC: "ovrMuffle", 0xFF0: "ovrOscDca", 0xFF4: "ovrDcb",
             0x11004: "tickToggle", 0x11008: "smoothB", 0x1100C: "smoothA"}
    out += [(o, 4, n) for o, n in names.items()]
    out += [(0xFAC + 4 * i, 4, f"muffleState[{i}]") for i in range(8)]
    out.append((0x1004, 0x10000, "filterTable"))
    out += [(0x11010 + 16 * i, 2, f"ctrl[{i - 1}]") for i in range(131)]
    out += [(0x11840 + 16 * i, 2, f"polyPressure[{i}]") for i in range(128)]
    out += [(0x12040, 2, "pitchBend"), (0x12050, 1, "inProcess")]
    return out


LAYOUT = _layout()
MODELED = np.zeros(MASTER_SIZE, dtype=bool)
for _o, _s, _n in LAYOUT:
    MODELED[_o:_o + _s] = True
FILTER_FIELDS = [(o, f"+{o:#04x}") for o in range(0x04, FILTER_SIZE, 4) if o not in (0x20, 0x24)]

LFO_FIELDS = ["freq", "p04", "p08", "p0c", "p10", "wave", "level", "levelAlt", "depthMod", "p24", "p28", "p2c",
              "levelSel", "p34"]
# Words of the Cdoc voice block written by the master before docUpdate.
DOC_MASTER_WORDS = [o for i in range(3) for o in range(0x18 * i, 0x18 * i + 0x18, 4)] + \
    [0x64, 0x68, 0x6C, 0x70, 0x78, 0x7C]


class Replayer:
    def __init__(self, tracer):
        self.t = tracer
        self.L = L = lib()
        vp, i32, f32t, u32 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_float, ctypes.c_uint32
        cp = ctypes.c_char_p
        sig = {
            "sq8l_master_new": ([CB_TYPE, i32, i32, ctypes.POINTER(i32)], vp),
            "sq8l_master_free": ([vp], None),
            "sq8l_master_set_guest_base": ([vp, u32], None),
            "sq8l_master_set_program": ([vp, u32, cp], None),
            "sq8l_master_set_current": ([vp, u32, i32], None),
            "sq8l_master_load": ([vp, cp], None),
            "sq8l_master_save": ([vp, cp], None),
            "sq8l_master_load_filter": ([vp, i32, i32, cp], None),
            "sq8l_master_save_filter": ([vp, i32, i32, cp], None),
            "sq8l_master_lfo_params": ([vp, i32, i32], vp),
            "sq8l_master_doc_params": ([vp, i32], vp),
            "sq8l_master_note_on": ([vp, i32, i32, i32], None),
            "sq8l_master_control": ([vp, i32, i32, i32, i32, i32], None),
            "sq8l_master_reset": ([vp, i32], None),
            "sq8l_master_control_update": ([vp, i32, i32], None),
            "sq8l_master_process_voice": ([vp, i32, ctypes.POINTER(f32t), i32], i32),
            "sq8l_master_set_sample_rate": ([vp, i32, i32], i32),
            "sq8l_master_set_sample_rate_f": ([vp, f32t, i32], i32),
            "sq8l_master_set_control_rate": ([vp, f32t, i32], None),
            "sq8l_master_set_voices": ([vp, i32, i32, i32], None),
            "sq8l_master_edit_event": ([vp, i32, u32, i32], None),
            "sq8l_master_load_overrides": ([vp, ctypes.POINTER(i32)], None),
            "sq8l_master_active_count": ([vp], i32),
            "sq8l_master_process_events": ([vp, vp, i32], None),
            "sq8l_master_process": ([vp, ctypes.POINTER(f32t), ctypes.POINTER(f32t), i32, i32], None),
        }
        for name, (args, res) in sig.items():
            fn = getattr(L, name)
            fn.argtypes = args
            fn.restype = res
        self._cb = CB_TYPE(self._callback)
        self.t_master_for_ctor = 0
        self.box = L.sq8l_master_new(self._cb, 44100, 0, None)
        L.sq8l_master_set_guest_base(self.box, tracer.master)
        self.cur = None

    # ------------------------------------------------------------ module call checking
    def _fail(self, msg):
        st = self.cur
        st["errors"].append(msg)

    def _callback(self, method, voice, index, io):
        try:
            self._handle(method, voice, index, io)
        except Exception as ex:  # never let exceptions escape into C
            self._fail(f"callback exception {ex!r}")

    def _handle(self, method, voice, index, io):
        st = self.cur
        if method == M_ENV_ACTIVE:
            io[0] = st["env_active"][voice][index]
            return
        if method == M_AMP_COUNTER:
            io[0] = st["amp_counter"][voice]
            return
        if method == M_DOC_TAIL:
            io[0] = st["doc_tail"]
            return
        calls = st["calls"]
        pos = st["pos"]
        if pos >= len(calls):
            self._fail(f"extra call #{pos} {METHOD_NAMES[method]}(v={voice}, i={index})")
            io[0] = 0.0
            return
        c = calls[pos]
        st["pos"] = pos + 1
        nargs = len(c.args)
        got = [io[k] for k in range(max(nargs, 1))]
        if (c.method, c.voice, c.index) != (method, voice, index):
            self._fail(f"call #{pos}: ours {METHOD_NAMES[method]}(v={voice}, i={index}) "
                       f"orig {c!r}")
        else:
            bad = []
            fl = FLOAT_ARGS.get(method, ())
            for k in range(nargs):
                want = c.args[k]
                have = got[k]
                if isinstance(want, Fraction):
                    if Fraction(have) != want:
                        bad.append((k, have, float(want)))
                elif k in fl:
                    if f32_bits(have) != f32_bits(want):
                        bad.append((k, have, want))
                elif int(have) != want:
                    bad.append((k, int(have), want))
            if bad:
                self._fail(f"call #{pos} {METHOD_NAMES[method]}(v={voice}, i={index}) arg mismatch "
                           f"(k, ours, orig): {bad}")
            if c.pre is not None:
                self._check_pre(pos, c, voice, index)
        # results
        if c.result is not None:
            io[0] = float(c.result) if isinstance(c.result, Fraction) else c.result
        post = c.post or {}
        if "acc" in post:
            io[1], io[2] = post["acc"]
        if "env_active" in post:
            st["env_active"][c.voice][c.index] = post["env_active"]
        if "amp_counter" in post:
            st["amp_counter"][c.voice] = post["amp_counter"]
        if "doc_tail" in post:
            st["doc_tail"] = post["doc_tail"]
        if "lfo" in post:
            ctypes.memmove(self.L.sq8l_master_lfo_params(self.box, c.voice, c.index), post["lfo"], 0x38)

    def _check_pre(self, pos, c, voice, index):
        if c.method in (M_LFO_START, M_LFO_TICK):
            ours = ctypes.string_at(self.L.sq8l_master_lfo_params(self.box, voice, index), 0x38)
            if ours != c.pre:
                o = struct.unpack("<14i", ours)
                w = struct.unpack("<14i", c.pre)
                d = [(LFO_FIELDS[k], o[k], w[k]) for k in range(14) if o[k] != w[k]]
                self._fail(f"call #{pos} {METHOD_NAMES[c.method]}(v={voice}, i={index}) LFO params "
                           f"(field, ours, orig): {d}")
        elif c.method == M_DOC_UPDATE:
            ours = ctypes.string_at(self.L.sq8l_master_doc_params(self.box, voice), 0x80)
            d = [(hex(o), struct.unpack_from("<i", ours, o)[0], struct.unpack_from("<i", c.pre, o)[0])
                 for o in DOC_MASTER_WORDS if ours[o:o + 4] != c.pre[o:o + 4]]
            if d:
                self._fail(f"call #{pos} doc_update(v={voice}) params (offset, ours, orig): {d}")

    # ------------------------------------------------------------ state
    def load(self, cap):
        L, box, s = self.L, self.box, cap.entry
        for a, b in s["programs"].items():
            L.sq8l_master_set_program(box, a, b)
        L.sq8l_master_set_current(box, s["current"], s["number"])
        L.sq8l_master_load(box, s["master"])
        for v in range(16):
            for i in range(2):
                L.sq8l_master_load_filter(box, v, i, s["filters"][v][i])
            for i in range(4):
                ctypes.memmove(L.sq8l_master_lfo_params(box, v, i), s["lfo"][v][i], 0x38)
        self.cur = {
            "calls": cap.calls, "pos": 0, "errors": [],
            "env_active": [list(x) for x in s["env_active"]],
            "amp_counter": list(s["amp_counter"]),
            "doc_tail": s["doc_tail"],
        }

    def construct(self, cap, sample_rate, overrides):
        """Construct a new C++ master with its module calls checked against `cap`."""
        self.cur = {"calls": cap.calls, "pos": 0, "errors": [],
                    "env_active": [[0] * 4 for _ in range(16)], "amp_counter": [0] * 16, "doc_tail": 0}
        box = self.L.sq8l_master_new(self._cb, sample_rate, 1, (ctypes.c_int32 * 5)(*overrides))
        self.L.sq8l_master_set_guest_base(box, self.t_master_for_ctor)
        return box

    def finish(self, cap, box=None):
        """Compare the C++ state with the exit snapshot; returns a list of problems."""
        L, box = self.L, box or self.box
        st = self.cur
        errs = list(st["errors"])
        if st["pos"] != len(cap.calls):
            errs.append(f"missing calls: ours made {st['pos']} of {len(cap.calls)}; next orig "
                        f"{cap.calls[st['pos']] if st['pos'] < len(cap.calls) else None!r}")
        exit_master = cap.exit["master"]
        buf = ctypes.create_string_buffer(bytes(exit_master), MASTER_SIZE)
        L.sq8l_master_save(box, buf)
        ours = buf.raw
        if ours != exit_master:
            bad = [n for o, sz, n in LAYOUT if ours[o:o + sz] != exit_master[o:o + sz]]
            errs.append(f"master state differs: {bad[:12]}{' ...' if len(bad) > 12 else ''}")
        if cap.entry is not None:
            changed = np.frombuffer(cap.entry["master"], np.uint8) != np.frombuffer(exit_master, np.uint8)
            unmodeled = np.nonzero(changed & ~MODELED)[0]
            if len(unmodeled):
                errs.append(f"unmodeled master bytes changed: {[hex(o) for o in unmodeled[:8]]}")
        fbuf = ctypes.create_string_buffer(FILTER_SIZE)
        for v in range(16):
            for i in range(2):
                want = cap.exit["filters"][v][i]
                fbuf.raw = want
                L.sq8l_master_save_filter(box, v, i, fbuf)
                if fbuf.raw != want:
                    d = [n for o, n in FILTER_FIELDS if fbuf.raw[o:o + 4] != want[o:o + 4]]
                    errs.append(f"filter[{v}][{i}] differs: {d}")
        return errs

    def rz(self, cap):
        return 1 if (cap.cw & 0xC00) == 0xC00 else 0
