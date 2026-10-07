"""Differential test: Csq_lfo (original, emulated) vs sq8l::Lfo (C++).

1. LFO data tables vs emulator memory.
2. Real calls of every entry point captured while the emulated plugin loads and
   plays all bank C/D programs plus edited sounds reaching every LFO feature
   (waves 0..82, HUMAN, PLAY modes, twin, smoothing, delay modes, AM/FM modes,
   mid-note parameter changes). Each call is replayed on the C++ object loaded
   from the captured bytes; outputs and the whole object state must match.
3. Direct calls (fuzz) of the original routines on random states and inputs,
   under both rounding modes.
4. Basic-block coverage of the original unit.

Usage: test_lfo.py [--quick]
"""
import ctypes
import os
import random
import re
import struct
import sys
import time
from collections import Counter

from harness import ROOT, lib, master_ptr
from vsthost import SQ8LHost, effSetProgram
from unicorn import UC_HOOK_BLOCK
from unicorn.x86_const import UC_X86_REG_FPCW

LFO_SIZE = 0xF0
VMT = 0x45CF94
CTOR = 0x45CF9C          # FUN_0045cf9c(cls, alloc, rate)
SET_RATE = 0x45D020      # FUN_0045d020(self, rate)
FREQ_INC = 0x45D110      # FUN_0045d110(self, freq) -> increment
TRIGGER = 0x45D2A4       # FUN_0045d2a4(self, phaseSteps, resetPhase)
TICK = 0x45DB2C          # FUN_0045db2c(self) -> output
WAVE_CALLBACK = 0x45C4F8
SHAPE_BASE = 0x4C1688
UNIT_START, UNIT_END = 0x45CF9C, 0x45DB40
FPCW_PROCESS, FPCW_HOST = 0x0E7F, 0x027F

TABLES = [  # (index in sq8l_lfo_table, address, size)
    ("wave params", 0, 0x494C34, 280), ("shaping tables", 1, 0x4C1688, 2048),
    ("level shift", 2, 0x4C1E88, 2), ("AM shift", 3, 0x4C1E8C, 2),
    ("noise", 4, 0x4C1E90, 256), ("humanize", 5, 0x4C1F90, 256),
]

FIELD_NAMES = {
    0x04: "output", 0x08: "running", 0x0C: "smoothCur", 0x10: "smoothState", 0x14: "smoothA",
    0x18: "smoothB", 0x1C: "phase", 0x20: "phaseInc", 0x24: "phaseOffset0", 0x28: "phaseOffset1",
    0x2C: "freqShift", 0x30: "twin", 0x34: "freqSeen", 0x38: "humanOffset", 0x3C: "reverse",
    0x40: "oneShot", 0x44: "level1Seen", 0x48: "level2Seen", 0x4C: "delaySeen", 0x50: "delayMode",
    0x54: "level", 0x58: "levelTarget", 0x5C: "levelStep", 0x60: "ticks", 0x64: "am", 0x68: "wave",
    0x6C: "waveData", 0x70: "waveMask", 0x74: "waveShift", 0x78: "human", 0x7C: "humanIndex",
    0x80: "humanStep", 0x84: "humanCount", 0x88: "emuClock", 0x8C: "emuPeriod",
    0x90: "levelShift/amShift", 0x94: "controlRate", 0x98: "rateRatio", 0x9C: "rateRatioLow",
    0xA0: "rateRatioHigh", 0xA4: "defaultRate", 0xB0: "freqIn", 0xB4: "humanIn", 0xB8: "waveIn",
    0xBC: "level1In", 0xC0: "level2In", 0xC4: "delayIn", 0xC8: "phaseIn", 0xCC: "twinPhaseIn",
    0xD0: "amIn", 0xD4: "delayModeIn", 0xD8: "reverseIn", 0xDC: "oneShotIn", 0xE0: "twinIn",
    0xE4: "smoothIn", 0xE8: "waveKeyIn", 0xEC: "freqShiftIn",
}
# Everything but the vmt and the callback method pointer (+0xa8 code, +0xac Cdoc).
COMPARED = [o for o in range(0x04, LFO_SIZE, 4) if o not in (0xA8, 0xAC)]


def setup_lib():
    L = lib()
    vp, f32, i32, u32 = ctypes.c_void_p, ctypes.c_float, ctypes.c_int32, ctypes.c_uint32
    sig = {
        "sq8l_lfo_set_bases": ([u32, u32], None),
        "sq8l_lfo_new": ([], vp),
        "sq8l_lfo_free": ([vp], None),
        "sq8l_lfo_load": ([vp, ctypes.c_char_p], None),
        "sq8l_lfo_save": ([vp, ctypes.c_char_p], None),
        "sq8l_lfo_construct": ([vp, f32, i32], None),
        "sq8l_lfo_set_control_rate": ([vp, f32, i32], None),
        "sq8l_lfo_trigger": ([vp, i32, i32, i32], None),
        "sq8l_lfo_tick": ([vp, i32], i32),
        "sq8l_lfo_freq_to_increment": ([vp, i32, i32], i32),
        "sq8l_lfo_table": ([i32, ctypes.POINTER(i32)], ctypes.POINTER(ctypes.c_uint8)),
        "sq8l_waverom_lfo_location": ([i32, u32, ctypes.POINTER(i32)], u32),
    }
    for name, (args, res) in sig.items():
        fn = getattr(L, name)
        fn.argtypes = args
        fn.restype = res
    return L


def f32_from_bits(b):
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


class Checker:
    """Replays calls on the C++ object and compares results and state."""

    def __init__(self, L):
        self.L = L
        self.obj = L.sq8l_lfo_new()
        self.buf = ctypes.create_string_buffer(LFO_SIZE)
        self.count = Counter()
        self.bad = Counter()
        self.examples = []

    def check(self, kind, before, after, run, expected=None, info=""):
        L = self.L
        if before is not None:
            L.sq8l_lfo_load(self.obj, before)
        res = run(self.obj)
        self.buf.raw = after
        L.sq8l_lfo_save(self.obj, self.buf)
        ours = self.buf.raw
        diffs = [FIELD_NAMES.get(o, hex(o)) for o in COMPARED if ours[o:o + 4] != after[o:o + 4]]
        self.count[kind] += 1
        if diffs or (expected is not None and s32(res) != s32(expected)):
            self.bad[kind] += 1
            if len(self.examples) < 12:
                detail = ", ".join(f"{FIELD_NAMES.get(o, hex(o))}: ours={struct.unpack_from('<i', ours, o)[0]:#x} "
                                   f"orig={struct.unpack_from('<i', after, o)[0]:#x}"
                                   for o in COMPARED if ours[o:o + 4] != after[o:o + 4])
                self.examples.append(f"{kind} #{self.count[kind] - 1} {info}: result ours={res} orig={expected}; {detail}")
        return res


# ---------------------------------------------------------------------------- LFO parameter editing
LFO_FIELDS = {  # name: (byte, mask, shift, signed)
    "freq": (0, 0x7F, 0, False), "reset": (1, 0xFF, 0, True), "human": (2, 0xFF, 0, True),
    "wave": (3, 0xFF, 0, True), "l1": (4, 0xFF, 0, True), "delay": (5, 0x3F, 0, False),
    "dmode": (5, 0x40, 6, False), "l2": (6, 0xFF, 0, True), "amamt": (9, 0xFF, 0, True),
    "fmamt": (12, 0xFF, 0, True), "ammode": (13, 0x03, 0, False), "fmmode": (13, 0x0C, 2, False),
    "rev": (13, 0x10, 4, False), "oneshot": (13, 0x20, 5, False), "phs": (14, 0x3F, 0, False),
    "twin": (14, 0x40, 6, False), "smooth": (15, 0x3F, 0, False),
}
# Modulation sources (FUN_00463808): 0..3 LFO1..4 outputs, 4..7 ENV1..4, 16.. MIDI controllers.
SRC_LFO1, SRC_ENV1, SRC_ENV2 = 0, 4, 5


def lfo_param_base(h):
    edit = h.emu.u32(master_ptr(h) + 0xFF8)
    return h.emu.u32(edit + 0xA94) + 0xA2


def set_lfo(h, i, **kw):
    e = h.emu
    a = lfo_param_base(h) + 16 * i
    b = bytearray(e.read(a, 16))
    for k, v in kw.items():
        if k in ("amsrc", "fmsrc"):
            struct.pack_into("<h", b, 7 if k == "amsrc" else 10, v)
            continue
        off, mask, shift, _signed = LFO_FIELDS[k]
        b[off] = (b[off] & ~mask & 0xFF) | ((v << shift) & mask) if mask != 0xFF else v & 0xFF
    e.write(a, bytes(b))


PLAIN = dict(freq=20, reset=0, human=0, wave=0, l1=63, delay=0, dmode=0, l2=63, amsrc=-1, amamt=0,
             fmsrc=-1, fmamt=0, ammode=0, fmmode=0, rev=0, oneshot=0, phs=0, twin=0, smooth=0)


def play(h, events, total):
    """events: list of (time_s, midi_bytes or callable). Renders `total` seconds."""
    sr, blk = h.sr, h.block
    evs = sorted(((int(t * sr), i, a) for i, (t, a) in enumerate(events)), key=lambda x: (x[0], x[1]))
    pos, n_total = 0, int(total * sr)
    while pos < n_total:
        n = min(blk, n_total - pos)
        midi = []
        for t, _, a in evs:
            if pos <= t < pos + n:
                if callable(a):
                    a()
                else:
                    midi.append((t - pos, a))
        if midi:
            h.send_midi(midi)
        h.process(n)
        pos += n


def note(t, dur, key, vel=100):
    return [(t, bytes([0x90, key, vel])), (t + dur, bytes([0x80, key, 0]))]


def scenarios():
    """Edited sounds: (name, base program, [per-LFO overrides], events builder, duration)."""
    out = []
    chord = lambda d: note(0.0, d, 48) + note(0.05, d - 0.05, 60, 80) + note(0.1, d - 0.1, 67, 127)

    # Every waveform (incl. invalid 83..90), forward and reversed, with phase offsets.
    waves = list(range(0, 91))
    for k in range(0, len(waves), 4):
        ws = waves[k:k + 4]
        lfos = [dict(PLAIN, wave=w, freq=(37 * w) % 128, rev=(w // 4) % 2, phs=(w * 5) % 64) for w in ws]
        out.append((f"waves {ws}", 384 + 12, lfos, lambda: chord(0.6), 0.7))
    # Wave switching while running (ROM <-> shape <-> basic).
    def wave_switch(h):
        evs = note(0.0, 1.2, 60)
        for j, w in enumerate([5, 80, 3, 40, 0, 74, 82, 6, 90, 12]):
            evs.append((0.1 + 0.1 * j, lambda w=w: [set_lfo(h, i, wave=(w + 9 * i) % 91) for i in range(4)]))
        return evs
    out.append(("wave switching", 384 + 12, [dict(PLAIN, freq=60)] * 4, wave_switch, 1.3))
    # Humanization modes with frequencies around 7 and above.
    for human in range(0, 7):
        lfos = [dict(PLAIN, human=human, freq=f, reset=-1 if i == 3 else 0) for i, f in enumerate((6, 7, 8, 40))]
        out.append((f"human {human}", 384 + 12, lfos, lambda: chord(2.5), 2.7))
    # HUMAN=ON while fading (no humanization during delay) + mode change mid-note.
    def human_change(h):
        evs = note(0.0, 3.0, 60)
        evs += [(1.0, lambda: [set_lfo(h, i, human=(i + 3) % 7) for i in range(4)]),
                (2.0, lambda: [set_lfo(h, i, human=1, freq=7 + i) for i in range(4)])]
        return evs
    out.append(("human change", 384 + 12,
                [dict(PLAIN, human=1, freq=30, l1=0, l2=63, delay=3, dmode=d) for d in (0, 1, 0, 1)],
                human_change, 3.1))
    # One long note: the humanization counter walks the whole 256-entry table.
    out.append(("human long", 384 + 12, [dict(PLAIN, human=hm, freq=f) for hm, f in ((1, 9), (2, 30), (4, 60), (6, 100))],
                lambda: note(0.0, 24.0, 60), 24.1))
    # Play modes FWD/REV/1XF/1XR with RESET OFF/0/32 and twin mode.
    for reset in (-1, 0, 32):
        lfos = [dict(PLAIN, rev=r, oneshot=o, reset=reset, freq=50, wave=1 + i, phs=16 * i, twin=i % 2)
                for i, (r, o) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1)))]
        out.append((f"play modes reset {reset}", 384 + 12, lfos,
                    lambda: note(0.0, 0.8, 60) + note(0.9, 0.6, 60) + note(1.0, 0.5, 64), 1.6))
    # One-shot toggled while running, twin with all offsets.
    def oneshot_toggle(h):
        evs = note(0.0, 2.0, 60)
        evs += [(0.5, lambda: [set_lfo(h, i, oneshot=1 - (i % 2)) for i in range(4)]),
                (1.2, lambda: [set_lfo(h, i, oneshot=i % 2, rev=1, twin=1, phs=63) for i in range(4)])]
        return evs
    out.append(("oneshot toggle", 384 + 12,
                [dict(PLAIN, freq=90, oneshot=i % 2, twin=1, phs=8 * i + 1, wave=w) for i, w in enumerate((0, 4, 3, 77))],
                oneshot_toggle, 2.1))
    # Delay (fade) EMU/SMTH, rising/falling/equal, fast/slow, mid-note changes.
    for dmode in (0, 1):
        lfos = [dict(PLAIN, dmode=dmode, l1=a, l2=b, delay=d, freq=30)
                for a, b, d in ((0, 63, 1), (63, 0, 20), (40, 40, 10), (10, 50, 63))]
        out.append((f"delay mode {dmode}", 384 + 12, lfos, lambda: chord(2.0), 2.2))

        def delay_change(h, dmode=dmode):
            evs = note(0.0, 3.0, 60)
            evs += [(0.6, lambda: [set_lfo(h, i, l2=(0, 63, 20, 5)[i]) for i in range(4)]),
                    (1.2, lambda: [set_lfo(h, i, delay=(0, 5, 63, 30)[i], l1=(63, 0, 30, 20)[i]) for i in range(4)]),
                    (1.8, lambda: [set_lfo(h, i, dmode=1 - dmode) for i in range(4)]),
                    (2.4, lambda: [set_lfo(h, i, l2=(63, 0, 63, 0)[i], delay=2) for i in range(4)]),
                    # rising fade restarted past its target -> clamped (0x45d4fc)
                    (2.6, lambda: set_lfo(h, 3, l1=0, l2=10, delay=63))]
            return evs
        lfos = [dict(PLAIN, dmode=dmode, l1=a, l2=b, delay=d, freq=45)
                for a, b, d in ((0, 63, 4), (63, 0, 4), (20, 40, 1), (50, 10, 2))]
        out.append((f"delay change {dmode}", 384 + 12, lfos, delay_change, 3.1))
    # Smoothing, AM modes UNI/BIP/PHS/SMT (sources ENV1 and LFO1).
    for ammode in range(4):
        lfos = [dict(PLAIN, freq=40, wave=w, smooth=s, ammode=ammode, amsrc=src, amamt=amt)
                for w, s, src, amt in ((0, 0, SRC_ENV1, 63), (1, 20, SRC_LFO1, -63), (2, 63, SRC_LFO1, 40), (4, 5, SRC_ENV2, -20))]
        lfos[0].update(l1=63, l2=63)
        out.append((f"AM mode {ammode}", 384 + 12, lfos, lambda: chord(1.5), 1.7))
    # FM modes with LFO1 as source: negative frequencies, clamp above 0x8600, PHS and SMT.
    for fmmode in range(4):
        lfos = [dict(PLAIN, freq=25, wave=0)] + [
            dict(PLAIN, freq=f, wave=w, fmmode=fmmode, fmsrc=SRC_LFO1, fmamt=amt, smooth=sm, twin=tw, phs=ph)
            for f, w, amt, sm, tw, ph in ((3, 1, 63, 0, 0, 0), (127, 3, 63, 30, 1, 20), (60, 75, -50, 10, 1, 40))]
        out.append((f"FM mode {fmmode}", 384 + 12, lfos, lambda: chord(1.5), 1.7))
    # Frequency sweep 0..127 with HUMAN OFF/ON/1x/16x.
    for k in range(0, 128, 8):
        lfos = [dict(PLAIN, freq=k + j * 2, human=(0, 1, 2, 6)[j], wave=(0, 1, 2, 4)[j]) for j in range(4)]
        out.append((f"freq {k}..", 384 + 12, lfos, lambda: note(0.0, 0.7, 60), 0.75))
    return out


def basic_block_leaders():
    """Basic block starts of the unit from the Ghidra listing (excluding inline constants)."""
    asm = open(os.path.join(ROOT, "re", "decomp", "mod_lfo4_22.asm")).read()
    leaders, prev_ends = set(), True
    skip = False
    for line in asm.splitlines():
        m = re.match(r"; ==== (\S+) @ ([0-9a-f]+)", line)
        if m:
            # FUN_0045d0f0, FUN_0045d1a4, FUN_0045d818 are 80-bit/float constants, not code.
            skip = int(m.group(2), 16) in (0x45D0F0, 0x45D1A4, 0x45D818) or "Destroy" in m.group(1)
            prev_ends = True
            continue
        if skip:
            continue
        if re.match(r"(LAB|case|switch|default)\w*:", line):
            prev_ends = True
            continue
        m = re.match(r"\s+([0-9a-f]{8})\s+(\S+)", line)
        if not m:
            continue
        addr, op = int(m.group(1), 16), m.group(2)
        if not (UNIT_START <= addr < UNIT_END) or addr == 0x45DB40:
            continue
        if prev_ends:
            leaders.add(addr)
        prev_ends = op.startswith("J") or op in ("CALL", "RET")
    return leaders


def main():
    quick = "--quick" in sys.argv
    L = setup_lib()
    ok = True
    t0 = time.time()
    chk = Checker(L)

    # ---------------------------------------------------------------- load-time calls
    h = SQ8LHost(sample_rate=44100.0)
    e = h.emu
    executed = set()
    e.uc.hook_add(UC_HOOK_BLOCK, lambda uc, addr, size, _: executed.add(addr), begin=UNIT_START, end=UNIT_END - 1)
    ctor_calls, rate_calls = [], []

    def fpcw():
        return e.uc.reg_read(UC_X86_REG_FPCW)

    def enter_ctor(emu):
        r = emu.regs()
        return emu.u32(r["esp"] + 4), r["edx"] & 0xFF, fpcw()

    def exit_ctor(emu, ctx):
        obj = emu.regs()["eax"]
        ctor_calls.append(ctx + (emu.read(obj, LFO_SIZE),))

    def enter_rate(emu):
        r = emu.regs()
        return r["eax"], emu.read(r["eax"], LFO_SIZE), emu.u32(r["esp"] + 4), fpcw()

    def exit_rate(emu, ctx):
        obj, before, rate, cw = ctx
        rate_calls.append((before, rate, cw, emu.read(obj, LFO_SIZE)))

    h_ctor = e.trace(CTOR, enter_ctor, exit_ctor)
    e.trace(SET_RATE, enter_rate, exit_rate)
    h.load()
    h.start()
    e.untrace(h_ctor)
    m = master_ptr(h)
    doc = e.u32(m + 0x1000)
    rom_base = e.u32(doc + 0x2008)
    L.sq8l_lfo_set_bases(rom_base, SHAPE_BASE)

    print("tables")
    size = ctypes.c_int32()
    for name, idx, addr, n in TABLES:
        p = L.sq8l_lfo_table(idx, ctypes.byref(size))
        same = size.value == n and bytes(p[:n]) == e.read(addr, n)
        print(f"  {name}: {n} bytes {'OK' if same else 'MISMATCH'}")
        ok &= same

    for rate, alloc, cw, after in ctor_calls:
        chk.check("ctor", None, after, lambda o: L.sq8l_lfo_construct(o, f32_from_bits(rate), (cw >> 10) & 3))
    for before, rate, cw, after in rate_calls:
        chk.check("setControlRate", before, after,
                  lambda o: L.sq8l_lfo_set_control_rate(o, f32_from_bits(rate), (cw >> 10) & 3))
    print(f"  load: {len(ctor_calls)} constructor calls, {len(rate_calls)} setControlRate calls "
          f"(rates {sorted({f32_from_bits(r[1]) for r in rate_calls})})")

    # ---------------------------------------------------------------- captured calls while playing
    stats = Counter()

    def enter_trigger(emu):
        r = emu.regs()
        return r["eax"], emu.read(r["eax"], LFO_SIZE), s32(r["edx"]), r["ecx"] & 0xFF, fpcw()

    def exit_trigger(emu, ctx):
        obj, before, steps, reset, cw = ctx
        chk.check("trigger", before, emu.read(obj, LFO_SIZE),
                  lambda o: L.sq8l_lfo_trigger(o, steps, reset, (cw >> 10) & 3), info=f"steps={steps} reset={reset}")
        stats["trigger reset" if reset else "trigger no reset"] += 1
        stats[f"trigger cw {cw:#x}"] += 1

    def enter_tick(emu):
        obj = emu.regs()["eax"]
        return obj, emu.read(obj, LFO_SIZE), fpcw()

    def exit_tick(emu, ctx):
        obj, before, cw = ctx
        after = emu.read(obj, LFO_SIZE)
        out = s32(emu.regs()["eax"])
        chk.check("tick", before, after, lambda o: L.sq8l_lfo_tick(o, (cw >> 10) & 3), expected=out)
        st = struct.unpack("<60i", after)
        stats[f"tick cw {cw:#x}"] += 1
        stats[("wave", st[0x68 // 4])] += 1
        stats[("human", st[0x78 // 4])] += 1
        stats[("delayMode", st[0x50 // 4])] += 1
        stats[("twin", st[0x30 // 4])] += 1
        stats[("reverse/oneShot", st[0x3C // 4], st[0x40 // 4])] += 1
        stats[("smoothing", st[0x0C // 4] > 0)] += 1
        stats[("stopped", st[0x08 // 4] == 0)] += 1
        stats["max humanIndex"] = max(stats["max humanIndex"], st[0x7C // 4])
        stats["max ticks"] = max(stats["max ticks"], st[0x60 // 4])
        f = st[0xB0 // 4]
        stats[("freq range", "neg" if f < 0 else "<0x700" if f < 0x700 else "<0x8600" if f < 0x8600 else "clamp")] += 1

    traces = [e.trace(TRIGGER, enter_trigger, exit_trigger), e.trace(TICK, enter_tick, exit_tick)]

    programs = list(range(256, 424)) if not quick else [256, 280, 300, 337, 384 + 10, 384 + 3]
    for prog in programs:
        h.dispatch(effSetProgram, value=prog)
        play(h, note(0.0, 1.4, 48) + note(0.05, 1.35, 60, 80) + note(0.1, 1.3, 67, 127)
             + note(0.7, 0.6, 72, 60) + note(1.5, 0.3, 48, 90), 1.9)
    print(f"  factory programs: {len(programs)} played, "
          f"{chk.count['trigger']} trigger / {chk.count['tick']} tick calls so far")

    for name, prog, lfos, events, dur in scenarios():
        h.dispatch(effSetProgram, value=prog)
        for i, kw in enumerate(lfos):
            set_lfo(h, i, **kw)
        evs = events(h) if events.__code__.co_argcount else events()
        play(h, evs, dur)
        play(h, [], 0.3)  # release
    print(f"  after edited sounds: {chk.count['trigger']} trigger / {chk.count['tick']} tick calls")

    for t in traces:
        e.untrace(t)
    executed_real = set(executed)

    # ---------------------------------------------------------------- direct calls (fuzz)
    rng = random.Random(1234)
    obj = e.heap_alloc(LFO_SIZE)
    n_fuzz = 3000 if quick else 30000

    rates = [1.0, 83.592575, 44.1, 300.0, 0.5, 0.001]
    base_states = []
    for rate in rates:
        e.write(obj, b"\0" * LFO_SIZE)
        e.w32(obj, VMT)
        e.call_fpu(SET_RATE, eax=obj, push=(struct.unpack("<I", struct.pack("<f", rate))[0],), fpcw=FPCW_HOST)
        base_states.append(bytearray(e.read(obj, LFO_SIZE)))

    def random_state():
        b = bytearray(rng.choice(base_states))
        if rng.random() < 0.3:
            struct.pack_into("<B", b, 0xA4, 1)
        put = lambda off, v: struct.pack_into("<i", b, off, s32(v))
        put(0x04, rng.randint(-64, 63))
        put(0x08, rng.choice((-1, -1, -1, 0)))
        put(0x0C, rng.choice((0, 0, rng.randint(1, 300), rng.randint(-5, 5))))
        put(0x10, rng.randint(-128 * 256, 127 * 256))
        a = rng.randint(0, 255)
        put(0x14, a)
        put(0x18, 256 - a)
        put(0x1C, rng.getrandbits(32))
        put(0x20, rng.randint(-0x20000000, 0x20000000))
        put(0x24, rng.getrandbits(32))
        put(0x28, rng.getrandbits(32))
        put(0x30, rng.choice((0, 1)))
        put(0x38, rng.randint(-0x3000, 0x3000))
        for off in (0x3C, 0x40):
            put(off, rng.choice((0, 1)))
        for off in (0x44, 0x48, 0x4C):
            put(off, rng.randint(0, 63))
        dmode = rng.choice((0, 1))
        put(0x50, dmode)
        b[0x90] = (1, 4)[dmode]
        b[0x91] = (0, 3)[dmode]
        put(0x54, rng.randint(-10, 63 << b[0x90]))
        put(0x58, rng.randint(0, 63) << b[0x90])
        put(0x5C, rng.choice((0, rng.randint(-63, 63), rng.randint(-1000, 1000))))
        put(0x60, rng.choice((0, rng.randint(0, 5000), rng.randint(0x7FFFFF00, 0x7FFFFFFF), rng.getrandbits(31))))
        put(0x64, rng.randint(-300, 300))
        wave = rng.randint(0, 82)
        put(0x68, wave)
        put(0x2C, 0)
        if 5 <= wave < 75:
            p = e.read(0x494C34 + 4 * (wave - 5), 4)
            size = ctypes.c_int32()
            off = L.sq8l_waverom_lfo_location(p[1], p[0], ctypes.byref(size))
            put(0x6C, rom_base + off)
            put(0x70, (1 << size.value) - 1)
            put(0x74, 30 - size.value)
            put(0x2C, p[2])
        elif wave >= 75:
            put(0x6C, SHAPE_BASE + 256 * (wave - 75))
        put(0x78, rng.randint(0, 6))
        put(0x7C, rng.choice((rng.randint(0, 0x200), rng.getrandbits(31))))
        put(0x80, rng.choice((0, 256, -256)))
        put(0x84, rng.randint(0, 16))
        put(0x88, rng.randint(-0x400, 0x2000))
        # inputs
        put(0xB0, rng.choice((rng.randint(0, 0x7F) << 8, rng.randint(-0x12000, 0x12000),
                              rng.randint(0x500, 0x900), rng.randint(0x8400, 0x8800))))
        put(0xB4, rng.choice((rng.randint(0, 6), b[0x78])))
        put(0xB8, rng.choice((wave, wave, rng.randint(0, 90))))
        for off in (0xBC, 0xC0, 0xC4):
            put(off, rng.choice((struct.unpack_from("<i", b, off - 0x78)[0], rng.randint(0, 63))))
        put(0xC8, rng.randint(-0x20000, 0x20000))
        put(0xCC, rng.randint(-0x20000, 0x20000))
        put(0xD0, rng.choice((0, rng.randint(-200, 200))))
        put(0xD4, rng.choice((dmode, dmode, 1 - dmode, 2)))
        for off in (0xD8, 0xDC, 0xE0):
            put(off, rng.choice((0, 1)))
        put(0xE4, rng.choice((0, rng.randint(0, 252), rng.randint(-300, 600))))
        put(0xE8, rng.choice((0, 0, 0, rng.randint(0, 40))))
        put(0xEC, rng.choice((0, 0, 0, rng.randint(0, 3))))
        struct.pack_into("<II", b, 0xA8, WAVE_CALLBACK if rng.random() < 0.95 else 0, doc)
        return bytes(b)

    for i in range(n_fuzz):
        cw = rng.choice((FPCW_PROCESS, FPCW_HOST))
        state = random_state()
        e.write(obj, state)
        r = rng.random()
        if r < 0.6:
            out, _, _ = e.call_fpu(TICK, eax=obj, fpcw=cw)
            chk.check("fuzz tick", state, e.read(obj, LFO_SIZE),
                      lambda o: L.sq8l_lfo_tick(o, (cw >> 10) & 3), expected=out)
        elif r < 0.8:
            steps = rng.choice((0, rng.randint(0, 63), rng.randint(-200, 200)))
            reset = rng.choice((0, 1, 0x100, 0xFF))
            e.call_fpu(TRIGGER, eax=obj, edx=steps, ecx=reset, fpcw=cw)
            chk.check("fuzz trigger", state, e.read(obj, LFO_SIZE),
                      lambda o: L.sq8l_lfo_trigger(o, steps, reset, (cw >> 10) & 3))
        elif r < 0.9:
            rate = rng.choice((rng.uniform(0, 500), rng.uniform(0, 0.02), 10 ** rng.uniform(-40, 38),
                               -rng.uniform(0, 100), 83.592575, 1.0, 0.01, float("inf"), float("nan")))
            bits = struct.unpack("<I", struct.pack("<f", rate))[0]
            e.call_fpu(SET_RATE, eax=obj, push=(bits,), fpcw=cw)
            chk.check("fuzz setControlRate", state, e.read(obj, LFO_SIZE),
                      lambda o: L.sq8l_lfo_set_control_rate(o, f32_from_bits(bits), (cw >> 10) & 3), info=f"rate={rate}")
        else:
            for _ in range(20):
                f = rng.choice((rng.randint(-0x10000, 0x10000), rng.randint(0x6F0, 0x710), rng.randint(0x85F0, 0x8610),
                                -0x80000000, 0x7FFFFFFF, rng.getrandbits(32)))
                out, _, _ = e.call_fpu(FREQ_INC, eax=obj, edx=f, fpcw=cw)
                chk.check("fuzz freqToIncrement", state, e.read(obj, LFO_SIZE),
                          lambda o: L.sq8l_lfo_freq_to_increment(o, s32(f), (cw >> 10) & 3), expected=out,
                          info=f"f={s32(f):#x}")

    # ---------------------------------------------------------------- report
    print("results (calls bit-exact / total)")
    for kind in chk.count:
        n, bad = chk.count[kind], chk.bad[kind]
        print(f"  {kind}: {n - bad}/{n}")
        ok &= bad == 0
    for ex in chk.examples:
        print("   ", ex)
    print("coverage of captured real calls")
    for k in sorted(stats, key=str):
        print(f"  {k}: {stats[k]}")
    leaders = basic_block_leaders()
    for what, blocks in (("real calls", executed_real), ("real + direct calls", executed)):
        missed = sorted(leaders - blocks)
        print(f"basic blocks of the original executed by {what}: {len(leaders) - len(missed)}/{len(leaders)}"
              + (f", missed {[hex(a) for a in missed]}" if missed else ""))
    print(f"time {time.time() - t0:.1f}s")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
