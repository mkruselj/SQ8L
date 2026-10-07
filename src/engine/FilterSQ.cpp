#include "FilterSQ.h"
#include "CrMath.h"

#include <cmath>

#include "Fpu.h"

namespace sq8l {

namespace {

// 80-bit constants from the original code, split hi/lo (see Fpu.h).
constexpr Ext kMaxNormFreq = {0x1.f5c28f5c28f5cp-2, 0x1.4800000000000p-57};  // 0.49
constexpr Ext kTwoPi = {0x1.921fb54442d18p+2, 0x1.1a80000000000p-52};
constexpr Ext kResoExpA = {-0x1.9b96b53d04acbp-1, 0x1.bd80000000000p-55};    // -0.8038841855035
constexpr Ext kResoMulA = {0x1.93dd5f0554338p+0, 0x1.cd00000000000p-54};     // 1.5775966060648
constexpr Ext kResoExpB = {0x1.c71fab29bdf0bp+1, -0x1.5580000000000p-53};    // 3.555653949148
constexpr Ext kResoMulB = {-0x1.f479c4f868859p-7, 0x1.1980000000000p-61};    // -0.015273305108
constexpr Ext kCutoffScale = {0x1.f851eb851eb85p-1, 0x1.ec00000000000p-57};  // 0.985
constexpr Ext kResoPoly2 = {-0x1.24e1c55b2d389p-1, 0x1.5000000000000p-56};   // -0.5720349954414
constexpr Ext kResoPoly1 = {0x1.9dab9d69daa79p+0, 0x1.3400000000000p-55};    // 1.615899885505
constexpr Ext kResoPoly0 = {-0x1.7824087c06260p-5, -0x1.cc00000000000p-62};  // -0.04591561944125
constexpr Ext kResoScale = {0x1.04189374bc6a8p+0, -0x1.0600000000000p-56};   // 1.016
constexpr Ext kResoGainBase = {0x1.51eb851eb851fp-2, -0x1.1e80000000000p-56};  // 0.33

double extValue(Ext c) { return c.hi + c.lo; }

FilterUnitTables buildUnitTables() {
    RoundToNearest rn;
    FilterUnitTables t{};

    // Cutoff frequencies: 4 exponential segments between anchor frequencies
    // (FUN_0045f61c). Each segment starts from the previous segment's last
    // entry, so the 0.985 factor accumulates (faithful to the original).
    const float anchors[4] = {309.662f, 2231.04f, 13638.128f, 45767.85f};
    const int counts[4] = {65, 65, 65, 64};
    float start = 42.394f;
    for (int seg = 0; seg < 4; seg++) {
        float* out = t.cutoffHz + 64 * seg;
        if (seg > 0) start = out[0];
        const float step = sgl(crmath::log(static_cast<double>(anchors[seg]) / start) / 63.0f);
        for (int i = 0; i < counts[seg]; i++) {
            const double e = crmath::exp(static_cast<double>(i) * step) * start;
            out[i] = sgl(mulExt(kCutoffScale, e));
        }
    }

    // Resonance amount per RES value (FUN_0045f79c).
    t.resoAmount[0] = 0;
    for (int i = 1; i < 32; i++) {
        const float x = sgl(static_cast<double>(i) / 31.0f);
        double v = mulExt(kResoPoly2, x) * x;
        v = v + mulExt(kResoPoly1, x);
        v = v + extValue(kResoPoly0);
        t.resoAmount[i] = sgl(mulExt(kResoScale, v));
    }

    // Output gain compensation per RES value (FUN_0045f830).
    const float lnBase = sgl(crmath::log(extValue(kResoGainBase)));
    for (int i = 0; i < 32; i++) {
        const double s = std::sqrt(static_cast<double>(i) / 31.0f);
        t.resoGain[i] = sgl(crmath::exp(s * lnBase));
    }
    return t;
}

// Normalized angular frequency, clamped below Nyquist (nested FUN_0045f29c).
float angularFreq(float hz, float sampleRate) {
    float w = sgl(static_cast<double>(hz) / sampleRate);
    if (!(extValue(kMaxNormFreq) >= w)) w = 0.49f;
    return sgl(mulExt(kTwoPi, w));
}

// Pole coefficient from angular frequency (FUN_0045f2f4).
float poleCoefficient(float w) {
    float t = sgl(crmath::tan(static_cast<double>(w) / 4.0f));
    const double d = static_cast<double>(t) - 1.0f;
    t = sgl((2.0f * static_cast<double>(t)) / (d * d));
    return sgl((static_cast<double>(t) - 1.0f) / (static_cast<double>(t) + 1.0f));
}

// Feedback amount for a resonance setting at pole coefficient p (FUN_0045f358).
float feedback(float amount, float p) {
    const double a = mulExt(kResoMulA, crmath::exp(mulExt(kResoExpA, p)));
    const double b = mulExt(kResoMulB, crmath::exp(mulExt(kResoExpB, p)));
    return sgl((b + a) * amount);
}

// Delphi Power(Base, Exponent) for a non-integral exponent: exp(e * ln(b)).
double delphiPower(double base, double exponent) {
    return crmath::exp(exponent * crmath::log(base));
}

}  // namespace

const FilterUnitTables& filterUnitTables() {
    static const FilterUnitTables tables = buildUnitTables();
    return tables;
}

void FilterTable::compute(float sampleRate) {
    const FilterUnitTables& u = filterUnitTables();
    for (int c = 0; c < 256; c++) {
        const float w = angularFreq(u.cutoffHz[c], sampleRate);
        const float p = poleCoefficient(w);
        for (int r = 0; r < 32; r++) {
            entry[r][c][0] = p;
            entry[r][c][1] = feedback(u.resoAmount[r], p);
        }
    }
}

void FilterSQ::init(const FilterTable* t, float sr) {
    resoComp = 1.075f;
    half = 0.5f;
    sixth = 1.0f / 6.0f;
    table = t;
    smoothTime = 0x1.25ff56p-6f;  // 0x3c92ffab
    gain = 1.0f;
    keyParam = -2;
    setKeyParam(-1);
    level = 1.0f;
    // CfilterBase constructor: virtual calls in this order.
    setMode(0);
    setSampleRate(sr);
    setParams(0, 0, true);
    copyStateFrom(nullptr);
}

void FilterSQ::setSampleRate(float sr) {
    sampleRate = sr;
    computeSmoothing();
    applyParams(true);
}

void FilterSQ::computeSmoothing() {
    // a = 0.01 ^ (1 / (0.5 * T * sr + 1)); the original repeats it three times.
    const double e = 1.0f / ((0.5f * static_cast<double>(smoothTime)) * sampleRate + 1.0f);
    pA = sgl(delphiPower(0.01, e));
    pB = sgl(1.0f - static_cast<double>(pA));
    kA = sgl(delphiPower(0.01, e));
    kB = sgl(1.0f - static_cast<double>(kA));
    gainA = sgl(delphiPower(0.01, e));
    gainB = sgl(1.0f - static_cast<double>(gainA));
}

void FilterSQ::copyStateFrom(const FilterSQ* o) {
    if (!o) {
        for (float& s : stage) s = 0;
        return;
    }
    gain = o->gain;
    keyParam = o->keyParam;
    for (int i = 0; i < 5; i++) stage[i] = o->stage[i];
    g = o->g;
    k = o->k;
    p = o->p;
    kTarget = o->kTarget;
    pTarget = o->pTarget;
    pA = o->pA;
    pB = o->pB;
    kA = o->kA;
    kB = o->kB;
    gainA = o->gainA;
    gainB = o->gainB;
    gainTarget = o->gainTarget;
    smoothTime = o->smoothTime;
}

void FilterSQ::setParams(int32_t c, int32_t r, bool immediate) {
    cutoff = c < 1 ? 0 : (c < 255 ? c : 255);
    reso = r < 1 ? 0 : (r < 31 ? r : 31);
    applyParams(immediate);
}

void FilterSQ::setCutoff(int32_t c, bool immediate) {
    cutoff = c < 1 ? 0 : (c < 255 ? c : 255);
    applyParams(immediate);
}

void FilterSQ::setReso(int32_t r, bool immediate) {
    reso = r < 1 ? 0 : (r < 31 ? r : 31);
    applyParams(immediate);
}

void FilterSQ::setKeyParam(int32_t v) {
    keyParam = v < 0 ? -1 : (v > 62 ? 63 : v);
}

void FilterSQ::applyParams(bool immediate) {
    const float* e = table->entry[reso][cutoff];
    pTarget = e[0];
    kTarget = e[1];
    if (immediate) {
        p = pTarget;
        k = kTarget;
        g = sgl((static_cast<double>(pTarget) + 1.0f) * 0.5f);
    }
    pTarget = sgl(static_cast<double>(pTarget) * pB);
    kTarget = sgl(static_cast<double>(kTarget) * kB);
    gainTarget = filterUnitTables().resoGain[reso];
    if (immediate) gain = gainTarget;
    gainTarget = sgl(static_cast<double>(gainTarget) * gainB);
}

double FilterSQ::process(double x) {
    const double t = x * (static_cast<double>(k) * resoComp + 1.0f);
    const double a4 = static_cast<double>(stage[3]) * g - static_cast<double>(stage[4]) * p;
    const double a3 = static_cast<double>(stage[2]) * g - static_cast<double>(stage[3]) * p;
    const double a2 = static_cast<double>(stage[1]) * g - static_cast<double>(stage[2]) * p;
    const double a1 = static_cast<double>(stage[0]) * g - static_cast<double>(stage[1]) * p;
    double y = t - static_cast<double>(stage[4]) * k;
    stage[0] = sgl(y);
    y = y * g + a1;
    stage[1] = sgl(y);
    y = y * g + a2;
    stage[2] = sgl(y);
    y = y * g + a3;
    stage[3] = sgl(y);
    y = y * g + a4;
    stage[4] = sgl(y);
    const double out = y * gain;
    g = sgl((1.0f + static_cast<double>(p)) * half);
    p = sgl(static_cast<double>(p) * pA + pTarget);
    k = sgl(static_cast<double>(k) * kA + kTarget);
    gain = sgl(static_cast<double>(gain) * gainA + gainTarget);
    return out;
}

}  // namespace sq8l
