// Floating point semantics of the original SQ8L (Delphi 5, x87 FPU).
//
// Inside processReplacing the original sets the x87 control word to
// "host CW | 0xC00": round toward zero, while a typical (MSVC) host keeps
// 53-bit precision. x87 arithmetic at 53-bit precision with round-toward-zero
// is exactly IEEE double arithmetic in FE_TOWARDZERO mode, so the engine runs
// its audio processing inside a RoundTowardZero scope and keeps intermediates
// that lived on the x87 stack in `double`, values the original stored to
// memory as Single in `float`.
//
// The whole engine must be compiled without FMA contraction and with dynamic
// rounding mode respected (see CMakeLists.txt: -ffp-contract=off -frounding-math).
#pragma once

#include <cfenv>
#include <cmath>
#include <cstdint>
#if defined(__x86_64__) || defined(_M_X64)
#include <xmmintrin.h>
#endif

namespace sq8l {

class RoundTowardZero {
public:
    RoundTowardZero() : saved_(std::fegetround()) { std::fesetround(FE_TOWARDZERO); }
    ~RoundTowardZero() { std::fesetround(saved_); }
    RoundTowardZero(const RoundTowardZero&) = delete;
    RoundTowardZero& operator=(const RoundTowardZero&) = delete;

private:
    int saved_;
};

// Outermost scope for the audio thread: round toward zero like the original, and
// make sure denormals are NOT flushed (hosts often enable FTZ/DAZ; the x87 never
// flushed, so decaying tails would differ).
class AudioFpuScope {
public:
    AudioFpuScope() {
#if defined(__aarch64__)
        uint64_t fpcr;
        __asm__ volatile("mrs %0, fpcr" : "=r"(fpcr));
        saved_ = fpcr;
        fpcr &= ~((1ull << 24) | (1ull << 19));   // FZ, FZ16 off
        fpcr |= (3ull << 22);                     // RMode = toward zero
        __asm__ volatile("msr fpcr, %0" : : "r"(fpcr));
#elif defined(__x86_64__) || defined(_M_X64)
        uint32_t csr = _mm_getcsr();
        saved_ = csr;
        csr &= ~((1u << 15) | (1u << 6));         // FTZ, DAZ off
        csr |= (3u << 13);                        // RC = toward zero
        _mm_setcsr(csr);
#else
        saved_ = static_cast<uint64_t>(std::fegetround());
        std::fesetround(FE_TOWARDZERO);
#endif
    }
    ~AudioFpuScope() {
#if defined(__aarch64__)
        __asm__ volatile("msr fpcr, %0" : : "r"(saved_));
#elif defined(__x86_64__) || defined(_M_X64)
        _mm_setcsr(static_cast<uint32_t>(saved_));
#else
        std::fesetround(static_cast<int>(saved_));
#endif
    }
    AudioFpuScope(const AudioFpuScope&) = delete;
    AudioFpuScope& operator=(const AudioFpuScope&) = delete;

private:
    uint64_t saved_;
};

class RoundToNearest {
public:
    RoundToNearest() : saved_(std::fegetround()) { std::fesetround(FE_TONEAREST); }
    ~RoundToNearest() { std::fesetround(saved_); }
    RoundToNearest(const RoundToNearest&) = delete;
    RoundToNearest& operator=(const RoundToNearest&) = delete;

private:
    int saved_;
};

// x87 FISTP: convert using the current rounding mode (Delphi's Round()).
inline int32_t fistp(double x) { return static_cast<int32_t>(std::lrint(x)); }

// Delphi Trunc(): always toward zero regardless of mode.
inline int32_t trunc32(double x) { return static_cast<int32_t>(x); }

// Store to a Single variable (FSTP dword): rounds with the current mode.
inline float sgl(double x) { return static_cast<float>(x); }

}  // namespace sq8l

namespace sq8l {

// An x87 80-bit constant split into two doubles (hi + lo == exact value).
struct Ext {
    double hi, lo;
};

// x87 "FLD tbyte c; FMUL x" at 53-bit precision: exact product rounded once.
// Exact for x with <= 42 significant bits (any float).
inline double mulExt(Ext c, double x) { return std::fma(c.hi, x, c.lo * x); }

}  // namespace sq8l
