#include "ModFollower.h"

#include <cmath>
#include <cstdint>

#include "Env.h"
#include "Fpu.h"

namespace sq8l {

namespace {

// 80-bit constant at 0x462bf8 / 0x462d98 (83.59257598), split hi/lo (see Fpu.h).
constexpr Ext kControlRate = {0x1.4e5ecc3cd9fb1p+6, -0x1.d800000000000p-48};

inline int32_t clamp20(int32_t v) { return v > 0x7ffff ? 0x7ffff : (v < -0x7ffff ? -0x7ffff : v); }
inline int32_t shl12(int32_t v) { return static_cast<int32_t>(static_cast<uint32_t>(v) << 12); }
inline int32_t neg32(int32_t v) { return static_cast<int32_t>(0u - static_cast<uint32_t>(v)); }
inline int32_t add32(int32_t a, int32_t b) {
    return static_cast<int32_t>(static_cast<uint32_t>(a) + static_cast<uint32_t>(b));
}

// x87 FISTP (Delphi Round, 0x4175c0): current rounding mode, integer indefinite when out of range.
inline int32_t fistp32(double x) {
    const double r = std::nearbyint(x);
    if (!(r >= -2147483648.0 && r <= 2147483647.0)) return INT32_MIN;
    return static_cast<int32_t>(r);
}

}  // namespace

void ModFollower::init(float controlRate) {
    *this = ModFollower{};  // TObject.Create zero-fills the instance
    setRate(controlRate);
    setSpeed(0.0f);
}

void ModFollower::setRate(float controlRate) {
    // FCOMP + JC: below 1.0 or unordered -> 1.0
    rate = (controlRate >= 1.0f) ? controlRate : 1.0f;
    stepScale = sgl(4096.0 / static_cast<double>(rate));
    setSpeed(speed);
}

void ModFollower::setSpeed(float unitsPerSecond) {
    const int32_t s = fistp32(sgl(static_cast<double>(unitsPerSecond) * stepScale));
    stepSize = s;
    step = step < 0 ? neg32(s) : s;
}

void ModFollower::setTarget(int32_t t) {
    t = shl12(clamp20(t));
    if (t == value) step = 0;
    else if (value < t) step = stepSize;
    else step = neg32(stepSize);
    target = t;
}

void ModFollower::reset(int32_t v) {
    step = 0;
    value = shl12(clamp20(v));
}

int32_t ModFollower::tick() {
    if (value != target) {
        value = add32(value, step);
        if (step >= 0) {
            if (target <= value) {
                value = target;
                step = 0;
            }
        } else if (target >= value) {
            value = target;
            step = 0;
        }
    }
    return value / 4096;  // test / jns / add 0xfff / sar 12
}

float ModFollower::glideSpeed(int32_t keyDelta, int32_t glideTime) {
    // CDQ / XOR / SUB (abs, wraps for INT_MIN), SHL 8, FILD
    const uint32_t mag = keyDelta < 0 ? 0u - static_cast<uint32_t>(keyDelta) : static_cast<uint32_t>(keyDelta);
    const int32_t x = static_cast<int32_t>(mag << 8);
    if (glideTime < 0) glideTime = 0;
    else if (glideTime > 63) glideTime = 63;
    const double num = mulExt(kControlRate, static_cast<double>(x));  // FLD tbyte; FMULP
    return sgl(num / static_cast<double>(data::kEnvTimeTable[glideTime]));  // FILD word; FDIVP; FSTP
}

}  // namespace sq8l
