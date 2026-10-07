"""Dump the Delphi unit initialization table of SQ8L.dll (InitTable at 0x4882b8) sorted by
address -> re/extracted/inittable.txt (unit init/finalization procedure addresses)."""
import os
import struct

import pefile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    pe = pefile.PE(os.path.join(ROOT, "original", "SQ8L.dll"))
    ib = pe.OPTIONAL_HEADER.ImageBase
    img = pe.get_memory_mapped_image()
    u32 = lambda va: struct.unpack_from("<I", img, va - ib)[0]
    cnt, tbl = u32(0x4882B8), u32(0x4882BC)
    rows = sorted((u32(tbl + 8 * i), u32(tbl + 8 * i + 4), i) for i in range(cnt))
    os.makedirs(os.path.join(ROOT, "re", "extracted"), exist_ok=True)
    with open(os.path.join(ROOT, "re", "extracted", "inittable.txt"), "w") as f:
        f.write("# Delphi unit init/finalization procedures (InitTable at 0x4882c0), sorted by address\n# index init fin\n")
        for a, b, i in rows:
            f.write(f"{i:3d} {a:#x} {b:#x}\n")
    print(len(rows), "entries")


if __name__ == "__main__":
    main()
