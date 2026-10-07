"""Differential test: CfilterSQ (original, emulated) vs sq8l::FilterSQ (C++)."""
import ctypes
import struct
import sys
from fractions import Fraction

from harness import (TEST_CHORD, TEST_PROGRAMS, MASTER_FILTER_TABLE, effSetProgram, floats, lib,
                     master_ptr, render_notes, started_host)

FILTER_SIZE = 0x7C
PROCESS = 0x45F578
APPLY_PARAMS = 0x45F4DC
UNIT_TABLES = 0x4C5A14
COMPARED = [(f"+{o:#04x}", o, 4) for o in range(0x04, FILTER_SIZE, 4) if o not in (0x20, 0x24)]


def compare_floats(name, ours, theirs):
    bad = [i for i, (a, b) in enumerate(zip(ours, theirs)) if struct.pack("<f", a) != struct.pack("<f", b)]
    status = "OK" if not bad else f"{len(bad)} MISMATCHES (first {bad[:5]})"
    print(f"  {name}: {len(ours)} values {status}")
    for i in bad[:5]:
        print(f"     [{i}] ours={ours[i]!r} orig={theirs[i]!r}")
    return not bad


def main():
    L = lib()
    h = started_host()
    e = h.emu
    ok = True

    print("unit tables")
    ours = L.sq8l_filter_unit_tables()[:320]
    theirs = floats(e.read(UNIT_TABLES, 320 * 4))
    ok &= compare_floats("cutoff Hz", ours[:256], theirs[:256])
    ok &= compare_floats("reso amount", ours[256:288], theirs[256:288])
    ok &= compare_floats("reso gain", ours[288:], theirs[288:])

    print("coefficient table @44100")
    table = L.sq8l_filter_table_new(44100.0)
    ours = L.sq8l_filter_table_data(table)[:32 * 256 * 2]
    theirs = floats(e.read(master_ptr(h) + MASTER_FILTER_TABLE, 32 * 256 * 2 * 4))
    ok &= compare_floats("p/k entries", ours, theirs)

    # Capture real calls while playing several programs.
    calls = {"process": [], "apply": []}

    def enter_process(emu):
        obj = emu.regs()["eax"]
        return obj, emu.read(obj, FILTER_SIZE), emu.read_st(0)

    def exit_process(emu, ctx):
        obj, before, x = ctx
        calls["process"].append((before, x, emu.read_st(0), emu.read(obj, FILTER_SIZE)))

    def enter_apply(emu):
        from unicorn.x86_const import UC_X86_REG_FPCW
        r = emu.regs()
        return r["eax"], emu.read(r["eax"], FILTER_SIZE), r["edx"] & 0xFF, emu.uc.reg_read(UC_X86_REG_FPCW)

    def exit_apply(emu, ctx):
        obj, before, imm, cw = ctx
        calls["apply"].append((before, imm, cw, emu.read(obj, FILTER_SIZE)))

    e.trace(PROCESS, enter_process, exit_process)
    e.trace(APPLY_PARAMS, enter_apply, exit_apply)
    for prog in TEST_PROGRAMS:
        h.dispatch(effSetProgram, value=prog)
        render_notes(h, TEST_CHORD, 0.25)
    print(f"captured {len(calls['process'])} process calls, {len(calls['apply'])} applyParams calls")

    flt = L.sq8l_filter_new(table, 44100.0)
    buf = ctypes.create_string_buffer(FILTER_SIZE)

    def state_diff(after_ours, after_orig):
        return [n for n, o, s in COMPARED if after_ours[o:o + s] != after_orig[o:o + s]]

    # process(): exact output and state
    n_bad = 0
    for i, (before, x, y, after) in enumerate(calls["process"]):
        L.sq8l_filter_load(flt, before)
        out = L.sq8l_filter_process(flt, float(x))
        buf.raw = after
        L.sq8l_filter_save(flt, buf)
        diffs = state_diff(buf.raw, after)
        if Fraction(out) != y or diffs:
            n_bad += 1
            if n_bad <= 5:
                print(f"  process #{i}: x={float(x)!r} out ours={out!r} orig={float(y)!r} state diffs={diffs}")
    print(f"  process: {len(calls['process']) - n_bad}/{len(calls['process'])} bit-exact")
    ok &= n_bad == 0

    n_bad = 0
    for i, (before, imm, cw, after) in enumerate(calls["apply"]):
        L.sq8l_filter_load(flt, before)
        c, r = struct.unpack_from("<ii", before, 0x0C)
        L.sq8l_filter_set_params(flt, c, r, imm, 1 if cw & 0xC00 == 0xC00 else 0)
        buf.raw = after
        L.sq8l_filter_save(flt, buf)
        diffs = state_diff(buf.raw, after)
        if diffs:
            n_bad += 1
            if n_bad <= 5:
                print(f"  applyParams #{i}: imm={imm} cw={cw:#x} diffs={diffs}")
    print(f"  applyParams: {len(calls['apply']) - n_bad}/{len(calls['apply'])} bit-exact")
    ok &= n_bad == 0

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
