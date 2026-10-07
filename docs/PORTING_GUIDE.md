# SQ8L porting guide

Goal: a faithful, **bit-exact** C++ port of the SQ8L 0.91b VST (Delphi 5, Win32) DSP engine.
Every ported routine is verified against the original binary running inside an x86
emulator (Unicorn) on real captured states.

## Repository map

| Path | What |
|---|---|
| `original/SQ8L.dll` | the original plugin (never modified) |
| `re/decomp/<unit>.c` / `.asm` | Ghidra decompilation and disassembly, one file per Delphi unit |
| `re/extracted/classes.json` | recovered Delphi classes: VMT, parent, instance size, virtual methods |
| `re/scripts/disasm.py ADDR [N]` | quick capstone disassembly of the DLL at an address |
| `oracle/` | the emulator: `w32emu.py` (Unicorn PE loader + Win32 subset), `vsthost.py`, `render.py` |
| `src/engine/` | the C++ engine (portable, no framework dependency) |
| `tests/capi*.cpp` | C API exposing engine internals to Python tests (compiled into `libsq8l_testapi.dylib`) |
| `tests/test_*.py` | differential tests: original (emulated) vs C++ |

Reference example for everything below: `src/engine/FilterSQ.{h,cpp}`, `tests/capi.cpp`,
`tests/test_filter.py`.

## Floating point: how to be bit-exact

* The original computes on the x87 FPU. Inside `processReplacing` it sets the control word to
  host CW | 0xC00 → **round toward zero, 53-bit precision** (CW 0x0E7F). At load time and in
  `setSampleRate` the CW is the host's: round to nearest, 53-bit (0x027F).
* x87 at 53-bit precision == IEEE double arithmetic. So:
  * a value kept on the x87 stack → C++ `double`;
  * a value stored to a Delphi `Single` (FSTP dword) → `float`, via `sgl(x)`;
  * code reached from `process` runs under `RoundTowardZero` (the C API wraps calls);
    init/table code runs under `RoundToNearest`.
* Follow the **exact operation order** of the asm (associativity matters). `a*b` of two floats is
  exact in double; sums/products of doubles round once, as on x87.
* 80-bit constants (`FLD tbyte [const]`) → `Ext{hi, lo}` and `mulExt(c, x)` (see `Fpu.h`); print
  the split with a small Python snippet (see git history of FilterSQ for the generator).
* Delphi `Round()` = `fistp()` (uses current mode: truncates inside process!). `Trunc()` = `trunc32()`.
* Transcendentals (`Exp` 0x402864, `Sin` 0x40287c, `Cos` 0x402854, `Tan` 0x415308,
  `Power` 0x4176cc = exp(e*ln(b)) for non-integral e) → libm; so far they always matched after
  rounding to float.
* Build flags (CMake) forbid FMA contraction and honor the rounding mode. Don't fight them.

## Delphi 5 conventions you will meet in the asm

* `register` calling convention: Self/1st arg in EAX, then EDX, ECX; remaining args pushed
  **left to right** (the last declared arg is at `[ebp+8]`); callee pops (`ret N`).
  Floating point args are always on the stack. Single/Double/Extended results return in ST0.
* Some DSP routines are hand-written asm that take/return audio samples **on the FPU stack**
  (e.g. filter `process`: input in ST0, output in ST0). Ghidra shows these as `in_ST0` — read
  the `.asm`, not the `.c`, for anything floating point.
* **Ghidra lists Delphi stack parameters in reverse order.** The x86delphi cspec models the
  register args (EAX, EDX, ECX) but treats stack args like stdcall, so in `re/decomp/*.c` a call
  `f(a, b, c, x, y)` with two stack args really has `x` and `y` swapped. Trust the asm (pushes)
  and traced values.
* Nested procedures receive the parent frame pointer as an extra pushed arg.
* Constants are often stored inside the code section right after a function (Ghidra may show
  them as bogus `FUN_` functions). Read them with `pefile` from `original/SQ8L.dll`.
* Object layout: `+0` is the VMT pointer; fields follow. Class sizes are in `classes.json`.
* RTL helpers already identified: 0x402864 Exp, 0x40287c Sin, 0x402854 Cos, 0x415308 Tan,
  0x4176cc Power(Base, Exponent), 0x4175c0 fistp(single)→int, 0x417640 Trunc(single),
  0x41757c Get8087CW, 0x417598 set CW|0xC00, 0x41758c Set8087CW, 0x402a40 FillChar,
  0x402d64 TObject.Create, 0x402d94 TObject.Free, 0x40c390 FreeAndNil, 0x4026dc GetMem,
  0x4026f4 FreeMem, 0x403094/0x4030ec/0x4030f4/0x4030e4 class create/destroy plumbing,
  0x40358c try/finally plumbing.

## Engine structure (CplugMaster, unit plugCore)

* `CSynth.processReplacing` → `FUN_004645c8(master, replacing, outL, n, outR)`.
* Per sample: MIDI parser tick, then for each active voice `FUN_00464410(master, voice, &out)`:
  control-rate update (`FUN_00463890`) every `master+0xf80`/1024 samples, then
  `Cdoc render (FUN_0045c7f4) → filter process (FUN_0045f578) → amp (FUN_0045e8ec / FUN_0045e95c)`
  passing the sample on the FPU stack. Optional "muffle" biquad, master volume.
* 16 voice slots of 0x3a dwords each at `master + (i*0x3a + 1)*4`; per slot 4 LFOs (+0x14..),
  4 envelopes, 2 filters (+0x74/+0x78), 1 amp (+0x7c), 1 mod follower.
* Control rate objects are constructed with rate 83.592575 Hz.

## Differential testing recipe

1. Find the module's entry points and who calls them (`// callers:` lines in `re/decomp/*.c`).
2. Write the C++ class with one field per original field, commented with its offset.
3. In `tests/capi_<module>.cpp` provide `new/free`, `load(bytes)`/`save(bytes)` mapping fields
   by offset (see `filterFields`), and wrappers for each method (wrapping in the right rounding
   mode).
4. In `tests/test_<module>.py`: start the emulated plugin (`harness.started_host()`), install
   `emu.trace(entry, on_enter, on_exit)` hooks on the original routines, play
   `TEST_PROGRAMS`/`TEST_CHORD` (add your own programs/notes to reach all code paths),
   capture object bytes before/after plus register/FPU inputs and outputs, then replay each
   captured call on the C++ object and require identical outputs and identical state bytes.
   `emu.read_st(0)` reads ST0 exactly; `emu.call_fpu(...)` calls a routine directly with
   register/stack/FPU inputs (see `tests/test_waverom.py`).
5. Report coverage: number of captured calls per routine, and make sure rarely hit branches
   are hit (pick programs that use the feature; patch parameters live in the edit buffer).

Build and run (use your own build dir if several people build concurrently):

```
cmake -S . -B build -G Ninja && cmake --build build
SQ8L_TESTAPI=$PWD/build/libsq8l_testapi.dylib /path/to/sq8l-port/.venv/bin/python tests/test_<module>.py
```

## Data

`re/scripts/extract_data.py` generates `src/engine/data/*.cpp` (wave ROM as decompressed by
the original, wavesample tables). Static tables that the original computes at load time
should be **computed** in C++ and verified against the emulator memory; embed only when the
original's data is not derivable (e.g. ROM contents, factory sounds).
