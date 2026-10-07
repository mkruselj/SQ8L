"""Differential test: program record (INIT program, parameter table, old-format conversion).

Also hosts the ctypes binding of tests/capi_library.cpp used by test_library.py and
test_sysex.py.
"""
import ctypes
import random
import struct
import sys

from harness import lib as _lib, master_ptr, started_host

PROG = 0x21C
LIB_GLOBAL = 0x494314            # CsoundLib instance
INIT_PROGRAM = 0x4539C0          # FUN_004539c0(prog)
CONVERT_OLD = 0x453C64           # FUN_00453c64(src, dst)
PARAM_GET = 0x45FA38             # CparamEditor get(index)
PARAM_SET = 0x45FB30             # CparamEditor set(index, value)

u8p = ctypes.POINTER(ctypes.c_uint8)


def api():
    L = _lib()
    vp, i32, cp = ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p
    sig = {
        "sq8l_prog_init": ([cp], None),
        "sq8l_prog_convert_old": ([cp, cp], None),
        "sq8l_prog_name": ([cp, cp, i32], i32),
        "sq8l_prog_set_name": ([cp, cp, i32], None),
        "sq8l_param_count": ([], i32),
        "sq8l_param_entry": ([i32, ctypes.POINTER(ctypes.c_uint32), u8p, u8p], None),
        "sq8l_param_descriptor": ([], u8p),
        "sq8l_param_get": ([cp, i32], i32),
        "sq8l_param_set": ([cp, i32, i32], None),
        "sq8l_param_find": ([ctypes.c_uint32], i32),
        "sq8l_sysex_from_nybbles": ([cp, cp], None),
        "sq8l_sysex_to_nybbles": ([cp, cp, i32], None),
        "sq8l_sysex_from_sq80": ([cp, cp, i32, i32, i32, i32], None),
        "sq8l_sysex_source_from": ([i32], i32),
        "sq8l_sysex_source_to": ([i32], i32),
        "sq8l_sysex_name_from": ([cp, i32, cp, i32], i32),
        "sq8l_sysex_name_to": ([cp, i32, i32, cp], None),
        "sq8l_sysex_parse_header": ([cp, ctypes.POINTER(i32)], i32),
        "sq8l_sysex_single_dump": ([cp, cp, i32], i32),
        "sq8l_lib_new": ([cp, i32], vp),
        "sq8l_lib_free": ([vp], None),
        "sq8l_lib_programs": ([vp], u8p),
        "sq8l_lib_header": ([vp], u8p),
        "sq8l_lib_clean": ([vp], i32),
        "sq8l_lib_set_state": ([vp, cp, i32], None),
        "sq8l_lib_restore_backup": ([vp, cp, i32], None),
        "sq8l_lib_load_library": ([vp, cp, i32], i32),
        "sq8l_lib_save_library": ([vp, cp, i32], i32),
        "sq8l_lib_save_backup": ([vp, cp, i32], i32),
        "sq8l_lib_load_bank": ([vp, cp, i32, i32], i32),
        "sq8l_lib_save_bank": ([vp, i32, cp, i32], i32),
        "sq8l_lib_init_library": ([vp, i32], None),
        "sq8l_lib_init_bank": ([vp, i32], None),
        "sq8l_lib_import_sysex_bank": ([vp, cp, i32, i32, i32], i32),
        "sq8l_lib_export_sysex_bank": ([vp, i32, cp, i32], i32),
        "sq8l_lib_write_program": ([vp, i32, cp], i32),
        "sq8l_lib_program_name": ([vp, i32, cp, i32], i32),
        "sq8l_eb_new": ([vp], vp),
        "sq8l_eb_free": ([vp], None),
        "sq8l_eb_state_size": ([], i32),
        "sq8l_eb_save_state": ([vp, cp], None),
        "sq8l_eb_load_state": ([vp, cp], None),
        "sq8l_eb_events": ([vp, ctypes.POINTER(i32), i32], i32),
        "sq8l_eb_init_program": ([vp], None),
        "sq8l_eb_reset_ext_zone": ([vp, i32], None),
        "sq8l_eb_set_bank": ([vp, i32], None),
        "sq8l_eb_select": ([vp, i32, i32], i32),
        "sq8l_eb_select_index": ([vp, i32], i32),
        "sq8l_eb_write": ([vp, i32, i32], i32),
        "sq8l_eb_compare": ([vp, i32, i32], None),
        "sq8l_eb_compare_off": ([vp], None),
        "sq8l_eb_get_chunk": ([vp, cp, i32], i32),
        "sq8l_eb_set_chunk": ([vp, cp, i32], i32),
        "sq8l_eb_import_sysex": ([vp, cp, i32, i32], i32),
        "sq8l_eb_export_sysex": ([vp, cp, i32], i32),
        "sq8l_eb_set_name": ([vp, i32, cp, i32], None),
        "sq8l_eb_name": ([vp, i32, cp, i32], i32),
        "sq8l_eb_set_param": ([vp, i32, i32], None),
        "sq8l_eb_library_index": ([vp], i32),
        "sq8l_settings_parse": ([cp, i32, ctypes.POINTER(i32), ctypes.POINTER(i32)], None),
        "sq8l_settings_modes": ([ctypes.POINTER(i32), cp, ctypes.POINTER(i32)], i32),
    }
    for name, (args, res) in sig.items():
        fn = getattr(L, name)
        fn.argtypes = args
        fn.restype = res
    return L


def buf(n_or_bytes):
    if isinstance(n_or_bytes, int):
        return ctypes.create_string_buffer(n_or_bytes)
    return ctypes.create_string_buffer(bytes(n_or_bytes), len(n_or_bytes))


def cxx_init():
    b = buf(PROG)
    API.sq8l_prog_init(b)
    return b.raw


def emu_call(e, addr, *args):
    return e.call(addr, *args, conv="register")


def rand_bytes(rng, n):
    return bytes(rng.randrange(256) for _ in range(n))


API = None


def main():
    global API
    API = api()
    rng = random.Random(1234)
    h = started_host()
    e = h.emu
    ok = True

    # ---- INIT program
    init = cxx_init()
    p = e.scratch(rand_bytes(rng, PROG))
    emu_call(e, INIT_PROGRAM, p)
    good = e.read(p, PROG) == init
    lib = e.u32(LIB_GLOBAL)
    for i in (424, 450, 511):
        good &= e.read(lib + 0x20 + i * PROG, PROG) == init
    print("INIT program:", "OK" if good else "MISMATCH")
    ok &= good

    # ---- parameter table (CparamEditor of the edit buffer)
    eb = e.u32(master_ptr(h) + 0xFF8)
    pe = e.u32(eb + 0xB7C)
    arr = e.u32(pe + 8)
    n = e.u32(arr - 4)
    cnt = API.sq8l_param_count()
    off, sh, ty = ctypes.c_uint32(), ctypes.c_uint8(), ctypes.c_uint8()
    bad = 0 if n == cnt else 1
    for i in range(min(n, cnt)):
        API.sq8l_param_entry(i, ctypes.byref(off), ctypes.byref(sh), ctypes.byref(ty))
        if struct.unpack("<IBB", e.read(arr + 8 * i, 6)) != (off.value, sh.value, ty.value):
            bad += 1
    print(f"param table: {cnt} entries", "OK" if not bad else f"{bad} MISMATCHES")
    ok &= bad == 0

    # get/set through the original editor on the edit program vs C++
    cur = e.u32(eb + 0xA94)
    bad = 0
    calls = 0
    for i in range(cnt):
        for v in (0, 1, -1, 63, -64, 127, 0x7FFF, -0x8000, 0x12345678, rng.randrange(-2**31, 2**31)):
            before = rand_bytes(rng, PROG)
            e.write(cur, before)
            emu_call(e, PARAM_SET, pe, i, v & 0xFFFFFFFF)
            want = e.read(cur, PROG)
            got = buf(before)
            API.sq8l_param_set(got, i, v)
            got_get = API.sq8l_param_get(got, i)
            want_get, _ = emu_call(e, PARAM_GET, pe, i)
            calls += 1
            if got.raw != want or (got_get & 0xFFFFFFFF) != want_get:
                bad += 1
    print(f"param get/set: {calls} calls", "OK" if not bad else f"{bad} MISMATCHES")
    ok &= bad == 0

    # ---- old (pre-0.90) program conversion
    bad = 0
    src = e.scratch(0x220)
    dst = e.scratch(PROG)
    for k in range(400):
        s = rand_bytes(rng, 0x220)
        if k % 2:
            s = s[:0x202] + bytes([rng.randrange(20)]) + s[0x203:]  # plausible name lengths
        d0 = rand_bytes(rng, PROG)
        e.write(src, s)
        e.write(dst, d0)
        emu_call(e, CONVERT_OLD, src, dst)
        got = buf(d0)
        API.sq8l_prog_convert_old(s, got)
        bad += got.raw != e.read(dst, PROG)
    print("old program conversion: 400 records", "OK" if not bad else f"{bad} MISMATCHES")
    ok &= bad == 0

    # ---- names (ShortString assignment keeps trailing bytes)
    bad = 0
    for k in range(200):
        base = bytearray(init)
        name = "".join(chr(rng.randrange(32, 127)) for _ in range(rng.randrange(0, 20)))
        b = buf(bytes(base))
        API.sq8l_prog_set_name(b, name.encode("latin1"), len(name))
        exp = bytearray(base)
        nn = min(len(name), 15)
        exp[2] = nn
        exp[3:3 + nn] = name[:nn].encode("latin1")
        bad += b.raw != bytes(exp)
    print("program names:", "OK" if not bad else f"{bad} MISMATCHES")
    ok &= bad == 0

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
