"""Differential test: Cdoc (DOC 5503 oscillators, original emulated) vs sq8l::Doc (C++).

1. static tables (pitch / level) vs the running plugin's memory;
2. helper routines called directly over their whole input range (pitch -> frequency,
   level -> amplitude, wavesample/pitch computation);
3. every entry point traced while the emulated plugin loads, starts and plays many programs
   (AM, sync, one-shot/drum, noise, glide, restart) across the keyboard at several sample
   rates; each call is replayed on the C++ object loaded from the captured state and must
   give the same FPU result and the same object bytes;
4. randomized direct calls (state fuzzing) of the entry points on a scratch copy of the
   object, to reach branches factory programs don't use.
"""
import ctypes
import os
import random
import struct
import sys
import time
from collections import Counter
from fractions import Fraction

from unicorn.x86_const import UC_X86_REG_FPCW

from harness import LIB_PATH, master_ptr, render_notes
from vsthost import SQ8LHost, effGetProgramName, effSetProgram
from w32emu import HOST_FPCW, PROCESS_FPCW

OBJ = 10400
VOICE_BASE, VOICE_SIZE = 0x8, 0x200
PARAM_BASE, PARAM_SIZE = 0x20A0, 0x80
ROM_PTR = 0x2008

CTOR = 0x45B9E0
RESET_ALL = 0x45BC18
RESET_VOICE = 0x45BB54
SET_NUM_VOICES = 0x45BC20
SET_SAMPLE_RATE = 0x45BC4C
START_VOICE = 0x45BCD0
SET_KEYS = 0x45BD44
SET_PITCH_KEY = 0x45C460
STOP_VOICE = 0x45BD78
UPDATE = 0x45BDA4
INTERP_LEVELS = 0x45C378
PARAMS = 0x45C7E0
RENDER = 0x45C7F4
LEVEL_TO_AMP = 0x45C568
PITCH_TO_FREQ = 0x45B8B4
COMPUTE_PITCH = 0x45C49C

PITCH_TABLE, PITCH_COUNT = 0x4C0278, 2049
LEVEL_TABLE_PTR = 0x4C3124

# Programs: bank C = 256.., bank D = 384.. (names checked at run time).
PROGRAMS = [
    256 + 0, 256 + 1, 256 + 2, 256 + 4, 256 + 7, 256 + 10, 256 + 11, 256 + 14, 256 + 16, 256 + 18,
    256 + 21, 256 + 28, 256 + 31, 256 + 32, 256 + 34, 256 + 41, 256 + 45, 256 + 71, 256 + 72,
    256 + 80, 256 + 82, 256 + 85, 256 + 87, 256 + 88, 256 + 113, 256 + 120, 256 + 121, 256 + 124,
    256 + 125, 256 + 126, 256 + 127,
    384 + 0, 384 + 3, 384 + 4, 384 + 8, 384 + 10, 384 + 12, 384 + 16, 384 + 21, 384 + 33, 384 + 34,
]
GLIDE_PROGRAMS = {256 + 4, 256 + 10, 256 + 16, 256 + 28, 256 + 41, 256 + 45, 256 + 120}
CHORD = [(0.0, 0.25, 36, 100), (0.02, 0.22, 60, 80), (0.05, 0.25, 84, 127), (0.08, 0.2, 108, 60),
         (0.11, 0.15, 21, 90), (0.14, 0.12, 127, 70), (0.16, 0.10, 0, 110)]
LEGATO = [(0.0, 0.10, 48, 100), (0.08, 0.10, 55, 100), (0.16, 0.10, 43, 90), (0.30, 0.04, 72, 100),
          (0.35, 0.04, 72, 100)]
# SQ8L_DOC_QUICK=1: a short smoke run (few programs, small fuzz).
QUICK = os.environ.get("SQ8L_DOC_QUICK") == "1"
# Options -> Emulation overrides (CplugMaster fields read by FUN_00463890 when filling the
# parameter block): +0xfec DCA1-3 smoothing (>0: used as is, 2 = FAST, 3/4 = no interpolation),
# +0xff4 DC-BLOCK (>0: mode + 1; 2 = ON, 3 = OFF).
OVERRIDE_RUNS = [({0xFEC: 2, 0xFF4: 2}, [256 + 0, 256 + 14, 256 + 31, 384 + 10]),
                 ({0xFEC: 3, 0xFF4: 3}, [256 + 1, 256 + 72]),
                 ({0xFEC: 4}, [256 + 41])]
EXTRA_RATES = [(48000.0, [256 + 0, 256 + 87, 256 + 41, 384 + 8, 256 + 72]),
               (96000.0, [256 + 1, 256 + 71, 384 + 0])]


def doclib():
    L = ctypes.CDLL(LIB_PATH)
    vp, f32, f64, i32, u32 = ctypes.c_void_p, ctypes.c_float, ctypes.c_double, ctypes.c_int32, ctypes.c_uint32
    cp, u8p, i32p = ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int32)
    sig = {
        "sq8l_doc_object_size": ([], u32),
        "sq8l_doc_new": ([u32], vp),
        "sq8l_doc_free": ([vp], None),
        "sq8l_doc_load": ([vp, cp, u32], None),
        "sq8l_doc_save": ([vp, cp, u32], None),
        "sq8l_doc_load_voice": ([vp, u32, cp], None),
        "sq8l_doc_save_voice": ([vp, u32, cp], None),
        "sq8l_doc_set_num_voices": ([vp, u32], None),
        "sq8l_doc_set_sample_rate": ([vp, f32, i32], None),
        "sq8l_doc_reset_all": ([vp, i32], None),
        "sq8l_doc_reset_voice": ([vp, u32, i32], None),
        "sq8l_doc_start_voice": ([vp, u32, i32, i32, i32, i32], None),
        "sq8l_doc_set_keys": ([vp, u32, i32, i32, i32], None),
        "sq8l_doc_stop_voice": ([vp, u32], None),
        "sq8l_doc_update": ([vp, u32, i32], None),
        "sq8l_doc_interpolate_levels": ([vp, u32, i32], None),
        "sq8l_doc_params_offset": ([vp, u32], u32),
        "sq8l_doc_render": ([vp, u32], f64),
        "sq8l_doc_level_to_amp": ([vp, i32, i32], f32),
        "sq8l_doc_pitch_to_freq": ([i32], u32),
        "sq8l_doc_compute_pitch": ([vp, i32, i32, i32, i32, i32, i32p, u8p, u8p], None),
        "sq8l_doc_pitch_table": ([], ctypes.POINTER(ctypes.c_uint16)),
        "sq8l_doc_level_table": ([], ctypes.POINTER(f32)),
    }
    for name, (args, res) in sig.items():
        fn = getattr(L, name)
        fn.argtypes = args
        fn.restype = res
    return L


def describe(off):
    if off < VOICE_BASE:
        return f"+{off:#x}"
    if off < VOICE_BASE + 16 * VOICE_SIZE:
        v, r = divmod(off - VOICE_BASE, VOICE_SIZE)
        if r < 0x150:
            return f"voice{v}.osc{r // 0x70}+{r % 0x70:#x}"
        return f"voice{v}+{r:#x}"
    if off >= PARAM_BASE:
        v, r = divmod(off - PARAM_BASE, PARAM_SIZE)
        return f"param{v}+{r:#x}"
    return f"+{off:#x}"


def diff_bytes(ours, theirs, base_off=0):
    if ours == theirs:
        return []
    out = []
    for o in range(0, len(ours), 4):
        if ours[o:o + 4] != theirs[o:o + 4]:
            a, b = struct.unpack_from("<I", ours, o)[0], struct.unpack_from("<I", theirs, o)[0]
            out.append(f"{describe(base_off + o)} ours={a:#010x} orig={b:#010x}")
    return out


def is_tz(cw):
    assert cw & 0x300 == 0x200, f"unexpected precision control in CW {cw:#x}"
    return 1 if cw & 0xC00 == 0xC00 else 0


def update_decisions(before, v):
    """Branch decisions FUN_0045bda4 takes for voice v (for coverage reporting); None when the
    call returns early (v >= numVoices)."""
    if v >= struct.unpack_from("<I", before, 4)[0]:
        return None
    P = struct.unpack_from("<32i", before, PARAM_BASE + v * PARAM_SIZE)
    V = struct.unpack_from("<128i", before, VOICE_BASE + v * VOICE_SIZE)
    key_changed = P[0x54 // 4] != 0 or V[0x1a4 // 4] != P[0x5c // 4] or V[0x1a8 // 4] != P[0x60 // 4]
    am, sync = P[0x64 // 4], P[0x68 // 4]
    out = []
    for i in range(3):
        o = 0x70 * i // 4
        enabled = P[6 * i] != 0 or am == 1 and i < 2
        source = i == 0 and (P[6] != 0 or am == 1) and (sync != 0 or am != 0)
        if not enabled and not source:
            out.append("off")
            continue
        recalc = (key_changed or V[o + 1] == 0 or P[6 * i + 1] != V[o + 2] or P[6 * i + 3] != V[o + 3]
                  or P[6 * i + 4] != V[o + 4] or (i == 1 and (sync != 0) != (V[o + 0x68 // 4] == 2)))
        out.append("recalc" if recalc else "cached")
    stale = am == 1 and P[0x6c // 4] != 0 and out[0] == "cached" and out[1] == "recalc"
    return key_changed, out, stale


class Checker:
    """Replays traced calls on the C++ object and records mismatches and coverage."""

    def __init__(self, L):
        self.L = L
        self.d = L.sq8l_doc_new(16)      # whole-object replays
        self.dr = L.sq8l_doc_new(16)     # render replays (only the voice block is reloaded)
        self.rkey = None
        self.calls = Counter()
        self.bad = Counter()
        self.cov = Counter()
        self.buf = ctypes.create_string_buffer(OBJ)
        self.vbuf = ctypes.create_string_buffer(VOICE_SIZE)

    def fail(self, kind, msg):
        self.bad[kind] += 1
        if self.bad[kind] <= 5:
            print(f"  MISMATCH {kind} #{self.calls[kind]}: {msg}")

    def replay(self, kind, before, after, base, fn, cw):
        """Load `before`, run fn(doc, tz), save over a copy of `before`, compare with `after`."""
        L = self.L
        self.calls[kind] += 1
        L.sq8l_doc_load(self.d, before, base)
        res = fn(self.d, is_tz(cw))
        self.buf.raw = before
        L.sq8l_doc_save(self.d, self.buf, base)
        diffs = diff_bytes(self.buf.raw, after)
        if diffs:
            self.fail(kind, f"cw={cw:#x} {len(diffs)} differing dwords: {diffs[:6]}")
        return res

    def replay_render(self, d_addr, v, glob, blk, after_blk, y, cw):
        L = self.L
        self.calls["render"] += 1
        if cw != PROCESS_FPCW:
            self.fail("render", f"unexpected CW {cw:#x}")
        if glob is not None:
            L.sq8l_doc_load(self.dr, glob, d_addr)
        if blk is None:
            out = L.sq8l_doc_render(self.dr, v)
            if Fraction(out) != y:
                self.fail("render", f"v={v} out ours={out!r} orig={float(y)!r}")
            return
        L.sq8l_doc_load_voice(self.dr, v, blk)
        out = L.sq8l_doc_render(self.dr, v)
        L.sq8l_doc_save_voice(self.dr, v, self.vbuf)
        diffs = diff_bytes(self.vbuf.raw, after_blk, VOICE_BASE + v * VOICE_SIZE)
        if Fraction(out) != y or diffs:
            self.fail("render", f"v={v} out ours={out!r} orig={float(y)!r} diffs={diffs[:6]}")
        self.render_coverage(blk, after_blk)

    def render_coverage(self, b, a):
        c = self.cov
        phase, prev = struct.unpack_from("<II", b, 0x164)
        order = struct.unpack_from("<i", b, 0x160)[0]
        c[f"render dc={int(struct.unpack_from('<i', b, 0x190)[0] != 0)}"] += 1
        if phase > prev:
            c["render interpolate only"] += 1
            return
        am = struct.unpack_from("<i", b, 0x150)[0]
        c[f"render new DOC sample, order {order}, AM={int(am != 0)}"] += 1
        if order >= 3:
            c0 = struct.unpack_from("<f", a, 0x180)[0]
            c[f"render order {order} slope {'kept' if abs(c0) >= 0.001 else 'dropped'}"] += 1
        for i in range(3):
            o = 0x70 * i
            halted_b, = struct.unpack_from("<i", b, o + 0x24)
            halted_a, = struct.unpack_from("<i", a, o + 0x24)
            wrapped, mode = struct.unpack_from("<i", b, o + 0x64)[0], struct.unpack_from("<i", b, o + 0x68)[0]
            if halted_b:
                c["render osc halted"] += 1
                continue
            if halted_a == 1 and not wrapped:
                c["render osc halted by zero sample"] += 1
            if wrapped:
                c[f"render osc wrap mode {mode}"] += 1
                if i == 0 and struct.unpack_from("<i", b, 0x70 + 0x68)[0] == 2:
                    c["render sync reset of osc 1"] += 1
            if struct.unpack_from("<i", a, o + 0x64)[0] and not wrapped:
                c["render osc accumulator wraps"] += 1

    def update_coverage(self, before, after, d_addr, v, prefix=""):
        c = Counter()
        decisions = update_decisions(before, v)
        if decisions is None:
            self.cov[prefix + "update v >= numVoices (no-op)"] += 1
            return
        p = PARAM_BASE + v * PARAM_SIZE
        P = struct.unpack_from("<32i", before, p)
        vb = VOICE_BASE + v * VOICE_SIZE
        A = struct.unpack_from("<128i", after, vb)
        init, reset_phase, new_note, force = P[0x48 // 4:0x58 // 4]
        linked = P[0x58 // 4]
        am, sync, ambug = P[0x64 // 4], P[0x68 // 4], P[0x6c // 4]
        c[f"update init={int(init != 0)} resetPhase={int(reset_phase != 0)} linked={int(linked != 0)}"] += 1
        c[f"update newNote={int(new_note != 0)} dcBlock>0={int(P[0x7c // 4] > 0)}"] += 1
        c[f"update AM={am} sync={int(sync != 0)}"] += 1
        c[f"update smoothMode={A[0x1ac // 4]}"] += 1
        c[f"update interpOrder={A[0x160 // 4]}"] += 1
        if A[(0x70 + 0x6c) // 4]:
            c["update AM bug applied to osc 1"] += 1
        for i in range(3):
            en = P[i * 6]
            mode = A[(0x70 * i + 0x68) // 4]
            if A[(0x70 * i + 0x24) // 4] == -1:
                c["update osc disabled"] += 1
            else:
                c[f"update osc running enabled={int(en != 0)} mode={mode}"] += 1
        key_changed, paths, stale = decisions
        c[f"update keyChanged={int(key_changed)}"] += 1
        for i, path in enumerate(paths):
            c[f"update osc{i} {path}"] += 1
        if stale:
            c["update AM bug with osc 0 cached (stale stack byte)"] += 1
        for k, n in c.items():
            self.cov[prefix + k] += n


def install_traces(emu, ck):
    """Trace every Cdoc entry point; each call is replayed when it returns."""
    def cw():
        return emu.uc.reg_read(UC_X86_REG_FPCW)

    def whole(kind, args_fn, call_fn, extra_exit=None):
        def enter(e):
            r = e.regs()
            args = args_fn(e, r)
            return r["eax"], args, cw(), e.read(r["eax"], OBJ)

        def leave(e, ctx):
            d_addr, args, c, before = ctx
            after = e.read(d_addr, OBJ)
            if kind == "update":
                ck.update_coverage(before, after, d_addr, args[0])
            res = ck.replay(kind, before, after, d_addr, lambda d, tz: call_fn(d, tz, *args), c)
            if extra_exit:
                extra_exit(e, d_addr, args, res)
        handles.append(emu.trace(addr_of[kind], enter, leave))

    L = ck.L
    handles = []
    addr_of = {"resetAll": RESET_ALL, "resetVoice": RESET_VOICE, "setNumVoices": SET_NUM_VOICES,
               "setSampleRate": SET_SAMPLE_RATE, "startVoice": START_VOICE, "setKeys": SET_KEYS,
               "setPitchKey": SET_PITCH_KEY, "stopVoice": STOP_VOICE, "update": UPDATE,
               "interpolateLevels": INTERP_LEVELS, "params": PARAMS}
    reg_v = lambda e, r: (r["edx"],)  # noqa: E731
    whole("resetAll", lambda e, r: (), lambda d, tz: L.sq8l_doc_reset_all(d, tz))
    whole("resetVoice", reg_v, lambda d, tz, v: L.sq8l_doc_reset_voice(d, v, tz))
    whole("setNumVoices", reg_v, lambda d, tz, n: L.sq8l_doc_set_num_voices(d, n))
    whole("setSampleRate", lambda e, r: (struct.unpack("<f", e.read(r["esp"] + 4, 4))[0],),
          lambda d, tz, sr: L.sq8l_doc_set_sample_rate(d, sr, tz))
    whole("startVoice", lambda e, r: (r["edx"], r["ecx"], e.s32(r["esp"] + 4), e.s32(r["esp"] + 8),
                                      e.s32(r["esp"] + 12)),
          lambda d, tz, v, key, linked, nn, rp: L.sq8l_doc_start_voice(d, v, key, linked, nn, rp))
    whole("setKeys", lambda e, r: (r["edx"], r["ecx"], e.s32(r["esp"] + 4)),
          lambda d, tz, v, key, flag: L.sq8l_doc_set_keys(d, v, key, flag, tz))
    whole("setPitchKey", lambda e, r: (r["edx"], r["ecx"]),
          lambda d, tz, v, key: L.sq8l_doc_set_keys(d, v, key, 0, tz))
    whole("stopVoice", reg_v, lambda d, tz, v: L.sq8l_doc_stop_voice(d, v))
    whole("update", reg_v, lambda d, tz, v: L.sq8l_doc_update(d, v, tz))
    whole("interpolateLevels", reg_v, lambda d, tz, v: L.sq8l_doc_interpolate_levels(d, v, tz))

    def params_exit(e, d_addr, args, res):
        off = L.sq8l_doc_params_offset(ck.d, args[0])
        expect = d_addr + off if off else 0
        if e.regs()["eax"] != expect:
            ck.fail("params", f"v={args[0]} ours={expect:#x} orig={e.regs()['eax']:#x}")
    whole("params", reg_v, lambda d, tz, v: None, params_exit)

    def render_enter(e):
        esp = e.regs()["esp"]
        d_addr, v = e.u32(esp + 4), e.u32(esp + 8)
        # Globals (numVoices, constants, rate-dependent fields) only change at setSampleRate /
        # setNumVoices: reload the whole object into the render replica when they do.
        key = (d_addr, e.read(d_addr + 4, 4) + e.read(d_addr + ROM_PTR, 0x98))
        glob = None
        if key != ck.rkey:
            ck.rkey = key
            glob = e.read(d_addr, OBJ)
        blk = e.read(d_addr + VOICE_BASE + v * VOICE_SIZE, VOICE_SIZE) if v < 16 else None
        return d_addr, v, glob, blk, cw()

    def render_exit(e, ctx):
        d_addr, v, glob, blk, c = ctx
        after = e.read(d_addr + VOICE_BASE + v * VOICE_SIZE, VOICE_SIZE) if v < 16 else None
        ck.replay_render(d_addr, v, glob, blk, after, e.read_st(0), c)
    handles.append(emu.trace(RENDER, render_enter, render_exit))
    return handles


def check_tables(L, e, doc):
    ok = True
    ours = bytes(ctypes.cast(L.sq8l_doc_pitch_table(), ctypes.POINTER(ctypes.c_uint8 * (2 * PITCH_COUNT))).contents)
    same = ours == e.read(PITCH_TABLE, 2 * PITCH_COUNT)
    print(f"  pitch table ({PITCH_COUNT} entries): {'OK' if same else 'MISMATCH'}")
    ok &= same
    level_addr = e.u32(LEVEL_TABLE_PTR)
    ours = bytes(ctypes.cast(L.sq8l_doc_level_table(), ctypes.POINTER(ctypes.c_uint8 * 1024)).contents)
    same = ours == e.read(level_addr, 1024)
    print(f"  level table (256 entries at {level_addr:#x}): {'OK' if same else 'MISMATCH'}")
    ok &= same
    return ok


def check_helpers(L, e, doc):
    """Call the internal helpers directly over their input ranges."""
    ok = True
    d = L.sq8l_doc_new(16)
    L.sq8l_doc_load(d, e.read(doc, OBJ), doc)

    bad = 0
    pitches = list(range(-0x200, 0x9200)) + [0x7FFF0000 // 64, 0x7FFFFFFF, -0x7FFFFFFF - 1, 0x12345, 0x7FFF, 0x8000]
    for p in pitches:
        eax, _, _ = e.call_fpu(PITCH_TO_FREQ, eax=p)
        if eax != L.sq8l_doc_pitch_to_freq(p):
            bad += 1
            if bad <= 5:
                print(f"    pitchToFreq({p:#x}) ours={L.sq8l_doc_pitch_to_freq(p)} orig={eax}")
    print(f"  pitchToFreq: {len(pitches) - bad}/{len(pitches)} exact")
    ok &= bad == 0

    bad = n = 0
    levels = list(range(-3, 0x8083)) + [0x10000, 0x7FFFFFFF, -0x80000000]
    for tz, cwv, lv in [(1, PROCESS_FPCW, levels), (0, HOST_FPCW, levels[::7])]:
        for level in lv:
            n += 1
            _, _, st0 = e.call_fpu(LEVEL_TO_AMP, eax=doc, edx=level, fpu_out=True, fpcw=cwv)
            ours = L.sq8l_doc_level_to_amp(d, level, tz)
            if Fraction(ours) != st0:
                bad += 1
                if bad <= 5:
                    print(f"    levelToAmp({level}, tz={tz}) ours={ours!r} orig={float(st0)!r}")
    print(f"  levelToAmp: {n - bad}/{n} exact")
    ok &= bad == 0

    rnd = random.Random(1234)
    out = e.scratch(16)
    pitch, wr, pg = ctypes.c_int32(), ctypes.c_uint8(), ctypes.c_uint8()
    bad = n = 0
    cases = [(w, k, s, f, pk) for w in list(range(77)) + [0xFFFFFFFF] for k in (0, 60, 127) for s in (-37, -25, -13, -1, 0, 12, 24, 36, 48)
             for f, pk in ((0, 60),)]
    cases += [(rnd.choice(list(range(80)) + [-1, 1000]), rnd.randint(-20, 150), rnd.randint(-60, 60), rnd.randint(-64, 64),
               rnd.randint(-10, 140)) for _ in range(15000)]
    for wave, key, semi, fine, pkey in cases:
        n += 1
        e.write(out, bytes(16))
        e.call_fpu(COMPUTE_PITCH, eax=doc, edx=key, ecx=pkey,
                   push=(semi, fine, wave, out + 8, out + 9, out))
        theirs = (e.s32(out), e.u8(out + 9), e.u8(out + 8))
        L.sq8l_doc_compute_pitch(d, key, pkey, wave, fine, semi, ctypes.byref(pitch), ctypes.byref(wr), ctypes.byref(pg))
        if (pitch.value, wr.value, pg.value) != theirs:
            bad += 1
            if bad <= 5:
                print(f"    computePitch(wave={wave}, key={key}, semi={semi}, fine={fine}, pkey={pkey}) "
                      f"ours={(pitch.value, wr.value, pg.value)} orig={theirs}")
    print(f"  computePitch: {n - bad}/{n} exact")
    ok &= bad == 0
    L.sq8l_doc_free(d)
    return ok


def play(h, ck, programs, rate, overrides=None):
    for prog in programs:
        h.dispatch(effSetProgram, value=prog)
        name = h.string_op(effGetProgramName)
        for off, val in (overrides or {}).items():
            h.emu.w32(master_ptr(h) + off, val)
        if overrides:
            name += f" with overrides {overrides}"
        t0 = time.time()
        n0 = ck.calls["render"]
        render_notes(h, CHORD, 0.32)
        if prog in GLIDE_PROGRAMS:
            render_notes(h, LEGATO, 0.42)
        print(f"  {rate:.0f} Hz program {prog} {name!r}: {ck.calls['render'] - n0} render calls "
              f"({time.time() - t0:.1f}s)")
        sys.stdout.flush()


# ---------------------------------------------------------------- state fuzzing

def rand_voice_state(rnd, base, v):
    """A plausible random voice block (0x200 bytes)."""
    b = bytearray(VOICE_SIZE)
    for i in range(3):
        o = 0x70 * i
        wave = rnd.randrange(0, 80)
        size = rnd.randrange(8, 16)
        res = rnd.randrange(0, 8)
        acc_mask = (1 << (res + 17)) - 1
        bank = rnd.randrange(4)
        page = rnd.randrange(256)
        fields = {
            0x00: rnd.choice([0, -1, -1, -1]), 0x04: rnd.choice([0, -1, -1]), 0x08: wave,
            0x0c: rnd.randrange(-30, 40), 0x10: rnd.randrange(-32, 32), 0x14: rnd.randrange(0, 0x9000),
            0x18: rnd.randrange(0, 0x9000), 0x1c: rnd.randrange(-0x800, 0x800), 0x20: rnd.randrange(0, 0x7f01),
            0x24: rnd.choice([0, 0, 0, 1, -1]), 0x3c: bank, 0x40: page, 0x44: (bank << 16) + (page << 8),
            0x48: size, 0x4c: 1 << size, 0x50: res, 0x54: acc_mask, 0x58: res + 17 - size,
            0x5c: rnd.randrange(acc_mask + 1), 0x60: rnd.choice([rnd.randrange(0x10000), rnd.randrange(64)]),
            0x64: rnd.choice([0, 0, 1]), 0x68: rnd.choice([0, 0, 1, 2]), 0x6c: rnd.choice([0, -1]),
        }
        for off, val in fields.items():
            struct.pack_into("<i" if val < 0 else "<I", b, o + off, val)
        for off in (0x2c, 0x30, 0x34, 0x38):
            struct.pack_into("<f", b, o + off, rnd.choice([rnd.uniform(0, 1), rnd.uniform(-0.3, 1.2), 1e-6]))
    struct.pack_into("<i", b, 0x150, rnd.choice([0, 0, 1, 2]))
    struct.pack_into("<iii", b, 0x154, rnd.choice([0, -1]), rnd.choice([0, 1]), rnd.randrange(2))
    struct.pack_into("<i", b, 0x160, rnd.randrange(5))
    phase = rnd.randrange(1 << 30)
    prev = rnd.choice([phase, rnd.randrange(1 << 30), max(phase - rnd.randrange(1 << 20), 0)])
    struct.pack_into("<II", b, 0x164, phase, prev)
    for off in range(0x16c, 0x17c, 4):
        struct.pack_into("<f", b, off, rnd.uniform(-1.5, 1.5))
    for off in range(0x180, 0x190, 4):
        struct.pack_into("<f", b, off, rnd.choice([rnd.uniform(-2, 2), rnd.uniform(-0.002, 0.002), 0.0]))
    struct.pack_into("<i", b, 0x190, rnd.choice([0, 1, 3]))
    for off in range(0x194, 0x1a4, 4):
        struct.pack_into("<f", b, off, rnd.uniform(-1, 1))
    struct.pack_into("<iii", b, 0x1a4, rnd.randrange(0, 128), rnd.randrange(0, 128), rnd.randrange(-1, 6))
    struct.pack_into("<i", b, 0x1b4, rnd.choice([0, 2]))  # junk in an unused dword (reset clears it)
    return bytes(b)


def rand_params(rnd, base, vstate):
    b = bytearray(PARAM_SIZE)
    for i in range(3):
        o = 0x18 * i
        old_wave, old_semi, old_fine = struct.unpack_from("<iii", vstate, 0x70 * i + 0x08)
        same = rnd.random() < 0.5
        wave = old_wave if same else rnd.choice(list(range(76)) + [80, -1])
        semi = old_semi if same and rnd.random() < 0.8 else rnd.randrange(-40, 50)
        fine = old_fine if same and rnd.random() < 0.8 else rnd.randrange(-40, 40)
        level = rnd.choice([rnd.randrange(0, 0x7f01), 0, 0x7f00, rnd.randrange(-100, 0x9000)])
        mod = rnd.choice([0, rnd.randrange(-0x1000, 0x1000), rnd.randrange(-0x9000, 0x9000)])
        struct.pack_into("<6i", b, o, rnd.choice([0, -1, -1, -1, 1]), wave, level, semi, fine, mod)
    old_wk, old_pk = struct.unpack_from("<ii", vstate, 0x1a4)
    linked = rnd.choice([0, 0, base + VOICE_BASE + rnd.randrange(16) * VOICE_SIZE])
    flags = [rnd.choice([0, 0, -1]) for _ in range(4)]
    wk = old_wk if rnd.random() < 0.7 else rnd.randrange(-5, 133)
    pk = old_pk if rnd.random() < 0.7 else rnd.randrange(-5, 133)
    struct.pack_into("<4iIii", b, 0x48, *flags, linked, wk, pk)
    struct.pack_into("<iiii", b, 0x64, rnd.choice([0, 0, 1, 2]), rnd.choice([0, 0, -1, 1]),
                     rnd.choice([0, -1]), rnd.choice([0, -1, 5]))
    struct.pack_into("<ii", b, 0x78, rnd.choice([0, 1, 1, 2, 2, 3, 4, -1]), rnd.choice([0, 1, 2, -1]))
    return bytes(b)


def fuzz(L, e, doc, ck, n_update=4000, n_render=30000, n_misc=3000):
    rnd = random.Random(20261005)
    real = e.read(doc, OBJ)
    obj = e.heap_alloc(OBJ)
    vmt_rom = real[0:4], real[ROM_PTR:ROM_PTR + 4]

    def make_obj(sample_rate_state=True):
        b = bytearray(real)
        for v in range(16):
            b[VOICE_BASE + v * VOICE_SIZE:VOICE_BASE + (v + 1) * VOICE_SIZE] = rand_voice_state(rnd, obj, v)
        for v in range(16):
            vs = bytes(b[VOICE_BASE + v * VOICE_SIZE:VOICE_BASE + (v + 1) * VOICE_SIZE])
            b[PARAM_BASE + v * PARAM_SIZE:PARAM_BASE + (v + 1) * PARAM_SIZE] = rand_params(rnd, obj, vs)
        struct.pack_into("<I", b, 4, rnd.choice([16, 16, 16, 8, 3]))
        b[0:4], b[ROM_PTR:ROM_PTR + 4] = vmt_rom
        return bytes(b)

    # Stack slot of FUN_0045bda4's uninitialised waveReg byte when called through call_fpu.
    esp0 = e.regs()["esp"]
    stale_slot = esp0 - 8 - 16 - 0x4C + 0x19

    def run(kind, before, target, call_fn, tz, **kw):
        e.write(obj, before)
        cwv = PROCESS_FPCW if tz else HOST_FPCW
        res = e.call_fpu(target, fpcw=cwv, **kw)
        after = e.read(obj, OBJ)
        ck.replay(kind, before, after, obj, call_fn, cwv)
        return res


    for _ in range(n_update):
        before = make_obj()
        v = rnd.randrange(17)
        tz = rnd.random() < 0.8
        if v < 16:  # see doc.md: the AM-bug path may read osc 0's register from a stale stack byte
            bank0 = struct.unpack_from("<I", before, VOICE_BASE + v * VOICE_SIZE + 0x3c)[0]
            poke = ((bank0 & 1) << 7) | rnd.randrange(128)
            e.write(stale_slot, bytes([poke]))
        kind = rnd.choice(["fuzz update"] * 6 + ["fuzz setKeys"] * 2 + ["fuzz interpolateLevels"] * 2)
        if kind == "fuzz update":
            run(kind, before, UPDATE, lambda d, t: L.sq8l_doc_update(d, v, t), tz, eax=obj, edx=v)
            if v < 16:
                after = e.read(obj, OBJ)
                ck.update_coverage(before, after, obj, v, "fuzz ")
                if (update_decisions(before, v) or (0, 0, False))[2]:
                    # Confirm the original really reads that stack byte: flip its bit 7.
                    e.write(stale_slot, bytes([poke ^ 0x80]))
                    e.write(obj, before)
                    e.call_fpu(UPDATE, eax=obj, edx=v, fpcw=PROCESS_FPCW if tz else HOST_FPCW)
                    bank_off = VOICE_BASE + v * VOICE_SIZE + 0x70 + 0x3C
                    flipped = (e.u32(obj + bank_off) ^ struct.unpack_from("<I", after, bank_off)[0]) == 1
                    ck.cov[f"fuzz update stale stack byte read by the original confirmed={flipped}"] += 1
        elif kind == "fuzz setKeys":
            key, flag = rnd.randrange(-3, 131), rnd.choice([0, 1, -1])
            run(kind, before, SET_KEYS, lambda d, t: L.sq8l_doc_set_keys(d, v, key, flag, t), tz,
                eax=obj, edx=v, ecx=key, push=(flag,))
        else:
            run(kind, before, INTERP_LEVELS, lambda d, t: L.sq8l_doc_interpolate_levels(d, v, t), tz, eax=obj, edx=v)

    for _ in range(n_misc):
        before = make_obj()
        v = rnd.choice(list(range(16)) + [16, 17, 0xFFFFFFFF])
        tz = rnd.random() < 0.5
        kind = rnd.choice(["fuzz startVoice", "fuzz stopVoice", "fuzz resetVoice", "fuzz params",
                           "fuzz setNumVoices", "fuzz resetAll"])
        if kind == "fuzz startVoice":
            key, linked = rnd.randrange(-3, 131), rnd.choice([-5, -1, 0, 7, 15, 16, 100])
            nn, rp = rnd.choice([0, -1]), rnd.choice([0, -1])
            run(kind, before, START_VOICE, lambda d, t: L.sq8l_doc_start_voice(d, v, key, linked, nn, rp), tz,
                eax=obj, edx=v, ecx=key, push=(rp, nn, linked))
        elif kind == "fuzz stopVoice":
            run(kind, before, STOP_VOICE, lambda d, t: L.sq8l_doc_stop_voice(d, v), tz, eax=obj, edx=v)
        elif kind == "fuzz resetVoice":
            run(kind, before, RESET_VOICE, lambda d, t: L.sq8l_doc_reset_voice(d, v, t), tz, eax=obj, edx=v)
        elif kind == "fuzz resetAll":
            run(kind, before, RESET_ALL, lambda d, t: L.sq8l_doc_reset_all(d, t), tz, eax=obj)
        elif kind == "fuzz setNumVoices":
            n = rnd.choice([0, 1, 8, 15, 16, 17, 0xFFFFFFFF])
            run(kind, before, SET_NUM_VOICES, lambda d, t: L.sq8l_doc_set_num_voices(d, n), tz, eax=obj, edx=n)
        else:
            eax, _, _ = run(kind, before, PARAMS, lambda d, t: None, tz, eax=obj, edx=v)
            L.sq8l_doc_load(ck.d, before, obj)
            off = L.sq8l_doc_params_offset(ck.d, v)
            if eax != (obj + off if off else 0):
                ck.fail(kind, f"v={v} ours={off:#x} orig={eax:#x}")

    rates = [40000.0, 44100.0, 48000.0, 50000.0, 88200.0, 96000.0, 176400.0, 192000.0, 384000.0,
             38455.85546875, 22050.0, 8000.0, 1000.0, 1e7]
    rates += [rnd.uniform(40000, 200000) for _ in range(200)]
    for sr in rates:
        for tz in (0, 1):
            before = make_obj()
            bits = struct.unpack("<I", struct.pack("<f", sr))[0]
            run("fuzz setSampleRate", before, SET_SAMPLE_RATE, lambda d, t: L.sq8l_doc_set_sample_rate(d, sr, t),
                tz, eax=obj, push=(bits,))

    # render: random voice states (all orders, AM, DC, halts, wraps, sync), random sample rates
    bad0 = ck.bad["fuzz render"]
    vbuf = ctypes.create_string_buffer(VOICE_SIZE)
    d = L.sq8l_doc_new(16)
    base = make_obj()
    for k in range(n_render):
        if k % 2000 == 0:
            e.write(obj, base)
            sr = rnd.choice([44100.0, 48000.0, 96000.0, rnd.uniform(40000, 200000)])
            e.call_fpu(SET_SAMPLE_RATE, eax=obj, push=(struct.unpack("<I", struct.pack("<f", sr))[0],), fpcw=HOST_FPCW)
            base = e.read(obj, OBJ)
            L.sq8l_doc_load(d, base, obj)
        v = rnd.randrange(16)
        blk = rand_voice_state(rnd, obj, v)
        voff = obj + VOICE_BASE + v * VOICE_SIZE
        e.write(voff, blk)
        _, _, y = e.call_fpu(RENDER, push=(v, obj), fpu_out=True)
        after = e.read(voff, VOICE_SIZE)
        ck.calls["fuzz render"] += 1
        L.sq8l_doc_load_voice(d, v, blk)
        out = L.sq8l_doc_render(d, v)
        L.sq8l_doc_save_voice(d, v, vbuf)
        diffs = diff_bytes(vbuf.raw, after, VOICE_BASE + v * VOICE_SIZE)
        if Fraction(out) != y or diffs:
            ck.fail("fuzz render", f"v={v} out ours={out!r} orig={float(y)!r} diffs={diffs[:6]}")
        ck.render_coverage(blk, after)
    # voices beyond numVoices render silence
    for v in (16, 17, 1000):
        _, _, y = e.call_fpu(RENDER, push=(v, obj), fpu_out=True)
        ck.calls["fuzz render"] += 1
        if Fraction(L.sq8l_doc_render(d, v)) != y:
            ck.fail("fuzz render", f"v={v} silent voice")
    L.sq8l_doc_free(d)
    return ck.bad["fuzz render"] == bad0


def main():
    L = doclib()
    assert L.sq8l_doc_object_size() == OBJ
    ck = Checker(L)
    ok = True

    # ---- load + start at 44.1 kHz with every entry point traced (constructor included)
    h = SQ8LHost(sample_rate=44100.0)
    e = h.emu
    ctor = {}

    def ctor_enter(emu):
        return emu.uc.reg_read(UC_X86_REG_FPCW), emu.regs()["ecx"]

    def ctor_exit(emu, ctx):
        ctor["obj"], ctor["cw"], ctor["n"] = emu.regs()["eax"], ctx[0], ctx[1]
        ctor["bytes"] = emu.read(ctor["obj"], OBJ)
    e.trace(CTOR, ctor_enter, ctor_exit)
    handles = install_traces(e, ck)
    h.load()
    h.start()
    doc = e.u32(master_ptr(h) + 0x1000)
    assert ctor["obj"] == doc

    print("constructor")
    d = L.sq8l_doc_new(ctor["n"])
    buf = ctypes.create_string_buffer(ctor["bytes"], OBJ)
    L.sq8l_doc_save(d, buf, doc)
    diffs = diff_bytes(buf.raw, ctor["bytes"])
    print(f"  Cdoc.Create({ctor['n']}) cw={ctor['cw']:#x}: {'OK' if not diffs else diffs[:8]}")
    ok &= not diffs and ctor["cw"] == HOST_FPCW
    L.sq8l_doc_free(d)

    print("static tables")
    ok &= check_tables(L, e, doc)
    print("helpers (direct calls)")
    ok &= check_helpers(L, e, doc)

    print("playback (traced entry points replayed on the C++ object)")
    play(h, ck, PROGRAMS[::8] if QUICK else PROGRAMS, 44100.0)
    for ov, progs in ([] if QUICK else OVERRIDE_RUNS):
        play(h, ck, progs, 44100.0, ov)
    for off in (0xFEC, 0xFF4):
        e.w32(master_ptr(h) + off, 0)
    ok &= check_tables(L, e, doc)  # still unmodified after playing

    print("state fuzzing (direct calls on a scratch object)")
    for hd in handles:
        e.untrace(hd)
    if QUICK:
        fuzz(L, e, doc, ck, n_update=300, n_render=2000, n_misc=200)
    else:
        fuzz(L, e, doc, ck)

    for rate, progs in ([] if QUICK else EXTRA_RATES):
        h2 = SQ8LHost(sample_rate=rate)
        install_traces(h2.emu, ck)
        h2.load()
        h2.start()
        play(h2, ck, progs, rate)

    print("results")
    for kind in sorted(ck.calls):
        n, b = ck.calls[kind], ck.bad[kind]
        print(f"  {kind:24s} {n - b:8d}/{n:<8d} bit-exact")
        ok &= b == 0
    for kind in ck.bad:
        if kind not in ck.calls:
            print(f"  {kind}: {ck.bad[kind]} failures")
            ok = False
    print("coverage")
    for k in sorted(ck.cov):
        print(f"  {k:60s} {ck.cov[k]}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
