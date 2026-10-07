"""Export golden renders + their MIDI scenarios to a raw format for the native checker
(tests/render_check.cpp): golden_raw/<prog>_<scen>.bin =
  int32 program, int32 nblocks, then per block: int32 n, int32 nev, nev*(int32 delta, 3 bytes, pad),
  then float32 L[total], float32 R[total]."""
import glob
import os
import struct

import numpy as np

from harness import ROOT
from oracle_render import GOLDEN
from render_scenarios import all_scenarios

OUT = os.path.join(ROOT, "tests", "golden_raw")


def main():
    os.makedirs(OUT, exist_ok=True)
    scen = all_scenarios()
    for f in sorted(glob.glob(os.path.join(GOLDEN, "*.npz"))):
        prog, name = os.path.basename(f)[:-4].split("_", 1)
        g = np.load(f)
        blocks = scen[name]()
        out = bytearray(struct.pack("<ii", int(prog), len(blocks)))
        for n, evs in blocks:
            out += struct.pack("<ii", n, len(evs))
            for d, b in evs:
                out += struct.pack("<i", d) + bytes(b) + bytes(4 - len(b))
        out += g["L"].astype("<f4").tobytes() + g["R"].astype("<f4").tobytes()
        open(os.path.join(OUT, f"{prog}_{name}.bin"), "wb").write(out)
    print(len(glob.glob(os.path.join(OUT, "*.bin"))), "files")


if __name__ == "__main__":
    main()
