// The "jumping mouse" (unit mouseJump, CmouseJump 0x47921c, one global instance): the cursor
// position is saved before a popup menu or a knob drag and restored after it (readme E.5).
// Only the first save counts until a restore (which only happens for the same owner, or with
// owner 0): a popup dismissed without choosing keeps the position saved.
#pragma once

#include "PlatformUi.h"

namespace sq8l::gui {

class MouseJump {
public:
    // FUN_004792a0
    void save(PlatformUi& ui, const void* owner) {
        if (!saved_) {
            owner_ = owner;
            pos_ = ui.cursorPos();
            saved_ = true;
        }
    }
    // FUN_00479328
    void restore(PlatformUi& ui, const void* owner) {
        if (saved_ && (owner == nullptr || owner == owner_)) ui.setCursorPos(pos_);
        saved_ = false;
        owner_ = nullptr;
    }
    bool saved() const { return saved_; }     // +0x04
    Point position() const { return pos_; }   // +0x08 / +0x0c (screen = form coordinates here)
    const void* owner() const { return owner_; }  // +0x10

private:
    bool saved_ = false;
    Point pos_;
    const void* owner_ = nullptr;
};

}  // namespace sq8l::gui
