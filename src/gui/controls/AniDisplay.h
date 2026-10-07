// Port of TAniDisplay (unit AniDisplay, 0x47b3e4-0x47ba3c): shows one sprite frame chosen by
// a float value (SQ8L: the MONO / SYNC / AM LEDs, LedGIF frame 0 = off, 1 = on).
//   frame = Round((value - min) / (max - min) * (numFrames - 1) + startFrame)
// The control resizes itself to the GIF frame size when painted.
#pragma once

#include "../Sprite.h"
#include "Control.h"

namespace sq8l::gui {

class AniDisplay : public Control {
public:
    explicit AniDisplay(std::string name);  // TAniDisplay.Create (0x47b658): 10x10, max 1.0

    void setAniGif(const Sprite* gif);  // FUN_0047b75c
    void setStartFrame(int f);          // FUN_0047b778 (< 0 -> -1)
    void setNumFrames(int n);           // FUN_0047b794 (< 1 -> 1)
    void setMinVal(float v);            // FUN_0047b7b4 (raises max if needed)
    void setMaxVal(float v);            // FUN_0047b7e8 (lowers min if needed)
    void setValue(float v);             // FUN_0047b81c (clamped; repaint if changed)

    const Sprite* aniGif() const { return gif_; }
    int startFrame() const { return startFrame_; }
    int numFrames() const { return numFrames_; }
    float minVal() const { return min_; }
    float maxVal() const { return max_; }
    float value() const { return value_; }
    // Frame index Paint would use, or -1 when it falls back to the coloured rectangle.
    int frameIndex() const;

    // Raw state (tests).
    void setRaw(int startFrame, int numFrames, float min, float max, float value) {
        startFrame_ = startFrame;
        numFrames_ = numFrames;
        min_ = min;
        max_ = max;
        value_ = value;
        invalidate();
    }

    void paint(Canvas& canvas) override;  // 0x47b874

private:
    void fitToGif();  // FUN_0047b704

    const Sprite* gif_ = nullptr;  // +0x1f8
    int startFrame_ = 0;           // +0x1fc
    int numFrames_ = 0;            // +0x200
    float min_ = 0.0f;             // +0x204
    float max_ = 1.0f;             // +0x208
    float value_ = 0.0f;           // +0x20c
    int lock_ = 0;                 // +0x210
};

}  // namespace sq8l::gui
