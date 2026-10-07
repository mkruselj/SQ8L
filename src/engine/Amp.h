// Final amplifier of a voice: DCA4 level, saturation (SAT) and panning.
// Original unit mod_amp4_13, class Camp (0x74 bytes, one per voice slot, master voice
// slot +0x7c). See docs/modules/amp.md.
#pragma once

#include <cstdint>

namespace sq8l {

namespace data {
extern const float kAmpShape[256];    // 0x4c2110: DCA4 level 0..255 -> linear gain 0..2
extern const float kSatParams[2][4];  // 0x4c2510: {drive SAT=0, drive max, gain off, gain max}
}  // namespace data

// Tables built by unit initialization code (computed; verified against the emulator).
struct AmpTables {
    // 0x4c5610 (FUN_0045ea54): {left, right} gain for pan -63..+63, index pan + 63.
    float pan[127][2];
    // 0x4c51f8 (unit satTables init FUN_0045e33c): per table 65 {drive, gain} pairs,
    // index = saturation index + 1 (entry 0 = saturation off). Only table 0 is used
    // (through the pointer variable at 0x4c3224); table 1 (0x4c5400) is dead data.
    float sat[2][65][2];
};
const AmpTables& ampTables();

// Rounding: like the original, every method computes in the caller's rounding mode. plugCore
// calls init/setSampleRate at load or sample-rate change (RoundToNearest) and everything else
// from processReplacing (RoundTowardZero: voice start, control tick, per sample).
class Amp {
public:
    // Comments give the field offset in the original object (for differential tests).
    // +0x00 is the VMT pointer.
    float volL = 0;          // +0x04  current left gain (ramped per sample)
    float volR = 0;          // +0x08  current right gain
    float stepL = 0;         // +0x0c  per-sample left gain increment
    float stepR = 0;         // +0x10  per-sample right gain increment
    int32_t rampCount = 0;   // +0x14  ramp samples left; decremented every sample, goes
                             //        negative after the ramp (plugCore reads it)
    int32_t satIndex = 0;    // +0x18  -1 = saturation off, else 4*SAT+1 (<= 63)
    float satThreshold = 0;  // +0x1c  |x| above which the output clips: sqrt2 / drive
    float satCubic = 0;      // +0x20  -drive^3 * satScale / 6
    float satLinear = 0;     // +0x24  drive * satScale
    float satGain = 0;       // +0x28  output gain of the saturation setting
    float clip[2] = {};      // +0x2c, +0x30  {+1, -1}: output of a clipped sample by sign
    float sqrt2 = 0;         // +0x34  sqrt(2)
    float satScale = 0;      // +0x38  3 / (2 sqrt 2): makes the cubic reach 1 at the threshold
    float sixth = 0;         // +0x3c  1 / 6
    float targetL = 0;       // +0x40  left gain the ramp is heading to
    float targetR = 0;       // +0x44  right gain target
    int32_t rampLength = 0;  // +0x48  ramp length in samples (0 = no ramp)
    float rampStep = 0;      // +0x4c  1 / rampLength
    int32_t level = 0;       // +0x50  DCA4 level 0..255 (after optional smoothing)
    int32_t pan = 0;         // +0x54  -63..+63
    int32_t smoothing = 0;   // +0x58  level smoothing amount (0 = off)
    int32_t levelAcc = 0;    // +0x5c  smoothed level, 8 fractional bits
    int32_t smoothA = 0;     // +0x60  smoothing feedback (= smoothing)
    int32_t smoothB = 0;     // +0x64  256 - smoothA
    float rampTime = 0;      // +0x68  ramp time in seconds
    float gain = 0;          // +0x6c  last overall gain: 1.22756 * satGain * gain argument
    float sampleRate = 0;    // +0x70

    // Constructor FUN_0045e4a0 (Camp.Create). The original also takes a Single (the master
    // passes the sample rate) that it never reads: sampleRate stays 0 until setSampleRate.
    // Needs round-to-nearest (load time).
    void init();

    // FUN_0045e7b8: store the sample rate and recompute the ramp length (round to nearest).
    void setSampleRate(float sr);

    // FUN_0045e7f8: ramp time in seconds; rampLength = Round(t * sampleRate) under the
    // current rounding mode (truncates inside process). t <= 0 disables ramping.
    void setRampTime(float seconds);

    // FUN_0045e89c: SAT parameter (signed program byte); < 0 switches saturation off.
    void setSaturation(int32_t sat);

    // FUN_0045e848: level smoothing amount (<= 0 off, clamped to 255).
    void setSmoothing(int32_t amount);

    // FUN_0045e69c: new DCA4 level (0..255) and pan (-63..63), both clamped, times an extra
    // gain (plugCore passes the filter's level field +0x14). immediate jumps to the new
    // gains, otherwise a linear ramp of rampLength samples starts (first step applied now).
    void setLevelPan(int32_t level, int32_t pan, float gain, bool immediate);
    // FUN_0045e680: same with gain 1.
    void setLevelPan(int32_t level, int32_t pan, bool immediate) { setLevelPan(level, pan, 1.0f, immediate); }

    // FUN_0045e554 (voice start): take over the gains of the amp of the voice being continued
    // (voice restart: same key struck again while its old voice sounds), or with nullptr jump
    // to silence (level 0, current pan).
    void start(const Amp* from);

    // FUN_0045e8ec: one sample. x is the filter output (x87 ST0, consumed); the stereo
    // result is accumulated into out[0], out[1] (Single).
    void process(double x, float* out);

    // FUN_0045e95c: like process, with the (saturated) sample multiplied by fade before the
    // gains (soft fade-out of a stolen voice; plugCore passes voice slot +0x3c).
    void processFade(double x, float* out, float fade);

private:
    friend struct AmpTestAccess;

    void setConstants();                                   // FUN_0045e52c
    void updateLevel(int32_t level, int32_t pan, bool immediate);  // FUN_0045e5dc
    void setRampLength(int32_t samples);                   // FUN_0045e7cc
    void refreshRampLength() { setRampTime(rampTime); }    // FUN_0045e890
    double saturate(double x) const;                       // inline part of FUN_0045e8ec
    void advanceRamp();                                    // inline part of FUN_0045e8ec
};

}  // namespace sq8l
