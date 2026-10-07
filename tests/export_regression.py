"""Export a self-contained regression set (no original DLL needed to run it):
tests/regression/cases/<prog>_<scen>.bin  = MIDI blocks (golden_raw format without audio)
tests/regression/expected.txt            = "<case> <samples> <fnv1a64 of the ORIGINAL's float32 L then R>"
Every program of banks C and D with the "chord" scenario, plus all scenarios for a few programs."""
import glob
import os
import struct

import numpy as np

from harness import ROOT
from oracle_render import GOLDEN, QUICK
from render_scenarios import all_scenarios

OUT = os.path.join(ROOT, "tests", "regression")


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def main():
    os.makedirs(os.path.join(OUT, "cases"), exist_ok=True)
    scen = all_scenarios()
    lines = []
    for f in sorted(glob.glob(os.path.join(GOLDEN, "*.npz"))):
        prog, name = os.path.basename(f)[:-4].split("_", 1)
        if not (name == "chord" or int(prog) in QUICK):
            continue
        g = np.load(f)
        blocks = scen[name]()
        out = bytearray(struct.pack("<ii", int(prog), len(blocks)))
        for n, evs in blocks:
            out += struct.pack("<ii", n, len(evs))
            for d, b in evs:
                out += struct.pack("<i", d) + bytes(b) + bytes(4 - len(b))
        case = f"{prog}_{name}.bin"
        open(os.path.join(OUT, "cases", case), "wb").write(out)
        audio = g["L"].astype("<f4").tobytes() + g["R"].astype("<f4").tobytes()
        lines.append(f"{case} {len(g['L'])} {fnv1a64(audio):016x}")
    with open(os.path.join(OUT, "expected.txt"), "w") as fh:
        fh.write("# case samples fnv1a64(original output: float32 L then R)\n" + "\n".join(lines) + "\n")
    print(len(lines), "cases")


if __name__ == "__main__":
    main()
