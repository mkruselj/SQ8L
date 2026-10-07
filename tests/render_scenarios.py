"""MIDI scenarios for full-render differential tests (original vs port).

A scenario is a list of blocks; each block is (nframes, [(delta, bytes3), ...]) plus
optional host actions between blocks: ("program", n), ("chunk", bytes), ("rate", sr).
Both renderers consume exactly the same sequence.
"""
import random

BLOCK = 256


def notes(seq, block=BLOCK, tail_blocks=40):
    """seq: list of (time_s, dur_s, key, vel); returns a block list at 44.1 kHz."""
    sr = 44100
    events = []
    for t, d, k, v in seq:
        events.append((int(t * sr), bytes([0x90, k, v])))
        events.append((int((t + d) * sr), bytes([0x80, k, 0])))
    return _blocks(events, block, tail_blocks)


def _blocks(events, block, tail_blocks):
    events.sort(key=lambda x: x[0])
    end = (events[-1][0] if events else 0) // block + 1 + tail_blocks
    out = []
    for b in range(end):
        lo = b * block
        out.append((block, [(t - lo, d) for t, d in events if lo <= t < lo + block]))
    return out


def chord_and_melody():
    return notes([(0.0, 0.8, 48, 100), (0.0, 0.8, 55, 90), (0.0, 0.8, 64, 80),
                  (0.9, 0.2, 72, 127), (1.15, 0.2, 74, 64), (1.4, 0.5, 76, 30)])


def stealing():
    rng = random.Random(7)
    return notes([(i * 0.05, 0.6, rng.randint(36, 96), rng.randint(1, 127)) for i in range(20)])


def legato():
    return notes([(0.0, 0.3, 60, 100), (0.25, 0.3, 64, 100), (0.5, 0.3, 67, 100), (0.75, 0.6, 72, 100)])


def controllers():
    sr, ev = 44100, []
    ev.append((0, bytes([0x90, 60, 100])))
    ev.append((0, bytes([0x90, 67, 90])))
    for i in range(80):
        t = int(i * 0.01 * sr)
        ev.append((t, bytes([0xE0, 0, (64 + int(40 * __import__("math").sin(i / 7))) & 0x7F])))
        ev.append((t + 50, bytes([0xB0, 1, (i * 3) % 128])))
        ev.append((t + 90, bytes([0xD0, (i * 5) % 128, 0])))
        ev.append((t + 120, bytes([0xA0, 60, (i * 7) % 128])))
        ev.append((t + 150, bytes([0xB0, 74, (i * 11) % 128])))
    ev.append((int(0.9 * sr), bytes([0xB0, 64, 127])))
    ev.append((int(1.0 * sr), bytes([0x80, 60, 0])))
    ev.append((int(1.0 * sr), bytes([0x80, 67, 0])))
    ev.append((int(1.6 * sr), bytes([0xB0, 64, 0])))
    ev.append((int(1.7 * sr), bytes([0xB0, 7, 90])))
    ev.append((int(1.8 * sr), bytes([0xB0, 10, 20])))
    return _blocks(ev, BLOCK, 60)


def random_play(seed):
    rng = random.Random(seed)
    sr, ev = 44100, []
    for _ in range(rng.randint(5, 30)):
        t = int(rng.uniform(0, 2.0) * sr)
        k, v = rng.randint(0, 127), rng.randint(1, 127)
        ev.append((t, bytes([0x90, k, v])))
        ev.append((t + int(rng.uniform(0.01, 1.0) * sr), bytes([0x80, k, rng.randint(0, 127)])))
    for _ in range(rng.randint(0, 40)):
        t = int(rng.uniform(0, 2.5) * sr)
        kind = rng.choice([0xB0, 0xD0, 0xE0, 0xA0])
        cc = rng.choice([1, 2, 4, 7, 10, 11, 64, 74, rng.randint(0, 127)])
        ev.append((t, bytes([kind, cc if kind == 0xB0 else rng.randint(0, 127), rng.randint(0, 127)])))
    return _blocks(ev, rng.choice([32, 64, 256, 512]), 40)


SCENARIOS = {
    "chord": chord_and_melody,
    "steal": stealing,
    "legato": legato,
    "controllers": controllers,
}


def all_scenarios(n_random=3):
    d = dict(SCENARIOS)
    for i in range(n_random):
        d[f"random{i}"] = (lambda i=i: random_play(1000 + i))
    return d
