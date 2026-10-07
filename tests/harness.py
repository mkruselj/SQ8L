"""Shared helpers for differential tests: emulated original vs C++ port."""
import ctypes
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "oracle"))

from vsthost import SQ8LHost, effSetProgram  # noqa: E402
from render import render_notes  # noqa: E402

# Override with SQ8L_TESTAPI when building in another directory (e.g. per-worktree builds).
LIB_PATH = os.environ.get("SQ8L_TESTAPI", os.path.join(ROOT, "build", "libsq8l_testapi.dylib"))

# CplugMaster layout
MASTER_FILTER_TABLE = 0x1004
VOICE_SLOT_DWORDS = 0x3A


def lib():
    L = ctypes.CDLL(LIB_PATH)
    vp, f32, f64, i32 = ctypes.c_void_p, ctypes.c_float, ctypes.c_double, ctypes.c_int32
    sig = {
        "sq8l_filter_unit_tables": ([], ctypes.POINTER(f32)),
        "sq8l_filter_table_new": ([f32], vp),
        "sq8l_filter_table_data": ([vp], ctypes.POINTER(f32)),
        "sq8l_filter_table_free": ([vp], None),
        "sq8l_filter_new": ([vp, f32], vp),
        "sq8l_filter_free": ([vp], None),
        "sq8l_filter_load": ([vp, ctypes.c_char_p], None),
        "sq8l_filter_save": ([vp, ctypes.c_char_p], None),
        "sq8l_filter_process": ([vp, f64], f64),
        "sq8l_filter_set_params": ([vp, i32, i32, i32, i32], None),
    }
    for name, (args, res) in sig.items():
        fn = getattr(L, name)
        fn.argtypes = args
        fn.restype = res
    return L


def started_host(sample_rate=44100.0):
    h = SQ8LHost(sample_rate=sample_rate)
    h.load()
    h.start()
    return h


def master_ptr(h):
    synth = h.emu.u32(h.effect + 64)
    return h.emu.u32(synth + 0xB0)


def floats(b):
    return struct.unpack(f"<{len(b) // 4}f", b)


def f32_bits(x):
    return struct.unpack("<I", struct.pack("<f", x))[0]


def diff_fields(a, b, layout):
    """Compare two byte blobs over named (offset, size) fields; return mismatches."""
    out = []
    for name, off, size in layout:
        if a[off:off + size] != b[off:off + size]:
            out.append(name)
    return out


# A few programs exercising different filter settings (bank C = 256.., bank D = 384..)
TEST_PROGRAMS = [384 + 10, 384 + 3, 384 + 12, 256 + 45, 256 + 82, 256 + 87, 256 + 28, 256 + 0]
TEST_CHORD = [(0.0, 0.6, 48, 100), (0.05, 0.5, 60, 80), (0.1, 0.4, 67, 127), (0.3, 0.3, 79, 60)]
