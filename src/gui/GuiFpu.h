// x87 helpers for the GUI code. The original's GUI runs with the host control word:
// round to nearest, 53-bit precision, so x87 arithmetic == IEEE double arithmetic and
// Delphi Round() == round half to even. Implemented explicitly so the result does not depend
// on the caller's floating point environment. Compile without FMA contraction.
#pragma once

#include <cmath>
#include <cstdint>

namespace sq8l::gui::fpu {

// Delphi Round() (FISTP, round to nearest even) of a value already rounded to Single.
inline int32_t roundEven(double x) {
    double r = std::floor(x);
    double d = x - r;
    if (d > 0.5 || (d == 0.5 && std::fmod(r, 2.0) != 0.0)) r += 1.0;
    return static_cast<int32_t>(r);
}

// FLD tbyte c; FMUL dword x at 53-bit precision: the exact product rounded once.
// c = hi + lo exactly (an 80-bit constant split in two doubles).
inline double mulExt(double hi, double lo, double x) { return std::fma(hi, x, lo * x); }

// 0x47aca4: extended 0.04 (snap zone of TGraphKnobB).
constexpr double kExt004Hi = 0x1.47ae147ae147bp-5;
constexpr double kExt004Lo = -0x1.ecp-61;

}  // namespace sq8l::gui::fpu
