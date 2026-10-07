"""Differential test: CMidiParser (emulated) vs sq8l::MidiParser (C++).

Random VstMidiEvent lists (including out-of-order deltaFrames, running notes, CCs,
pressure, pitch bend, system messages) are fed to both; we compare the record
array after processEvents and the sequence of dispatched callbacks with the
sample index at which they fire."""
import ctypes
import random
import struct
import sys

from harness import lib, master_ptr, started_host

PARSER_PROCESS_EVENTS = 0x44F93C
TICK = 0x44FE88
NOTE_ON_CB = 0x463420       # master handler reached from the parser's note callback
CTRL_CB = 0x463714
RESET_CB = 0x462174


class Raw(ctypes.Structure):
    _fields_ = [("deltaFrames", ctypes.c_int32), ("data", ctypes.c_uint8 * 3), ("noteOffVelocity", ctypes.c_uint8)]


def random_events(rng, block):
    evs = []
    for _ in range(rng.randint(0, 40)):
        kind = rng.choice([0x80, 0x90, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF8, 0xFC, 0xFF])
        st = kind if kind >= 0xF0 else kind | rng.randint(0, 15)
        d1, d2 = rng.randint(0, 127), rng.choice([0, rng.randint(0, 127)])
        delta = rng.randint(0, block - 1)
        evs.append((delta, bytes([st, d1, d2]), rng.randint(0, 127)))
    if rng.random() < 0.5:
        evs.sort(key=lambda x: x[0])  # mostly ordered, sometimes not
    return evs


def main():
    L = lib()
    L.sq8l_midi_new.restype = ctypes.c_void_p
    L.sq8l_midi_process_events.argtypes = [ctypes.c_void_p, ctypes.POINTER(Raw), ctypes.c_int32]
    L.sq8l_midi_records.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
    L.sq8l_midi_run_block.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int32), ctypes.c_int32]
    h = started_host()
    e = h.emu
    parser = e.u32(master_ptr(h) + 0xFFC)
    box = L.sq8l_midi_new()
    rng = random.Random(1234)
    block = 64
    bad = 0
    total_callbacks = 0
    for it in range(300):
        evs = random_events(rng, block)
        # original: processEvents through the plugin dispatcher, then capture records
        # build VstEvents manually (send_midi doesn't carry noteOffVelocity)
        base = h.events_buf
        e.write(base, struct.pack("<iI", len(evs), 0))
        evbase = base + 8 + 4 * 256
        for i, (d, b, nov) in enumerate(evs):
            ev = evbase + 32 * i
            e.write(ev, struct.pack("<iiiiii", 1, 32, d, 0, 0, 0) + b + b"\0" + struct.pack("<bbbb", 0, nov, 0, 0))
            e.w32(base + 8 + 4 * i, ev)
        e.call_fpu(PARSER_PROCESS_EVENTS, eax=parser, edx=base)
        n = e.s32(parser + 8)
        orig_records = e.read(e.u32(parser + 4), 16 * n) if n else b""
        arr = (Raw * max(len(evs), 1))()
        for i, (d, b, nov) in enumerate(evs):
            arr[i].deltaFrames = d
            arr[i].data[:] = list(b)
            arr[i].noteOffVelocity = nov
        L.sq8l_midi_process_events(box, arr, len(evs))
        buf = ctypes.create_string_buffer(16 * 1024)
        m = L.sq8l_midi_records(box, buf, 1024)
        if m != n or buf.raw[:16 * n] != orig_records:
            bad += 1
            if bad <= 3:
                print(f"iter {it}: records differ (orig {n}, ours {m})")
            continue
        # dispatch: drive the original parser tick by tick, logging master callbacks
        log = []
        cur_tick = [0]

        def mk(kind):
            def enter(emu):
                r = emu.regs()
                if kind == 1:
                    log.extend([cur_tick[0], 1, 0, r["edx"] & 0xFF, r["ecx"] & 0xFF, 0])
                elif kind == 8:
                    esp = r["esp"]
                    # Delphi pushes stack args left to right: value first ([esp+8]), ctrl last ([esp+4])...
                    # but observed: ctrl at [esp+8], value at [esp+4] (see PORTING_GUIDE on Ghidra order)
                    v, c = emu.s32(esp + 4) & 0xFFFF, emu.s32(esp + 8) & 0xFFFF
                    log.extend([cur_tick[0], 8, r["edx"] & 0xFF, r["ecx"] & 0xFF,
                                v - 0x10000 if v & 0x8000 else v, c - 0x10000 if c & 0x8000 else c])
                else:
                    log.extend([cur_tick[0], 0x40, 0, 0, 0, 0])
                return None
            return enter

        hooks = [e.trace(NOTE_ON_CB, mk(1), lambda emu, c: None), e.trace(CTRL_CB, mk(8), lambda emu, c: None)]
        e.call_fpu(0x44FCA8, eax=parser)
        for i in range(block):
            cur_tick[0] = i
            e.call_fpu(TICK, eax=parser)
        cur_tick[0] = block
        e.call_fpu(0x44F7FC, eax=parser)
        for hk in hooks:
            e.untrace(hk)
        out = (ctypes.c_int32 * 4096)()
        k = L.sq8l_midi_run_block(box, block, out, 4096)
        ours = list(out[:k])
        # note callbacks in the original go through the master's note-on handler with (key, vel):
        ours_norm = []
        for j in range(0, len(ours), 6):
            t, kind, ch, a, b, c = ours[j:j + 6]
            if kind == 1:
                ours_norm.extend([t, 1, 0, a, b, 0])
            elif kind == 8:
                ours_norm.extend([t, 8, ch, a, b, c])
        total_callbacks += len(log) // 6
        if ours_norm != log:
            bad += 1
            if bad <= 3:
                print(f"iter {it}: dispatch differs\n  orig {log[:36]}\n  ours {ours_norm[:36]}")
    print(f"300 random blocks, {total_callbacks} callbacks compared: {'OK' if not bad else f'{bad} FAILURES'}")
    print("PASS" if not bad else "FAIL")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
