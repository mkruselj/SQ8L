"""Differential test: Camp (original unit mod_amp4_13, emulated) vs sq8l::Amp (C++).

Every call of every Camp routine made by the emulated plugin while it plays programs of
banks C and D (plus voice stealing and a sample-rate change) is replayed on the C++ object
loaded from the captured bytes; outputs and the whole object state must be bit-identical.
Paths the plugin never takes are exercised by direct calls with crafted inputs.

Usage: test_amp.py [--programs quick|all|none|N,M,..] [--seconds S]
(quick: 48 programs, ~7 min; all: the 168 programs of banks C and D)
"""
import argparse
import ctypes
import math
import random
import struct
import sys
import time
from collections import Counter, defaultdict

from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_ESP, \
    UC_X86_REG_FP0, UC_X86_REG_FPCW, UC_X86_REG_FPSW

from harness import effSetProgram, floats, lib, master_ptr, render_notes
from vsthost import SQ8LHost, effSetSampleRate
from w32emu import HOST_FPCW, PROCESS_FPCW

AMP_SIZE = 0x74
# Entry points (unit mod_amp4_13) and the unit init code feeding its tables.
CTOR = 0x45E4A0             # Camp.Create
SET_CONSTANTS = 0x45E52C
START = 0x45E554            # voice start: copy from other amp / reset
UPDATE_LEVEL = 0x45E5DC
LEVEL_PAN_UNITY = 0x45E680
LEVEL_PAN = 0x45E69C
SET_SAMPLE_RATE = 0x45E7B8
SET_RAMP_LENGTH = 0x45E7CC
SET_RAMP_TIME = 0x45E7F8
SET_SMOOTHING = 0x45E848
REFRESH_RAMP = 0x45E890
SET_SATURATION = 0x45E89C
PROCESS = 0x45E8EC
PROCESS_FADE = 0x45E95C
PAN_PAIR = 0x45E9D0
PAN_INIT = 0x45EA54
SAT_INIT = 0x45E33C
NAMES = {CTOR: "ctor 45e4a0", SET_CONSTANTS: "setConstants 45e52c", START: "start 45e554",
         UPDATE_LEVEL: "updateLevel 45e5dc", LEVEL_PAN_UNITY: "setLevelPan(1.0) 45e680",
         LEVEL_PAN: "setLevelPan 45e69c", SET_SAMPLE_RATE: "setSampleRate 45e7b8",
         SET_RAMP_LENGTH: "setRampLength 45e7cc", SET_RAMP_TIME: "setRampTime 45e7f8",
         SET_SMOOTHING: "setSmoothing 45e848", REFRESH_RAMP: "refreshRampLength 45e890",
         SET_SATURATION: "setSaturation 45e89c", PROCESS: "process 45e8ec",
         PROCESS_FADE: "processFade 45e95c", PAN_PAIR: "panGains 45e9d0 (unit init)",
         PAN_INIT: "pan table 45ea54 (unit init)", SAT_INIT: "sat tables 45e33c (unit init)"}

# Tables
PAN_TABLE = 0x4C5610       # 127 x {L, R}
SAT_TABLES = 0x4C51F8      # 2 x 65 x {drive, gain}
AMP_SHAPE = 0x4C2110       # 256 Singles
AMP_SHAPE_PTR = 0x4C33C0   # -> AMP_SHAPE
SAT_TABLE_PTR = 0x4C3224   # -> SAT_TABLES
MIN_TARGET = 0x358637BD

# Bank C and D programs covering every factory SAT value (0..4, 9), both DCA4 smoothing modes
# (HARD: 271, 272, 335, 377), non-zero pans and a variety of envelopes.
QUICK_PROGRAMS = [256, 257, 259, 262, 263, 266, 269, 270, 271, 272, 274, 276, 282, 284, 288, 291, 292,
                  297, 301, 304, 305, 312, 320, 329, 335, 336, 343, 352, 355, 358, 363, 367, 373, 377,
                  381, 384, 386, 387, 392, 394, 397, 402, 405, 410, 414, 417, 420, 423]
# Voice stealing (> 8 sounding voices -> soft fade-out of stolen voices) on a few programs.
STEAL_PROGRAMS = [256, 283, 313, 368, 394, 396]


def amp_lib():
    L = lib()
    vp, f32, f64, i32, cp = ctypes.c_void_p, ctypes.c_float, ctypes.c_double, ctypes.c_int32, ctypes.c_char_p
    sig = {
        "sq8l_amp_tables": ([], ctypes.POINTER(f32)),
        "sq8l_amp_shape": ([], ctypes.POINTER(f32)),
        "sq8l_amp_new": ([], vp),
        "sq8l_amp_free": ([vp], None),
        "sq8l_amp_load": ([vp, cp], None),
        "sq8l_amp_save": ([vp, cp], None),
        "sq8l_amp_init": ([vp], None),
        "sq8l_amp_process": ([vp, f64, cp], None),
        "sq8l_amp_process_fade": ([vp, f64, cp, f32], None),
        "sq8l_amp_set_level_pan": ([vp, i32, i32, f32, i32, i32], None),
        "sq8l_amp_set_level_pan_unity": ([vp, i32, i32, i32, i32], None),
        "sq8l_amp_start": ([vp, vp, i32], None),
        "sq8l_amp_set_saturation": ([vp, i32, i32], None),
        "sq8l_amp_set_smoothing": ([vp, i32], None),
        "sq8l_amp_set_ramp_time": ([vp, f32, i32], None),
        "sq8l_amp_set_sample_rate": ([vp, f32, i32], None),
        "sq8l_amp_update_level": ([vp, i32, i32, i32], None),
        "sq8l_amp_set_ramp_length": ([vp, i32, i32], None),
        "sq8l_amp_refresh_ramp_length": ([vp, i32], None),
        "sq8l_amp_set_constants": ([vp], None),
    }
    for name, (args, res) in sig.items():
        fn = getattr(L, name)
        fn.argtypes = args
        fn.restype = res
    return L


def f32_of(bits):
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def s32(bits):
    return struct.unpack("<i", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def field_i(b, off):
    return struct.unpack_from("<i", b, off)[0]


def field_f(b, off):
    return struct.unpack_from("<f", b, off)[0]


def st0_double(uc):
    """ST0 as a Python float; raises if it is not exactly a (normal) double."""
    top = (uc.reg_read(UC_X86_REG_FPSW) >> 11) & 7
    m, se = uc.reg_read(UC_X86_REG_FP0 + top)
    sign = -1.0 if se & 0x8000 else 1.0
    e = (se & 0x7FFF) - 16383
    if m == 0 and (se & 0x7FFF) == 0:
        return math.copysign(0.0, sign)
    if m & 0x7FF or not m >> 63 or not -1022 <= e <= 1023:
        raise ValueError(f"ST0 is not a double: m={m:#x} se={se:#x}")
    return sign * math.ldexp(m >> 11, e - 52)


class Checker:
    """Replays captured calls on the C++ object and keeps per-entry statistics."""

    def __init__(self, L):
        self.L = L
        self.amp = L.sq8l_amp_new()
        self.other = L.sq8l_amp_new()
        self.state = ctypes.create_string_buffer(AMP_SIZE)
        self.out = ctypes.create_string_buffer(8)
        self.count = Counter()
        self.bad = Counter()
        self.tags = defaultdict(Counter)
        self.examples = defaultdict(list)

    def finish(self, entry, after, tags, detail, out_ok=True):
        """Compare the C++ object (already run) with the captured state `after`."""
        self.state.raw = after
        self.L.sq8l_amp_save(self.amp, self.state)
        ok = out_ok and self.state.raw == after
        self.count[entry] += 1
        for t in tags:
            self.tags[entry][t] += 1
        if not ok:
            self.bad[entry] += 1
            if len(self.examples[entry]) < 5:
                ours = self.state.raw
                diffs = [f"+{o:#04x}" for o in range(4, AMP_SIZE, 4) if ours[o:o + 4] != after[o:o + 4]]
                self.examples[entry].append(f"{detail} out_ok={out_ok} state diffs={diffs}")
        return ok


def install_traces(e, chk):
    L, uc = chk.L, e.uc

    def mode(cw):
        return 1 if cw & 0xC00 == 0xC00 else 0

    def regs():
        return (uc.reg_read(UC_X86_REG_EAX), uc.reg_read(UC_X86_REG_EDX), uc.reg_read(UC_X86_REG_ECX),
                uc.reg_read(UC_X86_REG_ESP), uc.reg_read(UC_X86_REG_FPCW))

    def arg(esp, i):  # i-th dword above the return address
        return e.u32(esp + 4 * i)

    # ---------------------------------------------------------------- per sample
    def enter_process(emu):
        eax, edx, _, esp, _ = regs()
        top = (uc.reg_read(UC_X86_REG_FPSW) >> 11) & 7
        fade = arg(esp, 1)
        return eax, edx, emu.read(eax, AMP_SIZE), emu.read(edx, 8), st0_double(uc), fade, top

    def exit_process_common(emu, ctx, entry):
        obj, outp, before, out_before, x, fade, top = ctx
        after, out_after = emu.read(obj, AMP_SIZE), emu.read(outp, 8)
        L.sq8l_amp_load(chk.amp, before)
        chk.out.raw = out_before
        if entry == PROCESS:
            L.sq8l_amp_process(chk.amp, x, chk.out)
        else:
            L.sq8l_amp_process_fade(chk.amp, x, chk.out, f32_of(fade))
        top_after = (uc.reg_read(UC_X86_REG_FPSW) >> 11) & 7
        out_ok = chk.out.raw == out_after and top_after == (top + 1) & 7
        sat = field_i(before, 0x18)
        if sat < 0:
            tags = ["sat off"]
        elif not field_f(before, 0x1C) >= abs(x):
            tags = ["clip +" if x >= 0 else "clip -"]
        else:
            tags = ["cubic"]
        tags.append("ramping" if field_i(before, 0x14) > 0 else "steady")
        chk.finish(entry, after, tags, f"x={x!r}", out_ok)

    e.trace(PROCESS, enter_process, lambda emu, c: exit_process_common(emu, c, PROCESS))
    e.trace(PROCESS_FADE, enter_process, lambda emu, c: exit_process_common(emu, c, PROCESS_FADE))

    # ---------------------------------------------------------------- control rate
    def generic_enter(emu):
        eax, edx, ecx, esp, cw = regs()
        return eax, edx, ecx, (arg(esp, 1), arg(esp, 2)), cw, emu.read(eax, AMP_SIZE)

    def level_pan_tags(before, level, pan, imm, tz):
        tags = ["immediate" if imm else "ramp", "RZ" if tz else "RN"]
        if not imm and field_i(before, 0x14) < 0:
            tags.append("snap to target")
        if level < 0:
            tags.append("level<0")
        if level > 255:
            tags.append("level>255")
        if pan < -63:
            tags.append("pan<-63")
        if pan > 63:
            tags.append("pan>63")
        if field_i(before, 0x58) > 0:
            tags.append("smoothing on")
        return tags

    def exit_level_pan(emu, ctx):
        obj, edx, ecx, (imm, gain), cw, before = ctx
        level, pan, imm = s32(edx), s32(ecx), imm & 0xFF
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_level_pan(chk.amp, level, pan, f32_of(gain), imm, mode(cw))
        after = emu.read(obj, AMP_SIZE)
        tags = level_pan_tags(before, level, pan, imm, mode(cw))
        if field_i(after, 0x40) == MIN_TARGET or field_i(after, 0x44) == MIN_TARGET:
            tags.append("target clamped to 1e-6")
        chk.finish(LEVEL_PAN, after, tags, f"level={level} pan={pan} gain={f32_of(gain)} imm={imm} cw={cw:#x}")

    def exit_level_pan_unity(emu, ctx):
        obj, edx, ecx, (imm, _), cw, before = ctx
        level, pan, imm = s32(edx), s32(ecx), imm & 0xFF
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_level_pan_unity(chk.amp, level, pan, imm, mode(cw))
        chk.finish(LEVEL_PAN_UNITY, emu.read(obj, AMP_SIZE), level_pan_tags(before, level, pan, imm, mode(cw)),
                   f"level={level} pan={pan} imm={imm} cw={cw:#x}")

    def exit_update_level(emu, ctx):
        obj, edx, ecx, (imm, _), cw, before = ctx
        level, pan, imm = s32(edx), s32(ecx), imm & 0xFF
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_update_level(chk.amp, level, pan, imm)
        chk.finish(UPDATE_LEVEL, emu.read(obj, AMP_SIZE), level_pan_tags(before, level, pan, imm, mode(cw)),
                   f"level={level} pan={pan} imm={imm}")

    def enter_start(emu):
        eax, edx, _, _, cw = regs()
        return eax, edx, cw, emu.read(eax, AMP_SIZE), emu.read(edx, AMP_SIZE) if edx else None

    def exit_start(emu, ctx):
        obj, other, cw, before, other_bytes = ctx
        L.sq8l_amp_load(chk.amp, before)
        if other_bytes is not None:
            L.sq8l_amp_load(chk.other, other_bytes)
        L.sq8l_amp_start(chk.amp, chk.other if other_bytes is not None else None, mode(cw))
        chk.finish(START, emu.read(obj, AMP_SIZE), ["copy from voice" if other else "reset (nil)",
                                                    "RZ" if mode(cw) else "RN"], f"other={other:#x} cw={cw:#x}")

    def exit_saturation(emu, ctx):
        obj, edx, _, _, cw, before = ctx
        sat = s32(edx)
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_saturation(chk.amp, sat, mode(cw))
        tag = "off (<0)" if sat < 0 else ("clamped to 63" if sat * 4 + 1 >= 63 else "on")
        chk.finish(SET_SATURATION, emu.read(obj, AMP_SIZE), [tag, "RZ" if mode(cw) else "RN", f"sat={sat}"],
                   f"sat={sat} cw={cw:#x}")

    def exit_smoothing(emu, ctx):
        obj, edx, _, _, cw, before = ctx
        a = s32(edx)
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_smoothing(chk.amp, a)
        tag = "off (<=0)" if a <= 0 else ("clamped to 255" if a >= 256 else "on")
        chk.finish(SET_SMOOTHING, emu.read(obj, AMP_SIZE), [tag, f"amount={a}"], f"amount={a}")

    def exit_ramp_time(emu, ctx):
        obj, _, _, (t, _), cw, before = ctx
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_ramp_time(chk.amp, f32_of(t), mode(cw))
        after = emu.read(obj, AMP_SIZE)
        tf = f32_of(t)
        tag = "t<=0" if not tf > 0 else ("length>0" if field_i(after, 0x48) > 0 else "length 0")
        chk.finish(SET_RAMP_TIME, after, [tag, "RZ" if mode(cw) else "RN", f"t={tf:.6g}"], f"t={tf!r} cw={cw:#x}")

    def exit_ramp_length(emu, ctx):
        obj, edx, _, _, cw, before = ctx
        n = s32(edx)
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_ramp_length(chk.amp, n, mode(cw))
        chk.finish(SET_RAMP_LENGTH, emu.read(obj, AMP_SIZE), ["n>0" if n > 0 else "n<=0", "RZ" if mode(cw) else "RN"],
                   f"n={n} cw={cw:#x}")

    def exit_refresh(emu, ctx):
        obj, _, _, _, cw, before = ctx
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_refresh_ramp_length(chk.amp, mode(cw))
        chk.finish(REFRESH_RAMP, emu.read(obj, AMP_SIZE), ["RZ" if mode(cw) else "RN"], f"cw={cw:#x}")

    def exit_sample_rate(emu, ctx):
        obj, _, _, (sr, _), cw, before = ctx
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_sample_rate(chk.amp, f32_of(sr), mode(cw))
        after = emu.read(obj, AMP_SIZE)
        chk.finish(SET_SAMPLE_RATE, after, ["RZ" if mode(cw) else "RN", f"sr={f32_of(sr):g}",
                                            "rampTime>0" if field_f(before, 0x68) > 0 else "rampTime 0"],
                   f"sr={f32_of(sr)} cw={cw:#x}")

    def exit_constants(emu, ctx):
        obj, _, _, _, cw, before = ctx
        L.sq8l_amp_load(chk.amp, before)
        L.sq8l_amp_set_constants(chk.amp)
        chk.finish(SET_CONSTANTS, emu.read(obj, AMP_SIZE), [], "")

    def enter_ctor(emu):
        eax, edx, _, _, cw = regs()
        return edx & 0xFF, cw

    def exit_ctor(emu, ctx):
        alloc, cw = ctx
        obj = uc.reg_read(UC_X86_REG_EAX)
        L.sq8l_amp_init(chk.amp)  # always round-to-nearest: flag a constructor run in any other mode
        chk.finish(CTOR, emu.read(obj, AMP_SIZE), ["alloc" if alloc else "no alloc", "RZ" if mode(cw) else "RN"],
                   f"cw={cw:#x}", out_ok=not mode(cw))

    e.trace(LEVEL_PAN, generic_enter, exit_level_pan)
    e.trace(LEVEL_PAN_UNITY, generic_enter, exit_level_pan_unity)
    e.trace(UPDATE_LEVEL, generic_enter, exit_update_level)
    e.trace(START, enter_start, exit_start)
    e.trace(SET_SATURATION, generic_enter, exit_saturation)
    e.trace(SET_SMOOTHING, generic_enter, exit_smoothing)
    e.trace(SET_RAMP_TIME, generic_enter, exit_ramp_time)
    e.trace(SET_RAMP_LENGTH, generic_enter, exit_ramp_length)
    e.trace(REFRESH_RAMP, generic_enter, exit_refresh)
    e.trace(SET_SAMPLE_RATE, generic_enter, exit_sample_rate)
    e.trace(SET_CONSTANTS, generic_enter, exit_constants)
    e.trace(CTOR, enter_ctor, exit_ctor)
    for entry in (PAN_PAIR, PAN_INIT, SAT_INIT):
        e.trace(entry, lambda emu: None, lambda emu, c, entry=entry: chk.count.update([entry]))


def compare_floats(name, ours, theirs):
    bad = [i for i, (a, b) in enumerate(zip(ours, theirs)) if struct.pack("<f", a) != struct.pack("<f", b)]
    print(f"  {name}: {len(theirs)} values {'OK' if not bad else f'{len(bad)} MISMATCHES (first {bad[:5]})'}")
    for i in bad[:5]:
        print(f"     [{i}] ours={ours[i]!r} orig={theirs[i]!r}")
    return not bad


def check_tables(L, e):
    print("tables")
    ok = True
    ok &= e.u32(AMP_SHAPE_PTR) == AMP_SHAPE and e.u32(SAT_TABLE_PTR) == SAT_TABLES
    print(f"  table pointers 0x4c33c0 -> {e.u32(AMP_SHAPE_PTR):#x}, 0x4c3224 -> {e.u32(SAT_TABLE_PTR):#x}")
    t = L.sq8l_amp_tables()
    ok &= compare_floats("pan law (0x4c5610)", t[:254], floats(e.read(PAN_TABLE, 254 * 4)))
    ok &= compare_floats("saturation tables (0x4c51f8)", t[254:254 + 260], floats(e.read(SAT_TABLES, 260 * 4)))
    ok &= compare_floats("DCA4 level curve (0x4c2110)", L.sq8l_amp_shape()[:256], floats(e.read(AMP_SHAPE, 1024)))
    return ok


def play(h, programs, seconds):
    # Chord with varied velocities, a key repeated while its first note is still releasing
    # (voice restart takes over the old voice's amp), staggered note-offs.
    pattern = [(0.0, 0.5 * seconds, 48, 100), (0.02, 0.3 * seconds, 60, 64), (0.04, 0.2 * seconds, 67, 127),
               (0.1 * seconds, 0.25 * seconds, 72, 20), (0.35 * seconds, 0.3 * seconds, 60, 64),
               (0.45 * seconds, 0.2 * seconds, 84, 90)]
    for prog in programs:
        h.dispatch(effSetProgram, value=prog)
        render_notes(h, pattern, seconds)


def play_stealing(h, programs):
    # 12 overlapping notes: more than 8 voices -> stolen voices fade out (FUN_0045e95c).
    notes = [(0.015 * i, 0.4, 40 + 3 * i, 40 + 7 * i) for i in range(12)]
    for prog in programs:
        h.dispatch(effSetProgram, value=prog)
        render_notes(h, notes, 0.6)


def edit_buffer(h):
    """The edit buffer (current program) read by the voices at note on and every control tick."""
    return h.emu.u32(h.emu.u32(master_ptr(h) + 0xFF8) + 0xA94)


def play_edits(h):
    """Parameter values no factory program uses, written into the edit buffer: SAT off and
    beyond its range (clipping), pan modulation beyond +-63, final volume modulation driving
    the DCA4 level out of 0..255. Edit buffer bytes: +0x133 PAN, +0x134 (word) final volume
    modulator source, +0x136 its amount, +0x139 pan modulation amount, +0x13a SAT."""
    e = h.emu
    loud = [(0.0, 0.2, 36, 127), (0.0, 0.2, 48, 127), (0.01, 0.15, 55, 127), (0.02, 0.15, 64, 127)]
    h.dispatch(effSetProgram, value=343)  # SAT 9
    for sat in (-1, -128, 15, 16, 127):
        e.write(edit_buffer(h) + 0x13A, struct.pack("<b", sat))
        render_notes(h, loud, 0.15)
    h.dispatch(effSetProgram, value=274)  # PAN -48, positive pan modulation
    for pan, amount in ((-48, -127), (63, 127), (-63, -127)):
        e.write(edit_buffer(h) + 0x133, struct.pack("<b", pan))
        e.write(edit_buffer(h) + 0x139, struct.pack("<b", amount))
        render_notes(h, loud, 0.15)
    h.dispatch(effSetProgram, value=256)
    for amount in (127, -127, 60):
        e.write(edit_buffer(h) + 0x134, struct.pack("<hb", 9, amount))
        render_notes(h, loud, 0.15)


def synthetic(L, e, chk_states, rng):
    """Direct calls of the original routines with crafted inputs for paths the plugin
    never takes (level smoothing, clamps, odd ramp times, extreme samples)."""
    stats = Counter()
    bad = Counter()
    state = ctypes.create_string_buffer(AMP_SIZE)
    out = ctypes.create_string_buffer(8)
    amp = L.sq8l_amp_new()

    def same(a, b, nan_equiv):
        if a == b:
            return True
        if not nan_equiv:
            return False
        # NaNs generated by invalid operations: the x87 default NaN is negative (0xffc00000),
        # the C++ one depends on the host (0x7fc00000 on ARM): compare NaN-ness only.
        return all(x == y or (math.isnan(fx) and math.isnan(fy))
                   for x, y, fx, fy in zip(struct.iter_unpack("<I", a), struct.iter_unpack("<I", b),
                                           floats(a), floats(b)))

    def run(name, before, call_orig, call_ours, out_before=None, cw=PROCESS_FPCW, label="", nan_equiv=False):
        mark = e.scratch_top
        obj = e.scratch(before)
        outp = e.scratch(out_before or bytes(8))
        call_orig(obj, outp, cw)
        after, out_after = e.read(obj, AMP_SIZE), e.read(outp, 8)
        e.reset_scratch(mark)
        L.sq8l_amp_load(amp, before)
        out.raw = out_before or bytes(8)
        call_ours(amp, out, 1 if cw & 0xC00 == 0xC00 else 0)
        state.raw = after
        L.sq8l_amp_save(amp, state)
        stats[name] += 1
        if not same(state.raw[4:], after[4:], nan_equiv) or not same(out.raw, out_after, nan_equiv):
            bad[name] += 1
            if bad[name] <= 3:
                diffs = [f"+{o:#04x} {state.raw[o:o + 4][::-1].hex()}/{after[o:o + 4][::-1].hex()}"
                         for o in range(4, AMP_SIZE, 4) if state.raw[o:o + 4] != after[o:o + 4]]
                print(f"  ! {name} {label}: out ours={floats(out.raw)} orig={floats(out_after)} state diffs={diffs}")

    def with_fields(b, **kw):
        b = bytearray(b)
        offs = dict(rampCount=0x14, satIndex=0x18, level=0x50, pan=0x54, smoothing=0x58, levelAcc=0x5C,
                    smoothA=0x60, smoothB=0x64, rampLength=0x48)
        for k, v in kw.items():
            struct.pack_into("<i", b, offs[k], v)
        return bytes(b)

    smooth_values = [-5, 0, 1, 2, 50, 128, 200, 254, 255, 256, 1000]
    levels = [-1000, -1, 0, 1, 77, 128, 254, 255, 256, 5000]
    pans = [-500, -64, -63, -10, 0, 31, 63, 64, 500]
    for base in chk_states:
        for a in smooth_values:
            run("setSmoothing", base, lambda o, p, cw, a=a: e.call_fpu(SET_SMOOTHING, eax=o, edx=a, fpcw=cw),
                lambda m, b, tz, a=a: L.sq8l_amp_set_smoothing(m, a))
        for _ in range(12):
            sm = rng.choice([1, 50, 128, 255])
            k = 255 - (((255 - sm) * (255 - sm)) >> 8)
            b = with_fields(base, smoothing=k, smoothA=k, smoothB=256 - k,
                            levelAcc=rng.choice([0, 1, 255, 1000, 65280, -300, 70000]))
            lv, pn, imm = rng.choice(levels), rng.choice(pans), rng.choice([0, 1])
            for gbits in (rng.choice([0x3F800000, 0x3F000000, 0x3E800000, 0x40000000, 0]),
                          rng.choice([0x7F800000, 0xFF800000, 0x7FC00000])):
                gain = f32_of(gbits)
                finite = math.isfinite(gain)
                for cw in (PROCESS_FPCW, HOST_FPCW):
                    run("setLevelPan smoothing" if finite else "setLevelPan inf/NaN gain", b,
                        lambda o, p, c, lv=lv, pn=pn, imm=imm, g=gbits: e.call_fpu(LEVEL_PAN, eax=o, edx=lv, ecx=pn,
                                                                                   push=(g, imm), fpcw=c),
                        lambda m, bb, tz, lv=lv, pn=pn, imm=imm, g=gain: L.sq8l_amp_set_level_pan(m, lv, pn, g, imm,
                                                                                                  tz),
                        cw=cw, label=f"level={lv} pan={pn} imm={imm} gain={gbits:#x}", nan_equiv=not finite)
            run("updateLevel smoothing", b,
                lambda o, p, c, lv=lv, pn=pn, imm=imm: e.call_fpu(UPDATE_LEVEL, eax=o, edx=lv, ecx=pn, push=(imm,),
                                                                  fpcw=c),
                lambda m, bb, tz, lv=lv, pn=pn, imm=imm: L.sq8l_amp_update_level(m, lv, pn, imm))
        for sat in (-128, -1, 0, 1, 7, 15, 16, 31, 127):
            for cw in (PROCESS_FPCW, HOST_FPCW):
                run("setSaturation", base, lambda o, p, c, s=sat: e.call_fpu(SET_SATURATION, eax=o, edx=s, fpcw=c),
                    lambda m, b, tz, s=sat: L.sq8l_amp_set_saturation(m, s, tz), cw=cw)
        # Ramp times as Single bits: negative, +-0, tiny (length 0), table values, huge
        # (Round overflows to the x87 integer indefinite), +inf, NaN.
        times = [struct.unpack("<I", struct.pack("<f", t))[0] for t in
                 (-1.0, 0.0, 1e-9, 1e-5, 1.1e-5, 0.0025, 0.01, 0.5, 3.7, 1e5, 1e6)]
        for tb in times + [0x80000000, 0x7F800000, 0x7FC00000]:
            tf = f32_of(tb)
            for cw in (PROCESS_FPCW, HOST_FPCW):
                run("setRampTime", base, lambda o, p, c, tb=tb: e.call_fpu(SET_RAMP_TIME, eax=o, push=(tb,), fpcw=c),
                    lambda m, b, tz, tf=tf: L.sq8l_amp_set_ramp_time(m, tf, tz), cw=cw)
        for n in (-3, 0, 1, 3, 7, 441, 100000):
            run("setRampLength", base, lambda o, p, c, n=n: e.call_fpu(SET_RAMP_LENGTH, eax=o, edx=n, fpcw=c),
                lambda m, b, tz, n=n: L.sq8l_amp_set_ramp_length(m, n, tz))
        # Samples: large (clip), tiny, signed zeros, with and without saturation, ramp counts.
        for sat in (-1, 0, 3, 20):  # SAT parameter -> index -1, 1, 13, 63
            mark = e.scratch_top
            o = e.scratch(base)
            e.call_fpu(SET_SATURATION, eax=o, edx=sat)
            b = e.read(o, AMP_SIZE)
            e.reset_scratch(mark)
            for rc in (5, 1, 0, -1, -2147483648):
                bb = with_fields(b, rampCount=rc)
                # (call_fpu loads ST0 through Fraction, so -0.0 cannot be passed)
                for x in (0.0, 1e-130, -2.5e-8, 0.3, -0.7, 1.0, -1.0, 1.5, -3.0, 1e10, -1e10,
                          rng.uniform(-2, 2), rng.uniform(-0.1, 0.1)):
                    ob = struct.pack("<ff", rng.uniform(-1, 1), rng.choice([0.0, -0.0, 0.25]))
                    run("process", bb, lambda o, p, c, x=x: e.call_fpu(PROCESS, eax=o, edx=p, fpu_in=(x,), fpcw=c),
                        lambda m, buf, tz, x=x: L.sq8l_amp_process(m, x, buf), out_before=ob,
                        label=f"sat={sat} rampCount={rc} x={x!r}")
                    fade = rng.choice([0.0, 0.3, 1.0, 0.999])
                    fb = struct.unpack("<I", struct.pack("<f", fade))[0]
                    run("processFade", bb,
                        lambda o, p, c, x=x, fb=fb: e.call_fpu(PROCESS_FADE, eax=o, edx=p, push=(fb,), fpu_in=(x,),
                                                               fpcw=c),
                        lambda m, buf, tz, x=x, f=f32_of(fb): L.sq8l_amp_process_fade(m, x, buf, f), out_before=ob,
                        label=f"sat={sat} rampCount={rc} x={x!r} fade={fade}")
    L.sq8l_amp_free(amp)
    return stats, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--programs", default="quick", help="quick, all, or a comma separated list")
    ap.add_argument("--seconds", type=float, default=0.3, help="audio rendered per program")
    a = ap.parse_args()
    if a.programs == "all":
        programs = list(range(256, 424))
    elif a.programs == "quick":
        programs = QUICK_PROGRAMS
    elif a.programs == "none":  # tables, constructors and synthetic calls only
        programs = []
    else:
        programs = [int(p) for p in a.programs.split(",")]

    L = amp_lib()
    chk = Checker(L)
    t0 = time.time()
    h = SQ8LHost(sample_rate=44100.0)
    e = h.emu
    install_traces(e, chk)  # before load: constructors and unit init run in main()/DllMain
    h.load()
    h.start()
    ok = check_tables(L, e)

    play(h, programs, a.seconds)
    if programs:
        play_stealing(h, STEAL_PROGRAMS)
        play_edits(h)
    # Sample rate change while voices exist (ramp lengths recomputed with round-to-nearest).
    h.dispatch(effSetProgram, value=QUICK_PROGRAMS[0])
    render_notes(h, [(0.0, 0.05, 60, 100)], 0.03)
    h.dispatch(effSetSampleRate, opt=48000.0)
    render_notes(h, [(0.0, 0.05, 64, 100)], 0.1)
    h.dispatch(effSetSampleRate, opt=44100.0)
    ok &= check_tables(L, e)  # still intact after playing
    elapsed = time.time() - t0

    print(f"\ncaptured calls ({len(programs)} programs + stealing on {len(STEAL_PROGRAMS)}, {elapsed:.0f}s)")
    for entry in sorted(NAMES):
        n, b = chk.count[entry], chk.bad[entry]
        status = "" if entry in (PAN_PAIR, PAN_INIT, SAT_INIT) else (" bit-exact" if not b else f" {b} MISMATCHES")
        print(f"  {NAMES[entry]:32s} {n:9d}{status}")
        tags = chk.tags.get(entry)
        if tags:
            shown = [f"{k}: {v}" for k, v in sorted(tags.items(), key=lambda kv: -kv[1]) if not k.startswith(("sat=", "amount=", "t=", "sr="))]
            vals = sorted(k for k in tags if k.startswith(("sat=", "amount=", "t=", "sr=")))
            print(f"      {', '.join(shown)}")
            if vals:
                print(f"      values: {', '.join(vals)}")
        for ex in chk.examples.get(entry, []):
            print(f"      ! {ex}")
        if entry not in (PAN_PAIR, PAN_INIT, SAT_INIT):
            ok &= b == 0
    never = [NAMES[x] for x in NAMES if chk.count[x] == 0]
    if never:
        print("  never called:", ", ".join(never))

    print("\nsynthetic calls (paths the plugin does not take)")
    rng = random.Random(1)
    # Base states: a few real objects of the running plugin.
    master = master_ptr(h)
    bases = [e.read(e.u32(master + (i * 0x3A + 1) * 4 + 0x7C), AMP_SIZE) for i in (0, 3, 7)]
    stats, bad = synthetic(L, e, bases, rng)
    for k in sorted(stats):
        exact = "bit-exact" + (" except NaN sign/payload" if "NaN" in k else "")
        print(f"  {k:28s} {stats[k]:6d} {exact if not bad[k] else f'{bad[k]} MISMATCHES'}")
        ok &= bad[k] == 0

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
