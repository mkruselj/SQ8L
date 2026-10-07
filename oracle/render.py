"""Render MIDI through the original SQ8L.dll (emulated) to a WAV file.

Usage: render.py out.wav [--program N] [--note 60] [--vel 100] [--len 1.0] [--tail 1.0]
"""
import argparse
import sys
import time
import wave

import numpy as np

from vsthost import SQ8LHost, effSetProgram, effGetProgramName


def write_wav(path, left, right, sr):
    data = np.stack([left, right], axis=1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def render_notes(host, notes, total_seconds):
    """notes: list of (start_s, dur_s, key, vel). Returns float32 arrays L, R."""
    sr, blk = host.sr, host.block
    events = []
    for start, dur, key, vel in notes:
        events.append((int(start * sr), bytes([0x90, key, vel])))
        events.append((int((start + dur) * sr), bytes([0x80, key, 0])))
    events.sort(key=lambda x: x[0])
    total = int(total_seconds * sr)
    L, R = [], []
    pos = 0
    while pos < total:
        n = min(blk, total - pos)
        evs = [(t - pos, d) for t, d in events if pos <= t < pos + n]
        if evs:
            host.send_midi(evs)
        l, r = host.process(n)
        L.append(l)
        R.append(r)
        pos += n
    return np.concatenate(L).astype(np.float32), np.concatenate(R).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--program", type=int, default=None)
    ap.add_argument("--note", type=int, default=60)
    ap.add_argument("--vel", type=int, default=100)
    ap.add_argument("--len", type=float, default=1.0)
    ap.add_argument("--tail", type=float, default=1.0)
    ap.add_argument("--sr", type=float, default=44100.0)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()

    h = SQ8LHost(sample_rate=a.sr, verbose=a.v)
    h.load()
    h.start()
    if a.program is not None:
        h.dispatch(effSetProgram, value=a.program)
    print("program:", h.string_op(effGetProgramName), file=sys.stderr)
    t0 = time.time()
    L, R = render_notes(h, [(0.0, a.len, a.note, a.vel)], a.len + a.tail)
    dt = time.time() - t0
    print(f"rendered {len(L)/a.sr:.2f}s in {dt:.2f}s, peak L={np.abs(L).max():.4f} R={np.abs(R).max():.4f}", file=sys.stderr)
    write_wav(a.out, L, R, a.sr)


if __name__ == "__main__":
    main()
