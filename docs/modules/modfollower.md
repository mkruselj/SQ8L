# Mod follower (glide): unit `mod_foll`, class `Cmod_foll` -> `sq8l::ModFollower`

Code range 0x45eaf0-0x45ed38. C++: `src/engine/ModFollower.{h,cpp}`. Test:
`tests/test_modfollower.py` (C API in `tests/capi_env.cpp`). Bit-exact.

A linear ramp toward a target, 20.12 fixed point, one per voice slot (slot + 0x8c). plugCore
uses it only for glide (portamento): the pitch offset in 1/256 semitone.

## Object layout (32 bytes)

| off | type | C++ | meaning |
|---|---|---|---|
| +0x00 | ptr | - | VMT |
| +0x04 | float | `rate` | control rate (< 1 or NaN -> 1.0) |
| +0x08 | float | `speed` | read by setRate as the speed to re-apply, **never written** (always 0.0) |
| +0x0c | float | `stepScale` | Single(4096 / rate) |
| +0x10 | int | `stepSize` | Round(Single(speed * stepScale)) |
| +0x14 | int | `value` | current value, 20.12 |
| +0x18 | int | `step` | signed step per tick, 0 = idle |
| +0x1c | int | `target` | 20.12 |

## Routines

| original | C++ | |
|---|---|---|
| FUN_0045eb28 constructor | `init(rate)` | zero-fill, `setRate(rate)`, `setSpeed(0)` |
| FUN_0045eba0 | `setRate(rate)` | rate, stepScale, then `setSpeed(speed field)` (= 0: stops a running ramp's step) |
| FUN_0045ebe8 | `setSpeed(unitsPerSecond)` | `stepSize = Round(Single(s * stepScale))` (current rounding mode, x87 integer-indefinite 0x80000000 when out of range); a running step keeps its sign |
| FUN_0045ec1c | `setTarget(t)` | t clamped to +-0x7ffff, `<< 12`; step = +-stepSize toward it (0 if already there) |
| FUN_0045ec68 | `reset(v)` | value = clamp(v) << 12, step = 0 (target unchanged) |
| FUN_0045ec90 | `tick()` -> int | if value != target: value += step, clamp at the target (then step = 0); returns value / 4096 toward zero |
| (plugCore) | `static glideSpeed(keyDelta, glide)` | the speed argument computed by the callers, see below |

Quirks reproduced: `tick()` with `step == 0` and `value > target` jumps to the target; with
`value < target` it stays forever. Arithmetic wraps on 32 bits (never in practice).

## How plugCore drives it

* Construction (FUN_00461cac): `init(83.592575f)` per slot; FUN_00462380 then calls
  `setRate(83.592575f)` on all.
* Note on (FUN_004625d4) with `fromKey` (glide source key passed by the voice allocator, clamped
  to 0..127; when negative it is replaced by the new key, i.e. no glide):
  * if patch GLIDE (patch+0x173) < 1 or `key == fromKey`: slot+0x84 = 0, `reset(0)`;
  * else slot+0x84 = -1, slot+0x88 = fromKey, `setSpeed(glideSpeed(key - fromKey, GLIDE))`,
    `reset(0)`, `setTarget((key - fromKey) * 256)`, and the oscillators start at fromKey
    (FUN_0045c460).
* Key change of a sounding voice (FUN_00462ca0, legato): if GLIDE < 1 or the key equals the
  voice's current key (slot+0x14): slot+0x84 = 0, `reset(0)`; else slot+0x84 = -1,
  `setSpeed(glideSpeed(key - currentKey, GLIDE))`, `setTarget((key - slot+0x88) * 256)` (no reset:
  the ramp continues from where it is; the target stays relative to the glide start key
  slot+0x88 while the speed uses the distance from the current key - quirk).
* Control tick (FUN_00463890): pitch offset = slot+0x84 ? `tick()` : 0.

`glideSpeed(keyDelta, glide)` = `Single(|keyDelta| * 256 * 83.59257598 / kEnvTimeTable[glide])`
(FILD; FLD tbyte; FMULP; FILD word; FDIVP; FSTP single at 53-bit precision, current rounding mode:
round toward zero inside process). It uses the envelope time table (`EnvData.cpp`), so a glide
of distance d takes `kEnvTimeTable[GLIDE]` control ticks. The original indexes the table without
clamping (GLIDE is 0..63 in the UI); the C++ clamps to 0..63.

## Verification

`tests/test_modfollower.py`: every call of the 6 entry points captured while playing
mono/glide/legato programs, pedal sequences, a sample of factory programs and random patches with
random GLIDE/MONO is replayed on the C++ object (outputs and bytes 4..0x1f identical); the glide
speed argument is recomputed from the caller's registers and compared bit for bit. Then 20000
direct calls on random states and arguments (overflow, clamping, NaN/inf/huge speeds and rates,
both rounding modes).

Full run (~2.5 min, `--quick` ~1.5 min), all bit-exact: real play ctor 16, setRate 48, setSpeed
222 (158 glide speeds recomputed exactly; note-on calls run with round toward zero), reset 484,
setTarget 158, tick 12771; direct calls: tick ~8000, setSpeed ~4000, setTarget ~3900, setRate
~2000, reset ~1900. The +0x08 speed field was 0 in every captured state.

Coverage: every instruction executed. Never reached by real play (only by the direct calls):
the rate < 1 clamp, the +-0x7ffff clamps of setTarget/reset, and setTarget with the target
equal to the current value (plugCore only glides between different keys).
