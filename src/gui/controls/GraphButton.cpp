#include "GraphButton.h"

#include <algorithm>

namespace sq8l::gui {

GraphButton::GraphButton(std::string name) : Control(std::move(name), true) {
    setBounds(0, 0, 0x14, 0x14);
    pressDisp_ = 1;
    fontColor_ = 0xFFFFFF;
    frameColor_ = fromTColor(0xFF00);
    frameWidth_ = 0;
}

void GraphButton::setAniGif(const Sprite* gif) {
    gif_ = gif;
    invalidate();
}

void GraphButton::setAniIdx(int idx) {
    aniIdx_ = idx < 0 ? 0 : idx;
    invalidate();
}

void GraphButton::setHasTwoFrames(bool two) {
    twoFrames_ = two;
    invalidate();
}

void GraphButton::setCaption(const std::string& c) {
    caption_ = c;
    invalidate();
}

void GraphButton::setCaptionPlace(CaptionPlace p) {
    place_ = p;
    invalidate();
}

void GraphButton::setFontColor(Color c) {
    fontColor_ = c;
    invalidate();
}

void GraphButton::setFrameColor(Color c) {
    frameColor_ = c;
    invalidate();
}

void GraphButton::setFrameWidth(int w) {
    frameWidth_ = w;
    invalidate();
}

void GraphButton::setPressDisplacement(int d) {
    pressDisp_ = d;
    invalidate();
}

void GraphButton::layout(Canvas& canvas) {
    // FUN_0047c100: size the control around image + caption, place both.
    canvas.font = font;
    int cx = canvas.textWidth(caption_);  // TCanvas.TextExtent
    int cy = canvas.textHeight();
    int fw = 0x14, fh = 0x14;
    if (gif_) {
        fw = gif_->frameWidth();
        fh = gif_->frameHeight();
    }
    auto fit = [this](int w, int h) {  // FUN_0047c0c0
        if (w != width_ || h != height_) setBounds(left_, top_, w, h);
    };
    switch (place_) {
        case CapCenter:
            fit(pressDisp_ + fw, pressDisp_ + fh);
            captionX_ = width_ / 2 - cx / 2;
            captionY_ = height_ / 2 - cy / 2 - 2;
            imageX_ = 0;
            imageY_ = 0;
            break;
        case CapTop:
            fit(std::max(cx, fw) + pressDisp_, cy + 2 + fh + pressDisp_);
            captionX_ = width_ / 2 - cx / 2;
            captionY_ = 0;
            imageX_ = width_ / 2 - fw / 2;
            imageY_ = cy + 2;
            break;
        case CapRight:
            fit(cx + 2 + fw + pressDisp_, std::max(cy, fh) + pressDisp_);
            captionX_ = fw + 2;
            captionY_ = height_ / 2 - cy / 2;
            imageX_ = 0;
            imageY_ = height_ / 2 - fh / 2;
            break;
        case CapBottom:
            fit(std::max(cx, fw) + pressDisp_, cy + 2 + fh + pressDisp_);
            captionX_ = width_ / 2 - cx / 2;
            captionY_ = fh + 2;
            imageX_ = width_ / 2 - fw / 2;
            imageY_ = 0;
            break;
        case CapLeft:
            fit(cx + 2 + fw + pressDisp_, std::max(cy, fh) + pressDisp_);
            captionX_ = 0;
            captionY_ = height_ / 2 - cy / 2;
            imageX_ = cx + 2;
            imageY_ = height_ / 2 - fh / 2;
            break;
    }
}

void GraphButton::paint(Canvas& canvas) {
    layout(canvas);
    int off = pressed_ > 0 ? pressDisp_ : 0;
    if (gif_) {
        canvas.draw(imageX_ + off, imageY_ + off, gif_->frame(frameIndex()));
    } else {
        canvas.brushColor = 0;
        canvas.penColor = 0xFFFFFF;
        canvas.fillRect(Rect{imageX_ + off, imageY_ + off, imageX_ + off + 0x14, imageY_ + off + 0x14});
    }
    if (frameWidth_ > 0 && hover_) {
        canvas.penColor = frameColor_;
        canvas.brushColor = frameColor_;
        canvas.frameRect(Rect{0, 0, width_, height_});
    }
    if (!caption_.empty()) {
        canvas.font.color = fontColor_;
        canvas.textOut(captionX_ + off, captionY_ + off, caption_);
    }
}

void GraphButton::mouseDown(MouseButton button, ShiftState shift, int x, int y) {
    Control::mouseDown(button, shift, x, y);
    if (button == MouseButton::Left) {
        setFocus();
        pressed_ = 1;
        invalidate();
    }
}

void GraphButton::mouseUp(MouseButton button, ShiftState shift, int x, int y) {
    Control::mouseUp(button, shift, x, y);
    if (button == MouseButton::Left) {
        if (pressed_ > 0) fireClick();
        pressed_ = 0;
        invalidate();
    }
}

void GraphButton::mouseMove(ShiftState shift, int x, int y) {
    bool inside = x >= 0 && y >= 0 && x < width_ && y < height_;
    if (inside) {
        setMouseCapture(this);  // Mouse.Capture := Handle
        Control::mouseMove(shift, x, y);
    } else if (mouseCaptureControl() == this) {
        setMouseCapture(nullptr);
    }
    bool changed = inside != hover_;
    hover_ = inside;
    if (pressed_ > 0 && !inside) {
        pressed_ = 0;
        changed = true;
    }
    if (changed) invalidate();
}

}  // namespace sq8l::gui
