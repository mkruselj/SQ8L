"""Differential test: Cmod_foll (unit mod_foll, original emulated) vs sq8l::ModFollower (C++).

1. Real calls of every entry point captured while the emulated plugin plays (glide / mono /
   legato programs, a subset of the factory programs, random patches with random glide):
   each call is replayed on the C++ object loaded from the captured bytes; outputs and the
   whole object state (bytes 4..0x1f) must be identical.
   The glide speed computed by plugCore (argument of setSpeed) is checked against
   ModFollower::glideSpeed computed from the caller's registers.
2. Direct calls of the original routines on random states / arguments (call_fpu), including
   overflow and clamping edge cases and both rounding modes.
3. Code coverage of the unit.
"""
import collections
import ctypes
import random
import struct
import sys
import time

from test_env import (BANK_C, BANK_D, HOST_FPCW, PROCESS_FPCW, RC_NAMES, CONTROL_RATE, Coverage, Results,
                      envlib, f32_from_bits, fpcw_rc, new_host, play_session, s32, stack_arg)

FOLL_SIZE = 0x20
FOLL_RANGE = (0x45EAF0, 0x45ED38)
FOLL_CTOR, FOLL_SET_RATE, FOLL_SET_SPEED, FOLL_SET_TARGET, FOLL_RESET, FOLL_TICK = (
    0x45EB28, 0x45EBA0, 0x45EBE8, 0x45EC1C, 0x45EC68, 0x45EC90)
# Return addresses of the two plugCore setSpeed call sites (glide speed computed by the caller).
GLIDE_NOTE_ON, GLIDE_RETRIGGER = 0x462B72, 0x462D3F
PATCH_GLIDE = 0x173

FOLL_FIELDS = [("rate", 0x04, 4), ("speed", 0x08, 4), ("stepScale", 0x0C, 4), ("stepSize", 0x10, 4),
               ("value", 0x14, 4), ("step", 0x18, 4), ("target", 0x1C, 4)]


def f32_bits(x):
    return struct.unpack("<I", struct.pack("<f", x))[0]


class FollChecker:
    def __init__(self, L, emu):
        self.L, self.e = L, emu
        self.obj = L.sq8l_foll_new()
        self.buf = ctypes.create_string_buffer(FOLL_SIZE)
        self.res = Results("mod follower calls")
        self.stats = collections.Counter()

    def _state(self, base):
        self.buf.raw = base
        self.L.sq8l_foll_save(self.obj, self.buf)
        return self.buf.raw

    def _cmp(self, name, before, after, out_ours=None, out_orig=None, info=""):
        ours = self._state(before)
        bad = [n for n, o, s in FOLL_FIELDS if ours[o:o + s] != after[o:o + s]]
        if ours[:4] != before[:4]:
            bad.append("vmt")
        ok = not bad and out_ours == out_orig
        detail = "" if ok else (f"{info} out ours={out_ours} orig={out_orig} before={before.hex()} diffs="
                                + ", ".join(f"{n}: ours={ours[o:o + s].hex()} orig={after[o:o + s].hex()}"
                                            for n, o, s in FOLL_FIELDS if n in bad))
        if after[8:12] != bytes(4):
            self.stats["states with speed field (+0x08) != 0"] += 1
        self.res.record(name, ok, detail)

    def ctor(self, rate, rc, after):
        self.L.sq8l_foll_init(self.obj, rate, rc)
        self._cmp("ctor 45eb28", after[:4] + bytes(FOLL_SIZE - 4), after, info=f"rate={rate!r}")

    def set_rate(self, before, rate, rc, after, name="setRate 45eba0"):
        self.L.sq8l_foll_load(self.obj, before)
        self.L.sq8l_foll_set_rate(self.obj, rate, rc)
        self._cmp(name, before, after, info=f"rate={rate!r} rc={rc}")

    def set_speed(self, before, speed, rc, after, name="setSpeed 45ebe8"):
        self.L.sq8l_foll_load(self.obj, before)
        self.stats[f"setSpeed rounding={RC_NAMES[rc]}"] += 1
        self.L.sq8l_foll_set_speed(self.obj, speed, rc)
        self._cmp(name, before, after, info=f"speed={speed!r} rc={rc}")

    def set_target(self, before, t, after, name="setTarget 45ec1c"):
        self.L.sq8l_foll_load(self.obj, before)
        self.L.sq8l_foll_set_target(self.obj, t)
        self._cmp(name, before, after, info=f"t={t}")

    def reset(self, before, v, after, name="reset 45ec68"):
        self.L.sq8l_foll_load(self.obj, before)
        self.L.sq8l_foll_reset(self.obj, v)
        self._cmp(name, before, after, info=f"v={v}")

    def tick(self, before, after, out, name="tick 45ec90"):
        self.L.sq8l_foll_load(self.obj, before)
        ours = self.L.sq8l_foll_tick(self.obj)
        if out:
            self.stats["tick with nonzero output"] += 1
        self._cmp(name, before, after, ours, out)


def install_foll_traces(e, chk, L):
    def rd(a):
        return e.read(a, FOLL_SIZE)

    def ctor_in(emu):
        return f32_from_bits(stack_arg(emu, 0)), fpcw_rc(emu)

    def ctor_out(emu, ctx):
        chk.ctor(ctx[0], ctx[1], rd(emu.regs()["eax"]))

    def rate_in(emu):
        obj = emu.regs()["eax"]
        return obj, rd(obj), f32_from_bits(stack_arg(emu, 0)), fpcw_rc(emu)

    def rate_out(emu, ctx):
        obj, before, rate, rc = ctx
        chk.set_rate(before, rate, rc, rd(obj))

    def speed_in(emu):
        r = emu.regs()
        obj, bits, rc = r["eax"], stack_arg(emu, 0), fpcw_rc(emu)
        ret = emu.u32(r["esp"])
        # Glide speed computed by plugCore just before the call (caller registers are intact).
        if ret in (GLIDE_NOTE_ON, GLIDE_RETRIGGER):
            slot = r["ebx"]
            patch = emu.u32(slot + 0x24)
            glide = struct.unpack("<b", emu.read(patch + PATCH_GLIDE, 1))[0]
            if ret == GLIDE_NOTE_ON:
                delta = emu.s32(r["ebp"] + 0x20) - emu.s32(r["ebp"] + 0x1C)
            else:
                delta = s32(r["esi"]) - struct.unpack("<b", emu.read(slot + 0x14, 1))[0]
            ours = f32_bits(L.sq8l_glide_speed(delta, glide, rc))
            chk.res.record("glideSpeed (plugCore)", ours == bits,
                           f"delta={delta} glide={glide} ours={ours:#x} orig={bits:#x}")
        return obj, rd(obj), f32_from_bits(bits), rc

    def speed_out(emu, ctx):
        obj, before, speed, rc = ctx
        chk.set_speed(before, speed, rc, rd(obj))

    def target_in(emu):
        r = emu.regs()
        return r["eax"], rd(r["eax"]), s32(r["edx"])

    def target_out(emu, ctx):
        obj, before, t = ctx
        chk.set_target(before, t, rd(obj))

    def reset_in(emu):
        r = emu.regs()
        return r["eax"], rd(r["eax"]), s32(r["edx"])

    def reset_out(emu, ctx):
        obj, before, v = ctx
        chk.reset(before, v, rd(obj))

    def tick_in(emu):
        obj = emu.regs()["eax"]
        return obj, rd(obj)

    def tick_out(emu, ctx):
        obj, before = ctx
        chk.tick(before, rd(obj), s32(emu.regs()["eax"]))

    return [e.trace(FOLL_CTOR, ctor_in, ctor_out),
            e.trace(FOLL_SET_RATE, rate_in, rate_out),
            e.trace(FOLL_SET_SPEED, speed_in, speed_out),
            e.trace(FOLL_SET_TARGET, target_in, target_out),
            e.trace(FOLL_RESET, reset_in, reset_out),
            e.trace(FOLL_TICK, tick_in, tick_out)]


def random_foll_state(rng):
    def pick(common, extreme):
        return extreme() if rng.random() < 0.1 else common()

    def i32():
        return rng.randint(-(1 << 31), (1 << 31) - 1)

    rate = rng.choice([CONTROL_RATE, CONTROL_RATE, rng.uniform(1, 500)])
    speed = rng.choice([0.0, 0.0, rng.uniform(0, 3e6)])
    step_scale = 4096.0 / rate
    size = pick(lambda: rng.randint(0, 1 << 20), i32)
    value = pick(lambda: rng.randint(-0x7FFFF, 0x7FFFF) << 12, i32)
    target = rng.choice([value, pick(lambda: rng.randint(-0x7FFFF, 0x7FFFF) << 12, i32)])
    step = rng.choice([0, size, -size, pick(lambda: rng.randint(-(1 << 20), 1 << 20), i32)])
    return struct.pack("<IfffiiIi", 0x45EAD0, rate, speed, step_scale, size, value, step & 0xFFFFFFFF, target)


def random_float(rng):
    return rng.choice([rng.uniform(-10, 10), rng.uniform(0, 3e6), rng.uniform(0, 1e12), 0.0, -0.0,
                       float("inf"), float("nan"), rng.uniform(0, 2), 1e-40, rng.uniform(0.5, 1.5)])


def fuzz_foll(e, chk, rng, n):
    obj = e.scratch(FOLL_SIZE)
    for _ in range(n):
        cw = rng.choice([HOST_FPCW, PROCESS_FPCW])
        rc = (cw >> 10) & 3
        state = random_foll_state(rng)
        e.write(obj, state)
        kind = rng.random()
        if kind < 0.4:
            out, _, _ = e.call_fpu(FOLL_TICK, eax=obj, fpcw=cw)
            chk.tick(state, e.read(obj, FOLL_SIZE), s32(out), name="fuzz tick")
        elif kind < 0.6:
            value = struct.unpack_from("<i", state, 0x14)[0]
            t = rng.choice([rng.randint(-0x80000, 0x80000), rng.randint(-(1 << 31), (1 << 31) - 1),
                            value >> 12])  # (value >> 12) << 12 == value when aligned: "already there"
            e.call_fpu(FOLL_SET_TARGET, eax=obj, edx=t, fpcw=cw)
            chk.set_target(state, t, e.read(obj, FOLL_SIZE), name="fuzz setTarget")
        elif kind < 0.7:
            v = rng.choice([rng.randint(-0x80000, 0x80000), rng.randint(-(1 << 31), (1 << 31) - 1)])
            e.call_fpu(FOLL_RESET, eax=obj, edx=v, fpcw=cw)
            chk.reset(state, v, e.read(obj, FOLL_SIZE), name="fuzz reset")
        elif kind < 0.9:
            bits = f32_bits(random_float(rng))
            e.call_fpu(FOLL_SET_SPEED, eax=obj, push=(bits,), fpcw=cw)
            chk.set_speed(state, f32_from_bits(bits), rc, e.read(obj, FOLL_SIZE), name="fuzz setSpeed")
        else:
            bits = f32_bits(rng.choice([CONTROL_RATE, random_float(rng), rng.uniform(1, 1e5)]))
            e.call_fpu(FOLL_SET_RATE, eax=obj, push=(bits,), fpcw=cw)
            chk.set_rate(state, f32_from_bits(bits), rc, e.read(obj, FOLL_SIZE), name="fuzz setRate")


def main():
    """Full run ~2.5 min; --quick: fewer programs and random patches (~1.5 min)."""
    quick = "--quick" in sys.argv
    t0 = time.time()
    L = envlib()
    rng = random.Random(4321)
    h = new_host()
    e = h.emu
    chk = FollChecker(L, e)
    hooks = install_foll_traces(e, chk, L)
    cov = Coverage(e, *FOLL_RANGE)
    h.load()
    h.start()

    print("playing (captures replayed on the fly)", flush=True)
    factory = rng.sample(BANK_C + BANK_D, 4 if quick else 16)
    played = play_session(h, rng, factory=factory, n_random=8 if quick else 25)
    print("  audio played: " + ", ".join(f"{k} {v:.0f}s" for k, v in played.items()))
    ok = chk.res.report()
    chk.stats["states with speed field (+0x08) != 0"] += 0
    for k in sorted(chk.stats):
        print(f"    {k}: {chk.stats[k]}")
    played_blocks = cov.snapshot()
    for hk in hooks:
        e.untrace(hk)

    print("direct calls on random states", flush=True)
    fchk = FollChecker(L, e)
    fuzz_foll(e, fchk, rng, 20000)
    ok &= fchk.res.report()

    skip = [(0x45EAF0, 0x45EB28),   # class metadata before the first routine
            (0x45EB78, 0x45EBA0),   # destructor (Cmod_foll_Destroy)
            (0x45EBE0, 0x45EBE8),   # float constants 1.0, 4096.0
            (0x45ECD8, 0x45ED38)]   # unit initialization / finalization
    for title, blocks in (("real play", played_blocks), ("real play + direct calls", None)):
        print(f"coverage, {title}: never executed (excluding destructor, constants, unit init)")
        runs = cov.uncovered(e, skip, blocks)
        for a, b, first in runs:
            print(f"  {a:#x}..{b:#x}  {first}")
        if not runs:
            print("  none")
    print(f"total {time.time() - t0:.0f}s")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
