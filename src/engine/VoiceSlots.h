// Voice slots of the engine. The original has 16 (the CplugMaster and Cdoc arrays): 8
// playable voices plus 8 more where the voices taken by soft voice stealing fade out. The
// port can play up to 32 voices (OPTIONS -> Polyphony, a port addition), always with 8
// fade slots: 40 slots, the first 16 used exactly like the original's.
#pragma once

namespace sq8l {

constexpr int kOriginalVoiceSlots = 16;      // the original's arrays (and state layouts)
constexpr int kOriginalPlayableVoices = 8;   // FUN_00463358(8, 8)
constexpr int kFadeVoices = 8;
constexpr int kMaxPlayableVoices = 32;
constexpr int kMaxVoiceSlots = kMaxPlayableVoices + kFadeVoices;

}  // namespace sq8l
