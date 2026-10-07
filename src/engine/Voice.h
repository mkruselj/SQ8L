// Voice record of the master (original: 0xe8 bytes at CplugMaster+4+slot*0xe8).
#pragma once

#include <cstdint>

namespace sq8l {

struct Voice {
    // Comments give the field offset inside the original voice record.
    int32_t active = 0;           // +0x00  -1 while the voice is allocated, 0 when free
    uint32_t age = 0;             // +0x04  0xffffffff at note start, -1 per sample (larger = newer)
    int32_t ctrlCountdown = 0;    // +0x08  control-rate countdown, 1/1024 sample units
    int32_t tickParity = 0;       // +0x0c  control updates alternate: 0 = full update, 1 = docTickOdd
    int32_t slot = 0;             // +0x10  own slot index (also the Cdoc voice)
    int8_t key = 0;               // +0x14  current key (byte; -1 = none)
    int32_t released = 0;         // +0x18  -1 after note off (envelopes released)
    int32_t mono = 0;             // +0x1c  -1 if started by a mono program (program[0x172] > 0)
    int32_t noteId = 0;           // +0x20  (program number << 16) of the note that started it
    const uint8_t* program = nullptr;  // +0x24  540-byte program record the voice plays
    Voice* stolenFrom = nullptr;  // +0x28  voice whose slot this note took over (may be itself)
    int32_t stealFlag = 0;        // +0x2c  note record +0x10 (voice stealing enabled)
    int32_t fading = 0;           // +0x30  -1 while a fade (in or out) runs
    int32_t fadeKill = 0;         // +0x34  -1: free the voice when the fade ends (fade out)
    int32_t fadeRemaining = 0;    // +0x38  samples left in the fade
    float fadeGain = 0;           // +0x3c
    float fadeStep = 0;           // +0x40
    int32_t fadeOutLength = 0;    // +0x44  Round(sr * 0.005)
    float fadeOutStep = 0;        // +0x48  1 / fadeOutLength
    int32_t fadeInLength = 0;     // +0x4c  Round(sr * kFadeInTime[...])
    float fadeInStep = 0;         // +0x50
    // +0x54..+0x60 LFO objects, +0x64..+0x70 envelopes, +0x74/+0x78 filters, +0x7c amp
    int32_t dca4Mode = 0;         // +0x80  0/1: selects the amp smoothing table row
    int32_t glideActive = 0;      // +0x84  -1 while gliding (mod follower running)
    int32_t glideFrom = 0;        // +0x88  key the glide started from
    // +0x8c mod follower (glide)
    int32_t bend = 0;             // +0x90  pitch bend * range / 32
    int32_t bendActive = 0;       // +0x94  -1 if pitch bend currently applies to this voice
    // Voice-internal modulation sources (+0x98 + i*4): 0..3 LFO outputs, 4..7 envelope
    // outputs, 8/9 velocity curves, 10 key, 11 key scaling, 12 poly pressure, 13..15 MAT1-3.
    int32_t mod[16] = {};
    int32_t noteLevel = 0;        // +0xd8  note record +0x04 (0x3f): scales the DCA4 level
    int32_t panOffset = 0;        // +0xdc  note record +0x06 (0): added to the pan
    int32_t tickCount = 0;        // +0xe0  full control updates since a new note (-1 for legato)
    int32_t fe4 = 0;              // +0xe4  cleared at start/reset, never read by the engine
};

}  // namespace sq8l
