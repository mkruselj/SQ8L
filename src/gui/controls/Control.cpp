#include "Control.h"

namespace sq8l::gui {

void Control::setBounds(int left, int top, int width, int height) {
    if (left == left_ && top == top_ && width == width_ && height == height_) return;
    left_ = left;
    top_ = top;
    width_ = width;
    height_ = height;
    if (windowed_) surface_.resize(width, height, 0);  // new area black, like a resized window surface
    invalidate();
}

void Control::paintWindow(TextRenderer* text) {
    if (surface_.width() != width_ || surface_.height() != height_) surface_.resize(width_, height_, 0);
    {
        // Erase: TWinControl.WMEraseBkgnd (or the DoubleBuffered memory-DC erase), brush = Color.
        Canvas erase(surface_, 0, 0, text);
        erase.brushColor = eraseColor;
        erase.fillRect(clientRect());
    }
    // Paint may resize the control (TAniDisplay, TGraphButton adjust their size to the GIF);
    // the window surface follows, keeping its content.
    Canvas canvas(surface_, 0, 0, text);
    paint(canvas);
    if (surface_.width() != width_ || surface_.height() != height_) {
        surface_.resize(width_, height_, 0);
    }
    for (Control* ch : children) {
        if (!ch->visible()) continue;
        Canvas cc(surface_, ch->left(), ch->top(), text);
        cc.setClip(ch->clientRect());
        ch->paint(cc);
    }
    invalid_ = false;
}

void Control::mouseDown(MouseButton button, ShiftState shift, int x, int y) {
    if (onMouseDown) onMouseDown(*this, button, shift, x, y);
}

void Control::mouseMove(ShiftState shift, int x, int y) {
    if (onMouseMove) onMouseMove(*this, shift, x, y);
}

void Control::mouseUp(MouseButton button, ShiftState shift, int x, int y) {
    if (onMouseUp) onMouseUp(*this, button, shift, x, y);
}

void Control::click() {
    if (onClick) onClick(*this);
}

void Control::dblClick() {
    if (onDblClick) onDblClick(*this);
}

}  // namespace sq8l::gui
