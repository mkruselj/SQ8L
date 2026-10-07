#include "AniDisplay.h"

#include "../GuiFpu.h"

namespace sq8l::gui {

AniDisplay::AniDisplay(std::string name) : Control(std::move(name), true) {
    setBounds(0, 0, 10, 10);
}

void AniDisplay::fitToGif() {
    if (!gif_) return;
    int w = 10, h = 10;
    if (gif_->loaded()) {
        w = gif_->frameWidth();
        h = gif_->frameHeight();
    }
    if (w != width_ || h != height_) {
        lock_++;
        setWidth(w);
        setHeight(h);
        if (lock_ > 0)
            lock_--;
        else
            lock_ = 0;
    }
}

void AniDisplay::setAniGif(const Sprite* gif) {
    gif_ = gif;
    fitToGif();
    invalidate();
}

void AniDisplay::setStartFrame(int f) {
    startFrame_ = f < 0 ? -1 : f;
    invalidate();
}

void AniDisplay::setNumFrames(int n) {
    numFrames_ = n < 1 ? 1 : n;
    invalidate();
}

void AniDisplay::setMinVal(float v) {
    min_ = v;
    if (!(max_ >= min_)) max_ = v;
    invalidate();
}

void AniDisplay::setMaxVal(float v) {
    max_ = v;
    if (!(max_ >= min_)) min_ = v;
    invalidate();
}

void AniDisplay::setValue(float v) {
    // fcomp min; jae / fcomp max; jbe / fcomp value; je  (unordered: v = min)
    if (!(v >= min_))
        v = min_;
    else if (v > max_)
        v = max_;
    if (v < value_ || v > value_) {
        value_ = v;
        invalidate();
    }
}

int AniDisplay::frameIndex() const {
    if (!gif_ || !gif_->loaded()) return -1;
    float r = static_cast<float>(static_cast<double>(max_) - static_cast<double>(min_));
    int idx = 0;
    if (r < 0.0f || r > 0.0f) {
        float t = static_cast<float>((static_cast<double>(value_) - static_cast<double>(min_)) / static_cast<double>(r) *
                                         static_cast<double>(numFrames_ - 1) +
                                     static_cast<double>(startFrame_));
        idx = fpu::roundEven(t);
    }
    if (idx < 0)
        idx = 0;
    else if (idx >= gif_->count())
        idx = gif_->count();  // (sic) count, not count - 1: no frame -> fallback rectangle
    return gif_->frame(idx) ? idx : -1;
}

void AniDisplay::paint(Canvas& canvas) {
    if (lock_ >= 1) return;
    lock_++;
    fitToGif();
    float r = static_cast<float>(static_cast<double>(max_) - static_cast<double>(min_));
    int idx = frameIndex();
    if (idx >= 0) {
        canvas.draw(0, 0, gif_->frame(idx));
    } else {
        // No frame: a rectangle, blue (TColor 0xff0000) if the value maps >= 0, else black.
        int k = 0;
        if (r < 0.0f || r > 0.0f)
            k = fpu::roundEven(static_cast<float>((static_cast<double>(value_) - static_cast<double>(min_)) /
                                                  static_cast<double>(r) * 255.0));
        Color c = k >= 0 ? fromTColor(0xFF0000) : 0;
        canvas.brushColor = c;
        canvas.penColor = c;
        canvas.fillRect(Rect{0, 0, width_, height_});  // Rectangle(0, 0, Width, Height), same colours
    }
    if (lock_ > 0)
        lock_--;
    else
        lock_ = 0;
}

}  // namespace sq8l::gui
