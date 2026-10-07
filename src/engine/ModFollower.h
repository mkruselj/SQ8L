// Linear "follower" ramp used for glide (portamento) (original unit mod_foll, class Cmod_foll,
// 32 bytes; 1 per voice slot at slot + 0x8c). See docs/modules/modfollower.md.
//
// The value is 20.12 fixed point. For a glide plugCore sets the oscillator key to the old key,
// reset(0)s the follower, sets the target to (newKey - oldKey) * 256 and adds tick() (1/256
// semitone) to the pitch every control tick while slot + 0x84 is set.
#pragma once

#include <cstdint>

namespace sq8l {

class ModFollower {
public:
    // Comments give the field offset in the original object (for differential tests).
    float rate = 0;           // +0x04  control rate (Hz), >= 1
    float speed = 0;          // +0x08  never written by the original (always 0), read by setRate
    float stepScale = 0;      // +0x0c  4096 / rate
    int32_t stepSize = 0;     // +0x10  |step| per tick, 20.12
    int32_t value = 0;        // +0x14  current value, 20.12
    int32_t step = 0;         // +0x18  signed step per tick (0 = idle)
    int32_t target = 0;       // +0x1c  target value, 20.12

    // Constructor (FUN_0045eb28): zero-filled object, setRate(rate), setSpeed(0).
    void init(float controlRate);
    // FUN_0045eba0: rate (< 1 or NaN -> 1), stepScale, then setSpeed(speed field).
    void setRate(float controlRate);
    // FUN_0045ebe8: speed in units per second (1/256 semitone per second for glide).
    // Keeps the direction of a running ramp.
    void setSpeed(float unitsPerSecond);
    // FUN_0045ec1c: ramp toward t (clamped to +-0x7ffff) at the current speed.
    void setTarget(int32_t t);
    // FUN_0045ec68: jump to v (clamped to +-0x7ffff) and stop.
    void reset(int32_t v);
    // FUN_0045ec90: advance one control tick; returns value >> 12 (rounded toward zero).
    int32_t tick();

    // Glide speed computed by plugCore before setSpeed (FUN_004625d4 at 0x462b28..0x462b63,
    // FUN_00462ca0 at 0x462cef..0x462d30):
    //   Single(|keyDelta| * 256 * 83.59257598 / timeTable[glideTime]).
    // Uses the current rounding mode (toward zero inside process). glideTime is the patch
    // GLIDE byte (0..63; clamped here, the original indexes the table unchecked).
    static float glideSpeed(int32_t keyDelta, int32_t glideTime);
};

}  // namespace sq8l
