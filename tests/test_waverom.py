"""Differential test: wave ROM data and wavesample lookups."""
import ctypes
import struct
import sys

from harness import lib, master_ptr, started_host

WAVE_PARAMS = 0x455F84    # FUN_00455f84(wave, key, var page, var waveReg)
LFO_LOCATION = 0x45C4F8   # callback(doc, key, wave, var sizeLog2, var ptr)


def main():
    L = lib()
    L.sq8l_waverom_data.restype = ctypes.POINTER(ctypes.c_uint8)
    L.sq8l_waverom_sample.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8)]
    L.sq8l_waverom_lfo_location.argtypes = [ctypes.c_int32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_int32)]
    L.sq8l_waverom_lfo_location.restype = ctypes.c_uint32
    h = started_host()
    e = h.emu
    doc = e.u32(master_ptr(h) + 0x1000)
    rom_base = e.u32(doc + 0x2008)
    ok = bytes(L.sq8l_waverom_data()[:0x40000]) == e.read(rom_base, 0x40000)
    print("wave ROM bytes:", "OK" if ok else "MISMATCH")

    out = e.scratch(16)
    page, reg, size = ctypes.c_uint8(), ctypes.c_uint8(), ctypes.c_int32()
    bad = 0
    for wave in list(range(80)) + [0xFFFFFFFF]:
        for key in range(-2, 131):
            e.call_fpu(WAVE_PARAMS, eax=wave, edx=key, ecx=out, push=(out + 1,))
            L.sq8l_waverom_sample(wave, key, ctypes.byref(page), ctypes.byref(reg))
            if (page.value, reg.value) != tuple(e.read(out, 2)):
                bad += 1
            e.call_fpu(LFO_LOCATION, eax=doc, edx=key, ecx=wave, push=(out + 8, out + 4))
            off = L.sq8l_waverom_lfo_location(key, wave, ctypes.byref(size))
            if (off, size.value) != (e.u32(out + 8) - rom_base, e.s32(out + 4)):
                bad += 1
    print("wavesample lookups:", "OK" if not bad else f"{bad} MISMATCHES")
    ok &= bad == 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
