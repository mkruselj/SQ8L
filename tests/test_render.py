"""Full-render differential test: the complete C++ engine vs golden renders of the
original (tests/golden, produced by oracle_render.py), sample by sample.

Usage: test_render.py [--programs quick|all] [--wav DIR]
"""
import argparse
import ctypes
import glob
import os
import sys

import numpy as np

from harness import LIB_PATH, ROOT
from oracle_render import GOLDEN
from render_scenarios import all_scenarios


class Raw(ctypes.Structure):
    _fields_ = [("deltaFrames", ctypes.c_int32), ("data", ctypes.c_uint8 * 3), ("noteOffVelocity", ctypes.c_uint8)]


def lib():
    L = ctypes.CDLL(LIB_PATH)
    L.sq8l_synth_new.restype = ctypes.c_void_p
    L.sq8l_synth_new.argtypes = [ctypes.c_float]
    L.sq8l_synth_free.argtypes = [ctypes.c_void_p]
    L.sq8l_synth_set_program.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    L.sq8l_synth_events.argtypes = [ctypes.c_void_p, ctypes.POINTER(Raw), ctypes.c_int32]
    L.sq8l_synth_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
                                     ctypes.c_int32, ctypes.c_int32]
    return L


def render_port(L, program, blocks):
    s = L.sq8l_synth_new(44100.0)
    L.sq8l_synth_set_program(s, program)
    outL, outR = [], []
    for n, events in blocks:
        if events:
            arr = (Raw * len(events))()
            for i, (d, b) in enumerate(events):
                arr[i].deltaFrames = d
                arr[i].data[:] = list(bytes(b) + bytes(3 - len(b)))
                arr[i].noteOffVelocity = 0
            L.sq8l_synth_events(s, arr, len(events))
        l = (ctypes.c_float * n)()
        r = (ctypes.c_float * n)()
        L.sq8l_synth_process(s, l, r, n, 1)
        outL.append(np.frombuffer(l, np.float32).copy())
        outR.append(np.frombuffer(r, np.float32).copy())
    L.sq8l_synth_free(s)
    return np.concatenate(outL), np.concatenate(outR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=None, help="write port renders as WAV into this directory")
    a = ap.parse_args()
    L = lib()
    scen = all_scenarios()
    files = sorted(glob.glob(os.path.join(GOLDEN, "*.npz")))
    if not files:
        print("no golden renders: run tests/oracle_render.py first")
        return 1
    bad = 0
    total = 0
    for f in files:
        prog, name = os.path.basename(f)[:-4].split("_", 1)
        g = np.load(f)
        blocks = scen[name]()
        pl, pr = render_port(L, int(prog), blocks)
        total += len(pl)
        same = np.array_equal(pl.view(np.uint32), g["L"].view(np.uint32)) and \
            np.array_equal(pr.view(np.uint32), g["R"].view(np.uint32))
        if same:
            status = "bit-exact"
        else:
            bad += 1
            d = np.abs(np.concatenate([pl - g["L"], pr - g["R"]]))
            first = int(np.argmax((pl != g["L"]) | (pr != g["R"])))
            status = f"DIFF first at sample {first}, max |d| {d.max():.3g}, differing {int((d > 0).sum())}"
        print(f"{prog:>4} {name:12} {len(pl):7d} samples  {status}")
        if a.wav:
            import wave
            os.makedirs(a.wav, exist_ok=True)
            pcm = (np.clip(np.stack([pl, pr], 1), -1, 1) * 32767).astype("<i2")
            with wave.open(os.path.join(a.wav, f"{prog}_{name}.wav"), "wb") as w:
                w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100), w.writeframes(pcm.tobytes())
    print(f"{len(files) - bad}/{len(files)} renders bit-exact ({total} samples per channel)")
    print("PASS" if not bad else "FAIL")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
