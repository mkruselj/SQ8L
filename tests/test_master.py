"""Differential test: CplugMaster (original, emulated) vs sq8l::Master (C++).

The original plays scripted scenarios (poly/mono/legato/glide programs, voice
stealing, retrigger, pitch bend modes, controllers, sustain, all-notes-off, MIDI
reset, panic, program changes, sample-rate and control-rate changes, voice counts,
ini overrides, patched program parameters, accumulating and silent process paths).
Master routines are captured (see test_master_capture.py) and replayed on the C++
master with a forwarding VoiceModules: the sequence of module calls with their
arguments, the resulting master/voice/filter state and the outputs must be
bit-identical. Whole processEvents+process blocks are captured as well.

Usage: test_master.py [--quick]"""
import ctypes
import random
import struct
import sys
import time

from harness import effSetProgram, started_host
from unicorn.x86_const import UC_X86_REG_EIP, UC_X86_REG_ESP

from test_master_capture import BRANCHES, Replayer, Tracer, add_code_hook, f32_from_bits, s16, s32
from vsthost import effSetSampleRate

QUICK = "--quick" in sys.argv

# Master routine addresses called directly by the scenarios.
RESET = 0x462174
SET_CONTROL_RATE = 0x462380
SET_VOICES = 0x463358
EDIT_EVENT = 0x463230
ACTIVE_COUNT = 0x463328
LOAD_OVERRIDES = 0x4620E4
PROCESS = 0x4645C8
MESSAGE_BOX = 0x44F474
CONFIG_PTR = 0x489BF8  # global holding the [synth] config object (via 0x4c31bc)
HOST_CW = 0x027F


class Raw(ctypes.Structure):
    _fields_ = [("deltaFrames", ctypes.c_int32), ("data", ctypes.c_uint8 * 3), ("noteOffVelocity", ctypes.c_uint8)]


class Sampler:
    """Capture the first `first` calls, then one every `every`."""

    def __init__(self, first, every):
        self.first, self.every, self.n = first, every, 0

    def __call__(self, emu=None):
        self.n += 1
        return self.n <= self.first or self.n % self.every == 0


ALL = (10 ** 9, 1)


class Suite:
    def __init__(self):
        self.h = started_host()
        self.e = self.h.emu
        self.t = Tracer(self.h)
        self.m = self.t.master
        self.blocks = []
        self.rng = random.Random(4321)
        self._install_units()
        self._skip_message_box()

    def _skip_message_box(self):
        """FUN_0044f474 (the plugin's VCL message box, shown by setSampleRate for sr < 40000)
        has no DSP effect: return immediately (register args + one stack arg, RET 4)."""
        e = self.e

        def cb(uc, a, size, _):
            esp = uc.reg_read(UC_X86_REG_ESP)
            uc.reg_write(UC_X86_REG_EIP, e.u32(esp))
            uc.reg_write(UC_X86_REG_ESP, esp + 8)

        add_code_hook(e, MESSAGE_BOX, cb)

    # ------------------------------------------------------------ unit routine hooks
    def _install_units(self):
        t, e = self.t, self.e

        def regs():
            return e.regs()

        def arg(i):
            return e.u32(regs()["esp"] + 4 * i)

        sampling = {"ctrlupd": (300, 23), "voice": (200, 151)}

        def smp(kind):
            return Sampler(*sampling.get(kind, ALL))

        t.hook_unit(0x463388, "note", smp("note"), lambda emu: (regs()["ecx"] & 0xFF, arg(1) & 0xFF))
        t.hook_unit(0x463714, "control", smp("control"),
                    lambda emu: (regs()["edx"] & 0xFF, regs()["ecx"] & 0xFF, s16(arg(1)), s16(arg(2))))
        t.hook_unit(RESET, "reset", smp("reset"), lambda emu: ())
        t.hook_unit(0x463890, "ctrlupd", smp("ctrlupd"), lambda emu: (t.voice_index(regs()["edx"]),))

        def voice_args(emu):
            r = regs()
            return (t.voice_index(r["edx"]), r["ecx"], e.f32(r["ecx"]), e.f32(r["ecx"] + 4))

        def voice_result(emu, cap):
            acc = cap.args[1]
            return (e.regs()["eax"] & 0xFF, e.f32(acc), e.f32(acc + 4))

        t.hook_unit(0x464410, "voice", smp("voice"), voice_args, voice_result, hot=True)
        t.hook_unit(0x462214, "sr", smp("sr"), lambda emu: (s32(regs()["edx"]),),
                    lambda emu, cap: s32(e.regs()["eax"]))
        t.hook_unit(SET_CONTROL_RATE, "crate", smp("crate"), lambda emu: (f32_from_bits(arg(1)),))
        t.hook_unit(SET_VOICES, "setvoices", smp("setvoices"), lambda emu: (s32(regs()["edx"]), s32(regs()["ecx"])))
        t.hook_unit(EDIT_EVENT, "edit", smp("edit"), lambda emu: (s32(regs()["ecx"]), arg(1)))
        t.hook_unit(ACTIVE_COUNT, "count", smp("count"), lambda emu: (), lambda emu, cap: s32(e.regs()["eax"]))

        def overrides_args(emu):
            cfg = e.u32(CONFIG_PTR)
            return tuple(e.s32(cfg + 0x38 + 4 * i) for i in range(5))

        t.hook_unit(LOAD_OVERRIDES, "overrides", smp("overrides"), overrides_args)

    # ------------------------------------------------------------ driving the original
    def block(self, events=(), n=64, capture=True):
        self.t.set_hot(capture)
        cap = self.t.begin("block", (list(events), n)) if capture else None
        if events:
            self.h.send_midi(list(events))
        left, right = self.h.process(n)
        if cap:
            self.t.end(cap)
            cap.result = (left, right)
            self.blocks.append(cap)
        return left, right

    def block_direct(self, events=(), n=64, replacing=False, null_out=False):
        """FUN_004645c8 called directly (accumulating mode / nil outputs) on prefilled buffers."""
        e = self.e
        out_l, out_r = self.h.outbufs
        init_l = [self.rng.uniform(-1, 1) for _ in range(n)]
        init_r = [self.rng.uniform(-1, 1) for _ in range(n)]
        e.write(out_l, struct.pack(f"<{n}f", *init_l))
        e.write(out_r, struct.pack(f"<{n}f", *init_r))
        init_l = list(struct.unpack(f"<{n}f", e.read(out_l, 4 * n)))
        init_r = list(struct.unpack(f"<{n}f", e.read(out_r, 4 * n)))
        self.t.set_hot(True)
        cap = self.t.begin("blockdirect", (list(events), n, init_l, init_r, replacing, null_out))
        if events:
            self.h.send_midi(list(events))
        e.call_fpu(PROCESS, eax=self.m, edx=1 if replacing else 0, ecx=0 if null_out else out_l,
                   push=(out_r, n), fpcw=HOST_CW)
        self.t.end(cap)
        cap.result = (struct.unpack(f"<{n}f", e.read(out_l, 4 * n)), struct.unpack(f"<{n}f", e.read(out_r, 4 * n)))
        self.blocks.append(cap)

    def run(self, seconds, events=(), n=64, capture_every=8):
        """Run `seconds` of audio; events = [(time_s, bytes)]; captures every block with
        events and every `capture_every`-th block."""
        sr = self.e.s32(self.m + 0xF70)
        total = int(seconds * sr)
        evs = sorted(((int(ts * sr), i), bytes(b)) for i, (ts, b) in enumerate(events))
        pos, k = 0, 0
        while pos < total:
            cnt = min(n, total - pos)
            these = [(t - pos, b) for (t, _), b in evs if pos <= t < pos + cnt]
            self.block(these, cnt, capture=bool(these) or bool(capture_every and k % capture_every == 0))
            pos += cnt
            k += 1

    def program(self, p):
        self.t.set_hot(True)
        self.h.dispatch(effSetProgram, value=p)

    def current_program(self):
        return self.e.u32(self.e.u32(self.m + 0xFF8) + 0xA94)

    def patch(self, offset, value):
        """Live edit of a byte of the current program."""
        self.e.write(self.current_program() + offset, bytes([value & 0xFF]))

    def patch16(self, offset, value):
        self.e.write(self.current_program() + offset, struct.pack("<h", value))

    def poke(self, offset, value):
        self.e.w32(self.m + offset, value)

    def call(self, addr, edx=None, ecx=None, push=()):
        self.t.set_hot(True)
        return self.e.call_fpu(addr, eax=self.m, edx=edx, ecx=ecx, push=push, fpcw=HOST_CW)

    def set_sample_rate(self, sr):
        self.t.set_hot(True)
        self.h.sr = sr
        self.h.dispatch(effSetSampleRate, opt=float(sr))


# ---------------------------------------------------------------- MIDI helpers
def on(k, v=100, ch=0):
    return bytes([0x90 | ch, k, v])


def off(k, ch=0):
    return bytes([0x80 | ch, k, 0])


def cc(n, v, ch=0):
    return bytes([0xB0 | ch, n, v])


def bend(v):
    v += 0x2000
    return bytes([0xE0, v & 0x7F, (v >> 7) & 0x7F])


def chord(keys, t_on=0.0, dur=0.3, dt=0.02, vel=100):
    ev = []
    for i, k in enumerate(keys):
        ev.append((t_on + i * dt, on(k, vel)))
        ev.append((t_on + i * dt + dur, off(k)))
    return ev


# ---------------------------------------------------------------- scenarios
def scenarios(s):
    def step(name, fn):
        t0 = time.time()
        fn()
        print(f"  {name}: {time.time() - t0:.1f}s")

    poly_programs = [394, 387, 301, 337] if QUICK else [394, 387, 396, 338, 343, 256, 14, 63, 18, 40, 77, 130]

    def poly():
        for p in poly_programs:
            s.program(p)
            s.run(0.6, chord([48, 60, 67, 72], dur=0.3, vel=90) + [(0.45, on(60, 1)), (0.5, off(60))])

    def stealing():
        s.program(394)
        keys = [36 + 3 * i for i in range(14)]
        s.run(1.0, [(0.02 * i, on(k, 60 + i * 4)) for i, k in enumerate(keys)] +
              [(0.5 + 0.01 * i, off(k)) for i, k in enumerate(keys)])
        s.poke(0xFE4, 1)  # voiceStealMode override: mode -1 -> no stealing fades
        s.run(0.8, [(0.015 * i, on(k)) for i, k in enumerate(keys)] + [(0.4, off(k)) for k in keys])
        s.poke(0xFE4, 0)  # follow the program (0x195)
        s.patch(0x195, 0xFF)
        s.run(0.4, [(0.01 * i, on(k)) for i, k in enumerate(keys[:10])] + [(0.2, off(k)) for k in keys[:10]])
        s.patch(0x195, 0)
        s.run(0.5, [(0.01 * i, on(k)) for i, k in enumerate(keys[:12])] + [(0.25, off(k)) for k in keys[:12]])
        s.poke(0xFE4, 2)
        # fast repeated notes on few keys while others hold
        ev = [(0.0, on(40)), (0.0, on(47))]
        for i in range(24):
            ev += [(0.02 + 0.03 * i, on(60 + (i % 5))), (0.035 + 0.03 * i, off(60 + (i % 5)))]
        s.run(1.2, ev + [(1.0, off(40)), (1.0, off(47))])

    def retrigger():
        for p, envrestart in ((40, 0), (40, 1), (8, 0), (18, 1)):
            s.program(p)
            s.patch(0x174, 1)
            s.patch(0x175, envrestart)
            ev = [(0.0, on(60)), (0.05, on(60, 70)), (0.1, on(60, 120)), (0.15, off(60)), (0.2, on(60)),
                  (0.25, on(64)), (0.3, on(64)), (0.45, off(60)), (0.5, off(64))]
            s.run(0.7, ev)
        s.program(40)
        s.patch(0x174, 1)
        s.patch(0x9B, 0xFF)  # negative fade-in index
        s.run(0.4, [(0.0, on(62)), (0.05, on(62)), (0.2, off(62))])
        s.patch(0x9B, 2)
        s.run(0.4, [(0.0, on(62)), (0.05, on(62)), (0.2, off(62))])

    def mono():
        for p, glide in ((301, None), (301, 0), (4, None), (8, None), (9, 12)):
            s.program(p)
            s.patch(0x172, 1)
            if glide is not None:
                s.patch(0x173, glide)
            ev = [(0.0, on(48)), (0.1, on(55)), (0.2, on(60)), (0.3, off(60)), (0.4, off(55)), (0.5, on(52)),
                  (0.6, off(52)), (0.65, off(48)),
                  # released voice + new note, held stack overflow (>4 held keys)
                  (0.75, on(50)), (0.8, off(50)), (0.82, on(53)), (0.85, on(57)), (0.86, on(59)), (0.87, on(61)),
                  (0.88, on(62)), (0.9, off(62)), (0.92, off(57)), (0.94, off(61)), (0.96, off(53)), (0.98, off(59))]
            s.run(1.3, ev)
        s.program(4)
        s.patch(0x172, 0)  # poly glide
        s.patch(0x173, 20)
        s.run(0.8, [(0.0, on(48)), (0.1, on(60)), (0.2, on(36)), (0.3, off(48)), (0.35, off(60)), (0.4, off(36)),
                    (0.5, on(36)), (0.6, off(36))])

    def bends():
        s.program(394)
        for mode in range(8):
            s.patch(0x191, mode)
            s.patch(0x190, [2, 12, 24, -12, 2, 7, 0, 127][mode])
            ev = [(0.0, on(48)), (0.02, on(55)), (0.05, bend(4000)), (0.1, bend(-8192)), (0.15, off(55)),
                  (0.2, bend(8191)), (0.25, on(60)), (0.3, bend(-1234)), (0.35, off(48)), (0.4, bend(0)),
                  (0.45, off(60))]
            s.run(0.6, ev)
        s.patch(0x191, 0)
        s.patch(0x190, 2)

    def controllers():
        s.program(387)
        ev = [(0.0, on(48)), (0.0, on(52)), (0.01, on(55))]
        for i in range(20):
            tt = 0.02 + 0.02 * i
            ev += [(tt, cc(1, 6 * i)), (tt, cc(7, 127 - 5 * i)), (tt, cc(8, 3 * i)), (tt, cc(11, 6 * i)),
                   (tt, cc(74, 127 - 6 * i)), (tt, bytes([0xD0, 6 * i])), (tt, bytes([0xA0, 52, 5 * i])),
                   (tt, cc(20 + i, i))]
        ev += [(0.5, cc(64, 127)), (0.55, off(48)), (0.55, off(52)), (0.6, on(60)), (0.65, off(60)),
               (0.8, cc(64, 0)), (0.85, off(55)), (0.9, cc(7, 100)), (0.9, cc(1, 0))]
        s.run(1.1, ev)
        # all notes off, channels, MIDI reset / stop
        s.run(0.5, [(0.0, on(60)), (0.0, on(64)), (0.1, cc(123, 0)), (0.15, on(67, 90)), (0.2, cc(64, 1, ch=5)),
                    (0.25, off(67)), (0.3, cc(64, 0)), (0.35, on(48, 80))])
        s.run(0.4, [(0.0, on(50)), (0.05, bytes([0xFF, 0, 0])), (0.1, on(52)), (0.15, bytes([0xFC, 0, 0])),
                    (0.2, on(53)), (0.3, off(53))])
        s.call(RESET)  # panic button
        s.run(0.1, [(0.0, on(60)), (0.05, off(60))])
        s.call(RESET)

    def programs():
        s.program(394)
        s.run(0.2, [(0.0, on(48)), (0.0, on(55))])
        for p in (387, 396, 301, 14, 394):
            s.program(p)
            s.run(0.15, [(0.0, on(60 + p % 7)), (0.1, off(60 + p % 7))])
        s.run(0.3, [(0.0, off(48)), (0.0, off(55))])
        prog = s.current_program()
        for code in (1, 2, 3, 0, 4, 5):
            s.run(0.05, [(0.0, on(62))])
            s.call(EDIT_EVENT, ecx=code, push=(prog,))
            s.run(0.05, [(0.0, off(62))])

    def muffle_and_overrides():
        for p in (14, 394, 18, 387):  # muffle on / off programs alternately
            s.program(p)
            s.run(0.3, chord([48, 55, 62], dur=0.2))
        for off_ in (0xFE8, 0xFEC, 0xFF0, 0xFF4):
            for val in (1, 2, 3, 4):
                s.poke(off_, val)
                s.run(0.15, [(0.0, on(57)), (0.1, off(57))])
            s.poke(off_, 0)
        cfg = s.e.u32(CONFIG_PTR)
        saved = s.e.read(cfg + 0x38, 20)
        for vals in ((2, 1, 2, 3, 4), (0, 0, 0, 0, 0), (1, 3, 1, 2, 2)):
            s.e.write(cfg + 0x38, struct.pack("<5i", *vals))
            s.call(LOAD_OVERRIDES)
            s.run(0.15, [(0.0, on(57)), (0.1, off(57))])
        s.e.write(cfg + 0x38, saved)
        s.call(LOAD_OVERRIDES)
        # doc params from program bits
        s.program(394)
        for b192, b196, b170, b171 in ((0x1F, 3, 1, 1), (0x09, 1, 0, 0x7F), (0x11, 2, 1, 0x80), (0x19, 0, 0, 2)):
            s.patch(0x192, b192)
            s.patch(0x196, b196)
            s.patch(0x170, b170)
            s.patch(0x171, b171)
            s.patch(0x13B, s.rng.randrange(256))
            s.run(0.15, [(0.0, on(59)), (0.1, off(59))])

    def modulation():
        s.program(394)
        base = s.current_program()
        orig = s.e.read(base, 0x21C)
        srcs = [-1, 0, 3, 4, 8, 9, 10, 11, 12, 13, 15, 16, 17, 0x17, 0x5A, 0x81, 0x90, 0x91, 0x95, -5]
        rng = s.rng
        for it in range(6 if QUICK else 24):
            for i in range(4):  # LFO modulation modes and sources
                b = 0xA2 + 16 * i
                s.patch(b + 0xD, rng.randrange(64))
                s.patch(b + 0xE, rng.randrange(128))
                s.patch(b + 0xF, rng.randrange(64))
                s.patch(b + 0x1, rng.choice([0xFF, 0, 5, 63]))
                s.patch(b + 0x9, rng.randrange(-63, 64))
                s.patch(b + 0xC, rng.randrange(-63, 64))
                s.patch16(b + 0xA, rng.choice(srcs))
                s.patch16(b + 0x7, rng.choice(srcs))
            for i in range(3):  # matrices
                b = 0xE2 + 12 * i
                for k in range(4):
                    s.patch16(b + 3 * k, rng.choice(srcs))
                    s.patch(b + 3 * k + 2, rng.randrange(-63, 64))
            for o in (0x22, 0x3A, 0x52):  # oscillator modulation
                for k in (5, 8, 0x10, 0x13):
                    s.patch16(o + k, rng.choice(srcs))
                for k in (7, 0xA, 0x12, 0x15):
                    s.patch(o + k, rng.randrange(-63, 64))
            for k in (4, 7):  # filter / amp modulation
                s.patch16(0x106 + k, rng.choice(srcs))
                s.patch16(0x132 + k - 2, rng.choice(srcs))
            s.patch(0x108, rng.randrange(-63, 64))
            s.run(0.25, [(0.0, on(rng.randrange(30, 90), rng.randrange(1, 128))), (0.02, bend(rng.randrange(-8192, 8192))),
                         (0.03, cc(1, rng.randrange(128))), (0.04, bytes([0xD0, rng.randrange(128)])),
                         (0.05, on(rng.randrange(30, 90))), (0.2, cc(123, 0))], capture_every=2)
        s.e.write(base, orig)

    def tails():
        for p in (394, 63, 343):
            s.program(p)
            s.run(2.5 if not QUICK else 1.0, chord([45, 57], dur=0.1), capture_every=16)

    def rates_and_voices():
        s.program(394)
        for sr in (48000, 96000, 32000, 44100):
            s.set_sample_rate(sr)
            s.run(0.2, [(0.0, on(60)), (0.01, on(67)), (0.1, off(60)), (0.15, off(67))])
        s.run(0.05, [(0.0, on(60))])
        s.set_sample_rate(48000)  # with a voice sounding
        s.set_sample_rate(44100)
        for rate in (0.5, 100.0, 1e7, 83.592575):
            s.run(0.05, [(0.0, on(64))])
            s.call(SET_CONTROL_RATE, push=(struct.unpack("<I", struct.pack("<f", rate))[0],))
            s.run(0.1, [(0.05, off(64))])
        for play, fade in ((6, 4), (3, 2), (1, 1), (12, 4), (0, 0), (8, 8)):
            s.call(SET_VOICES, edx=play, ecx=fade)
            s.run(0.3, chord([40 + 2 * i for i in range(7)], dur=0.1, dt=0.01))
            s.call(ACTIVE_COUNT)

    def direct_process():
        s.program(394)
        s.block_direct([(0, on(60)), (10, on(64))], 64)
        for _ in range(3):
            s.block_direct([], 64)
        s.call(ACTIVE_COUNT)
        s.block_direct([(5, off(60))], 32, replacing=True)
        s.block_direct([(3, on(70))], 64, null_out=True)
        s.poke(0xF8C, 1)  # lock count: silent output, events still dispatched
        s.block([(2, on(72)), (40, off(64))], 64)
        s.block_direct([(2, off(72))], 64)
        s.poke(0xF8C, 0)
        s.run(0.3, [(0.0, off(70))])

    def corner_cases():
        # envelope full-cycle flag, program-wide and per envelope
        for p in (7, 13):
            s.program(p)
            s.run(0.3, chord([50, 57], dur=0.1))
        s.program(394)
        s.patch(0x177, 1)
        s.run(0.3, chord([50, 57], dur=0.1))
        s.patch(0x177, 0)
        # all fade slots busy: bursts of simultaneous notes over 8 held voices
        held = [30 + i for i in range(8)]
        burst = [70 + i for i in range(12)]
        s.run(0.3, [(0.0, on(k)) for k in held] + [(0.1, on(k)) for k in burst] + [(0.1005, on(k + 13)) for k in burst]
              + [(0.2, off(k)) for k in held + burst] + [(0.2, off(k + 13)) for k in burst])
        # matrix with zero sum and zero scale ((CC1=0 - 127) * 63 + 8001 = 0)
        orig = s.e.read(s.current_program(), 0x21C)
        for i in range(3):
            b = 0xE2 + 12 * i
            for k in range(3):
                s.patch16(b + 3 * k, -1)
            s.patch16(b + 9, 17)
            s.patch(b + 0xB, 63)
        # pitch-bend source on a voice whose bend is off (mode 1, released voice)
        s.patch(0x191, 1)
        s.patch16(0xA2 + 0xA, 0x91)
        s.patch(0xA2 + 0xC, 40)
        s.run(0.4, [(0.0, cc(1, 0)), (0.0, on(60)), (0.05, bend(3000)), (0.1, off(60)), (0.3, bend(-3000))],
              capture_every=1)
        s.e.write(s.current_program(), orig)
        # an active voice whose age counter reached 0 (after 2^32 samples)
        s.run(0.05, [(0.0, on(48))])
        for v in range(16):
            base = s.m + 4 + v * 0xE8
            if s.e.s32(base) != 0:
                s.e.w32(base + 4, 0)
        s.run(0.1, [(0.0, on(52)), (0.05, off(52)), (0.06, off(48))])

    for name, fn in [("poly", poly), ("stealing", stealing), ("retrigger", retrigger), ("mono/legato/glide", mono),
                     ("pitch bend modes", bends), ("controllers", controllers), ("program changes", programs),
                     ("muffle/overrides", muffle_and_overrides), ("modulation", modulation), ("tails", tails),
                     ("rates/voices", rates_and_voices), ("direct process", direct_process),
                     ("corner cases", corner_cases)]:
        step(name, fn)


# ---------------------------------------------------------------- replay
def replay_all(s):
    r = Replayer(s.t)
    L = r.L
    box = r.box
    ok = True
    results = {}

    def check(kind, cap, run):
        nonlocal ok
        r.load(cap)
        errs = run(cap) or []
        errs += r.finish(cap)
        res = results.setdefault(kind, [0, 0, 0])
        res[0] += 1
        res[2] += len(cap.calls)
        if errs:
            res[1] += 1
            ok = False
            if res[1] <= 3:
                print(f"  {kind} #{res[0] - 1} args={str(cap.args)[:100]}: {len(errs)} problems")
                for msg in errs[:6]:
                    print("     ", msg[:400])

    units = s.t.units
    for cap in units.get("note", []):
        check("note", cap, lambda c: L.sq8l_master_note_on(box, c.args[0], c.args[1], r.rz(c)))
    for cap in units.get("control", []):
        check("control", cap, lambda c: L.sq8l_master_control(box, *c.args, r.rz(c)))
    for cap in units.get("reset", []):
        check("reset", cap, lambda c: L.sq8l_master_reset(box, r.rz(c)))
    for cap in units.get("ctrlupd", []):
        check("ctrlupd", cap, lambda c: L.sq8l_master_control_update(box, c.args[0], r.rz(c)))

    def run_voice(c):
        acc = (ctypes.c_float * 2)(c.args[2], c.args[3])
        res = L.sq8l_master_process_voice(box, c.args[0], acc, r.rz(c))
        want = c.result
        if (res, struct.pack("<2f", *acc)) != (1 if want[0] else 0, struct.pack("<2f", want[1], want[2])):
            return [f"result ours ({res}, {acc[0]!r}, {acc[1]!r}) orig {want}"]
        return []

    for cap in units.get("voice", []):
        check("voice", cap, run_voice)

    def run_sr(c):
        res = L.sq8l_master_set_sample_rate(box, c.args[0], r.rz(c))
        return [] if s32(res) == c.result else [f"result ours {res} orig {c.result}"]

    for cap in units.get("sr", []):
        check("sr", cap, run_sr)
    for cap in units.get("crate", []):
        check("crate", cap, lambda c: L.sq8l_master_set_control_rate(box, c.args[0], r.rz(c)))
    for cap in units.get("setvoices", []):
        check("setvoices", cap, lambda c: L.sq8l_master_set_voices(box, c.args[0], c.args[1], r.rz(c)))
    for cap in units.get("edit", []):
        check("edit", cap, lambda c: L.sq8l_master_edit_event(box, c.args[0], c.args[1], r.rz(c)))

    def run_count(c):
        res = L.sq8l_master_active_count(box)
        return [] if res == c.result else [f"result ours {res} orig {c.result}"]

    for cap in units.get("count", []):
        check("count", cap, run_count)

    def run_overrides(c):
        L.sq8l_master_load_overrides(box, (ctypes.c_int32 * 5)(*c.args))

    for cap in units.get("overrides", []):
        check("overrides", cap, run_overrides)

    def send_events(events):
        if events:
            arr = (Raw * len(events))()
            for i, (d, b) in enumerate(events):
                arr[i].deltaFrames = d
                arr[i].data[:] = list(b[:3]) + [0] * (3 - len(b[:3]))
                arr[i].noteOffVelocity = 0
            L.sq8l_master_process_events(box, ctypes.cast(arr, ctypes.c_void_p), len(events))

    def compare_out(n, outl, outr, want_l, want_r):
        ours = struct.pack(f"<{n}f", *outl) + struct.pack(f"<{n}f", *outr)
        theirs = struct.pack(f"<{n}f", *want_l) + struct.pack(f"<{n}f", *want_r)
        if ours != theirs:
            bad = [i for i in range(n) if struct.pack("<f", outl[i]) != struct.pack("<f", want_l[i])]
            return [f"output differs at {len(bad)} samples (first {bad[:5]})"]
        return []

    def run_block(c):
        if c.kind == "block":
            events, n = c.args
            outl, outr = (ctypes.c_float * n)(), (ctypes.c_float * n)()
            replacing, null_out = True, False
        else:
            events, n, init_l, init_r, replacing, null_out = c.args
            outl, outr = (ctypes.c_float * n)(*init_l), (ctypes.c_float * n)(*init_r)
        send_events(events)
        L.sq8l_master_process(box, None if null_out else outl, outr, n, 1 if replacing else 0)
        return compare_out(n, outl, outr, *c.result)

    for cap in s.blocks:
        check(cap.kind, cap, run_block)

    print("replayed captures (kind: captures, failures, module calls checked):")
    for kind, (n, bad, calls) in results.items():
        print(f"  {kind:12s} {n:6d} {bad:4d} {calls:9d}")
    return ok, results


def construction_test():
    """FUN_00461cac on a fresh emulator: module calls and final state of the C++ ctor."""
    from vsthost import SQ8LHost
    from unicorn.x86_const import UC_X86_REG_EDI
    from test_master_capture import Capture, hook_call
    h = SQ8LHost(sample_rate=44100.0)
    e = h.emu
    ref = [0]
    add_code_hook(e, 0x461CC9, lambda uc, a, size, _: ref.__setitem__(0, uc.reg_read(UC_X86_REG_EDI)))
    t = Tracer(h, master=ref, counters=False)
    cap = Capture("ctor", (44100,))

    def enter(emu):
        t.active.append(cap)
        return cap

    def leave(emu, c):
        t.end(c)

    hook_call(e, 0x461CAC, enter, leave)
    h.load()
    r = Replayer(t)
    r.t_master_for_ctor = ref[0]
    cfg = e.u32(CONFIG_PTR)
    overrides = [e.s32(cfg + 0x38 + 4 * i) for i in range(5)]  # SQ8L.ini [synth]
    box = r.construct(cap, 44100, overrides)
    errs = r.finish(cap, box)
    print(f"construction: {len(cap.calls)} module calls, overrides {overrides}: {'OK' if not errs else 'FAIL'}")
    for msg in errs[:6]:
        print("     ", msg[:400])
    return not errs


def report_counts(s):
    print("master routine entries in the original (per-sample routines counted in captured blocks only):")
    items = sorted(s.t.counts.items())
    print("  " + ", ".join(f"{k}={v}" for k, v in items))
    print("  never reached:", [k for k, v in items if v == 0])
    hit = sum(1 for v in s.t.branch_counts.values() if v)
    print(f"branch probes executed inside captures (verified): {hit}/{len(BRANCHES)}")
    print("  never hit:", [k for k, v in s.t.branch_counts.items() if v == 0])


def main():
    t0 = time.time()
    s = Suite()
    print(f"emulator started in {time.time() - t0:.1f}s; running scenarios")
    scenarios(s)
    report_counts(s)
    t1 = time.time()
    ok, _ = replay_all(s)
    ok &= construction_test()
    print(f"replay {time.time() - t1:.1f}s, total {time.time() - t0:.1f}s")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
