"""Render scenarios through the emulated original and cache the result (.npz) so that
the C++ side can be compared quickly and repeatedly.

Usage: oracle_render.py [--programs all|quick] [--force]
Output: tests/golden/<program>_<scenario>.npz  (float32 L/R exactly as the original wrote them)
"""
import argparse
import os
import struct
import sys
import time

import numpy as np

from harness import ROOT, SQ8LHost, effSetProgram
from render_scenarios import all_scenarios

GOLDEN = os.path.join(ROOT, "tests", "golden")
QUICK = [384 + 10, 384 + 3, 384 + 12, 384 + 33, 256 + 0, 256 + 13, 256 + 28, 256 + 45, 256 + 82, 256 + 87]


def render_original(program, blocks):
    h = SQ8LHost(block_size=max(n for n, _ in blocks))
    h.load()
    h.start()
    h.dispatch(effSetProgram, value=program)
    L, R = [], []
    for n, events in blocks:
        if events:
            h.send_midi(events)
        l, r = h.process(n)
        L.append(np.array(l, np.float32))
        R.append(np.array(r, np.float32))
    return np.concatenate(L), np.concatenate(R)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--programs", default="quick")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(GOLDEN, exist_ok=True)
    progs = QUICK if a.programs == "quick" else list(range(256, 384)) + list(range(384, 424))
    scen = all_scenarios()
    t0 = time.time()
    for p in progs:
        for name, make in scen.items():
            path = os.path.join(GOLDEN, f"{p}_{name}.npz")
            if os.path.exists(path) and not a.force:
                continue
            blocks = make()
            L, R = render_original(p, blocks)
            np.savez_compressed(path, L=L, R=R, blocks=np.array([n for n, _ in blocks]))
            print(f"{p} {name}: {len(L)} samples, peak {max(np.abs(L).max(), np.abs(R).max()):.3f} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
