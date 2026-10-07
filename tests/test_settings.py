"""Differential test: SQ8L.ini settings (CplugConfig) and the master's copy of [synth].

The original reads <dir>/SQ8L/SQ8L.ini with TIniFile.ReadInteger (StrToIntDef). The emulator's
profile API (oracle/winapi.py) is case sensitive, so keys/sections keep their exact spelling
here; the C++ parser is case insensitive like Windows.
"""
import ctypes
import os
import sys

import test_program as tp
from harness import master_ptr, started_host

CONFIG_GLOBAL = 0x489BF8
CONFIG_LOAD = 0x45284C        # CplugConfig_v005
MASTER_READ_SYNTH = 0x4620E4  # copies config synth[0..4] into master +0xfe4..+0xff4

VARIANTS = [
    ("original", open(os.path.join(os.path.dirname(__file__), "..", "original", "SQ8L.ini")).read()),
    ("changed", "[gui]\nrestMouseMenu=0\nrestMouseKnob=0\nkeyCaptMode=-1\ncompareOnWrite=0\n"
                "swapProgUpDn=1\nrmbScrollDisplay=0\n[synth]\nvoiceStealMode=1\nmuffleMode=2\n"
                "oscDcaMode=2\ndca4Mode=1\ndcbMode=3\n"),
    ("missing keys", "[synth]\ndcbMode=2\n"),
    ("empty", ""),
    ("formats", "[gui]\nrestMouseMenu=abc\nrestMouseKnob=\nkeyCaptMode=$1F\ncompareOnWrite=+5\n"
                "swapProgUpDn=1.5\nrmbScrollDisplay=-7\n[synth]\nvoiceStealMode=0x10\nmuffleMode=99999999999\n"
                "oscDcaMode=2147483647\ndca4Mode=-2147483648\ndcbMode=4294967295\n"),
    ("ranges", "[gui]\nrestMouseMenu=$FFFFFFFF\nrestMouseKnob=0x80000000\nkeyCaptMode=2147483648\n"
               "compareOnWrite=-2147483649\nswapProgUpDn=$100000000\nrmbScrollDisplay=-$10\n[synth]\n"
               "voiceStealMode=-0x7FFFFFFF\nmuffleMode=X10\noscDcaMode=-\ndca4Mode=007\ndcbMode=$\n"),
    ("spaces", "[gui]\nrestMouseMenu = 0 \n[synth]\nvoiceStealMode=  1\n"),
    ("missing file", None),
]


def main():
    api = tp.api()
    h = started_host()
    e = h.emu
    cfg = e.u32(CONFIG_GLOBAL)
    master = master_ptr(h)
    path = os.path.join(h.sandbox, "C", "VST", "SQ8L", "SQ8L.ini")
    saved = open(path).read() if os.path.exists(path) else None
    ok = True
    try:
        for name, text in VARIANTS:
            if text is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "w") as f:
                    f.write(text)
            e.call(CONFIG_LOAD, cfg, conv="register")
            want = [e.s32(cfg + 0x20 + 4 * i) for i in range(11)]
            e.call(MASTER_READ_SYNTH, master, conv="register")
            master_vals = [e.s32(master + o) for o in (0xFE4, 0xFEC, 0xFF0, 0xFE8, 0xFF4)]
            gui = (ctypes.c_int32 * 6)()
            synth = (ctypes.c_int32 * 5)()
            t = (text or "").encode("latin1")
            api.sq8l_settings_parse(t, len(t), gui, synth)
            got = list(gui) + list(synth)
            good = want == got and master_vals == list(synth)
            if text is None:
                good &= os.path.exists(path) and os.path.getsize(path) == 0  # created empty
            print(f"settings ({name}):", "OK" if good else f"MISMATCH want {want} got {got} master {master_vals}")
            ok &= good
    finally:
        if saved is not None:
            with open(path, "w") as f:
                f.write(saved)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
