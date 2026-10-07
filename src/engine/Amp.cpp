#include "Amp.h"

#include <cmath>
#include <cstdint>

#include "Fpu.h"

namespace sq8l {

namespace {

// 80-bit constants from the original code, split hi/lo (see Fpu.h).
constexpr Ext kOneOver63 = {0x1.0410410410410p-6, 0x1.0400000000000p-60};  // 0x45e3f0
constexpr Ext kGainScale = {0x1.3a415f45e0b4ep+0, 0x1.1e00000000000p-56};  // 0x45e7a0: 1.22756

// Comparisons of a Single against 80-bit constants, expressed on Singles:
// x < 0.1 (ext, 0x45e400)  <=>  x < 0.1f (0.1f is the first Single above it);
// x < 1e-6 (ext, 0x45e7ac) <=>  x <= 0x1.0c6f7ap-20f (the last Single below it).
constexpr float kMinDrive = 0x1.99999ap-4f;    // 0x3dcccccd, also the replacement value
constexpr float kMinTarget = 0x1.0c6f7ap-20f;  // 0x358637bd, also the replacement value

// FISTP dword (Delphi Round, current rounding mode), including the x87 "integer indefinite"
// result 0x80000000 for out-of-range values and NaN.
int32_t fistp32(double x) {
    const double r = std::nearbyint(x);
    return (r >= -2147483648.0 && r < 2147483648.0) ? static_cast<int32_t>(r) : INT32_MIN;
}

// Constant-power pan law for p in [-1, 1] (FUN_0045e9d0).
void panGains(float p, float* left, float* right) {
    if (!(p >= -1.0f)) {
        p = -1.0f;
    } else if (p > 1.0f) {
        p = 1.0f;
    }
    const double s = std::sqrt(1.0f / ((static_cast<double>(p) * p) * 2.0f + 2.0f));
    *left = sgl(s - s * p);
    *right = sgl(s * p + s);
}

AmpTables buildTables() {
    RoundToNearest rn;
    AmpTables t{};

    // Unit mod_amp4_13 init (FUN_0045ea54): pan -63..63 -> p = pan / 63.
    for (int i = -63; i < 64; i++) {
        const float p = sgl(static_cast<double>(i) / 63.0f);
        panGains(p, &t.pan[i + 63][0], &t.pan[i + 63][1]);
    }

    // Unit satTables init (FUN_0045e33c): drive grows with t^2, gain linearly with t.
    for (int n = 0; n < 2; n++) {
        const float* r = data::kSatParams[n];
        float(*e)[2] = t.sat[n];
        e[0][0] = 1.0f;
        e[0][1] = r[2];
        for (int k = 0; k < 64; k++) {
            float u = sgl(mulExt(kOneOver63, k));
            e[k + 1][1] = sgl((static_cast<double>(r[3]) - 1.0f) * u + 1.0f);
            u = sgl(static_cast<double>(u) * u);
            float drive = sgl((static_cast<double>(r[1]) - r[0]) * u + r[0]);
            if (drive < kMinDrive) drive = kMinDrive;
            e[k + 1][0] = drive;
        }
    }
    return t;
}

}  // namespace

const AmpTables& ampTables() {
    static const AmpTables tables = buildTables();
    return tables;
}

void Amp::init() {
    *this = Amp{};  // TObject.InitInstance zero-fills the object
    setConstants();
    refreshRampLength();
    setRampTime(0.0f);
    satIndex = -2;
    setSaturation(0);
}

void Amp::setConstants() {
    sqrt2 = 0x1.6a09e6p+0f;     // 0x3fb504f3
    satScale = 0x1.0f876cp+0f;  // 0x3f87c3b6
    sixth = 0x1.555556p-3f;     // 0x3e2aaaab
    clip[0] = 1.0f;
    clip[1] = -1.0f;
}

void Amp::setSampleRate(float sr) {
    sampleRate = sr;
    refreshRampLength();
}

void Amp::setRampTime(float seconds) {
    if (seconds > 0.0f) {
        rampTime = seconds;
        setRampLength(fistp32(sgl(static_cast<double>(seconds) * sampleRate)));
    } else {
        rampTime = 0;
        setRampLength(0);
    }
}

void Amp::setRampLength(int32_t samples) {
    if (samples > 0) {
        rampLength = samples;
        rampStep = sgl(1.0f / static_cast<double>(samples));
    } else {
        rampLength = 0;
        rampStep = 0;
    }
}

void Amp::setSaturation(int32_t sat) {
    // The original computes sat*4+1 in 32 bits; plugCore passes a signed byte.
    int32_t idx = -1;
    if (sat >= 0) {
        idx = static_cast<int32_t>(static_cast<uint32_t>(sat) * 4u + 1u);
        if (idx >= 63) idx = 63;
    }
    satIndex = idx;
    const float* e = ampTables().sat[0][idx + 1];
    const double drive = e[0];
    satThreshold = sgl(static_cast<double>(sqrt2) / drive);
    satGain = e[1];
    satCubic = sgl(-((((drive * drive) * drive) * satScale) * sixth));
    satLinear = sgl(drive * satScale);
}

void Amp::setSmoothing(int32_t amount) {
    if (amount <= 0) {
        smoothing = 0;
        return;
    }
    if (amount >= 256) amount = 255;
    const uint32_t d = static_cast<uint32_t>(255 - amount);
    const int32_t k = 255 - static_cast<int32_t>((d * d) >> 8);
    smoothing = k;
    smoothA = k;
    smoothB = 256 - k;
}

void Amp::updateLevel(int32_t lvl, int32_t p, bool immediate) {
    if (lvl > 255) {
        lvl = 255;
    } else if (lvl < 0) {
        lvl = 0;
    }
    if (p > 63) {
        p = 63;
    } else if (p < -63) {
        p = -63;
    }
    if (smoothing <= 0) {
        level = lvl;
    } else if (!immediate) {
        // One-pole smoothing in 24.8 fixed point; IMUL wraps, SAR rounds toward zero.
        uint32_t acc = static_cast<uint32_t>(levelAcc) * static_cast<uint32_t>(smoothA) +
                       ((static_cast<uint32_t>(lvl) * static_cast<uint32_t>(smoothB)) << 8);
        int32_t a = static_cast<int32_t>(acc);
        if (a < 0) a += 0xff;
        a >>= 8;
        levelAcc = a;
        if (a < 0) a += 0xff;
        a >>= 8;
        level = a < 0 ? 0 : (a > 255 ? 255 : a);
    } else {
        levelAcc = lvl << 8;
        level = lvl;
    }
    pan = p;
}

void Amp::setLevelPan(int32_t lvl, int32_t p, float gainArg, bool immediate) {
    updateLevel(lvl, p, immediate);
    const float g = sgl(mulExt(kGainScale, satGain) * gainArg);
    if (!immediate && rampCount < 0) {
        volL = targetL;
        volR = targetR;
    }
    const float* pg = ampTables().pan[pan + 63];
    const float a = data::kAmpShape[level];
    targetL = sgl(static_cast<double>(pg[0]) * a * g);
    targetR = sgl(static_cast<double>(pg[1]) * a * g);
    if (targetL <= kMinTarget) targetL = kMinTarget;
    if (targetR <= kMinTarget) targetR = kMinTarget;
    if (immediate) {
        volL = targetL;
        volR = targetR;
        stepL = 0;
        stepR = 0;
        rampCount = 0;
    } else {
        stepL = sgl((static_cast<double>(targetL) - volL) * rampStep);
        stepR = sgl((static_cast<double>(targetR) - volR) * rampStep);
        volL = sgl(static_cast<double>(volL) + stepL);
        volR = sgl(static_cast<double>(volR) + stepR);
        rampCount = rampLength;
    }
    gain = g;
}

void Amp::start(const Amp* o) {
    if (!o) {
        setLevelPan(0, pan, true);
        return;
    }
    level = o->level;
    pan = o->pan;
    smoothing = o->smoothing;
    levelAcc = o->levelAcc;
    smoothA = o->smoothA;
    smoothB = o->smoothB;
    satIndex = o->satIndex;
    satThreshold = o->satThreshold;
    satCubic = o->satCubic;
    satLinear = o->satLinear;
    satGain = o->satGain;
    volL = o->volL;
    volR = o->volR;
    targetL = o->targetL;
    targetR = o->targetR;
    stepL = o->stepR;  // sic: the original copies +0x10 into +0x0c
    stepR = o->stepR;
    rampCount = o->rampCount;
    rampLength = o->rampLength;
    rampStep = o->rampStep;
    gain = o->gain;
}

double Amp::saturate(double x) const {
    const double a = std::fabs(x);
    if (!(static_cast<double>(satThreshold) >= a)) {  // FCOMIP + JC (also taken for NaN)
        return clip[x >= 0.0 ? 0 : 1];                // FTST: C0 set for x < 0 and NaN
    }
    return ((a * a) * satCubic + satLinear) * x;
}

void Amp::advanceRamp() {
    if (rampCount > 0) {
        volL = sgl(static_cast<double>(volL) + stepL);
        volR = sgl(static_cast<double>(volR) + stepR);
    }
    rampCount = static_cast<int32_t>(static_cast<uint32_t>(rampCount) - 1u);  // SUB wraps
}

void Amp::process(double x, float* out) {
    if (satIndex >= 0) x = saturate(x);
    out[0] = sgl(x * volL + out[0]);
    out[1] = sgl(x * volR + out[1]);
    advanceRamp();
}

void Amp::processFade(double x, float* out, float fade) {
    if (satIndex >= 0) x = saturate(x);
    x = x * fade;
    out[0] = sgl(x * volL + out[0]);
    out[1] = sgl(x * volR + out[1]);
    advanceRamp();
}

}  // namespace sq8l
