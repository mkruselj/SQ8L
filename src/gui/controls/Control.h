// Base class of the editor controls: the parts of VCL's TControl / TWinControl /
// TCustomControl that the original components rely on (layer 2 of docs/GUI_ARCHITECTURE.md).
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "../Bitmap.h"
#include "../Canvas.h"

namespace sq8l::gui {

// TMouseButton
enum class MouseButton : uint8_t { Left = 0, Right = 1, Middle = 2 };

// TShiftState bits (set of ssShift, ssAlt, ssCtrl, ssLeft, ssRight, ssMiddle, ssDouble).
using ShiftState = uint32_t;
enum : ShiftState {
    ssShift = 1u << 0,
    ssAlt = 1u << 1,
    ssCtrl = 1u << 2,
    ssLeft = 1u << 3,
    ssRight = 1u << 4,
    ssMiddle = 1u << 5,
    ssDouble = 1u << 6,
};

class Control;

// What a control needs from its window system (implemented by EditorView).
class ControlHost {
public:
    virtual ~ControlHost() = default;
    virtual Control* mouseCapture() const = 0;    // Mouse.Capture / GetCapture
    virtual void setMouseCapture(Control* c) = 0;  // Mouse.Capture := c (nullptr releases)
    virtual void setFocus(Control* c) = 0;         // TWinControl.SetFocus
    virtual TextRenderer* textRenderer() = 0;
};

class Control {
public:
    // windowed: a TWinControl (own window and surface; receives mouse messages directly).
    Control(std::string name, bool windowed) : name_(std::move(name)), windowed_(windowed) {}
    virtual ~Control() = default;
    Control(const Control&) = delete;
    Control& operator=(const Control&) = delete;

    const std::string& name() const { return name_; }
    bool windowed() const { return windowed_; }

    // ---------------------------------------------------------------- geometry
    // TControl.Left/Top/Width/Height, in parent client coordinates.
    int left() const { return left_; }
    int top() const { return top_; }
    int width() const { return width_; }
    int height() const { return height_; }
    Rect bounds() const { return boundsRect(left_, top_, width_, height_); }
    Rect clientRect() const { return Rect{0, 0, width_, height_}; }
    // TControl.SetBounds (vmt+0x7c). Resizing keeps the surface's top-left content.
    virtual void setBounds(int left, int top, int width, int height);
    void setWidth(int w) { setBounds(left_, top_, w, height_); }   // FUN_00440e58
    void setHeight(int h) { setBounds(left_, top_, width_, h); }  // FUN_00440e78

    bool visible() const { return visible_; }
    void setVisible(bool v) {
        visible_ = v;
        invalidate();
    }
    bool enabled = true;
    std::string hint;          // TControl.Hint (+0x70)
    bool captureMouse = true;  // csCaptureMouse in ControlStyle
    bool clickEvents = true;   // csClickEvents in ControlStyle

    Control* parent = nullptr;          // parent windowed control (nullptr = the form)
    std::vector<Control*> children;     // graphic (non-windowed) children, painted on our canvas
    ControlHost* host = nullptr;

    // ---------------------------------------------------------------- painting
    void invalidate() { invalid_ = true; }
    bool invalid() const { return invalid_; }
    // WM_ERASEBKGND / double-buffer erase colour: ParentColor -> the form colour.
    Color eraseColor = rgb(0x1A, 0x1B, 0x24);
    // TControl.Paint in client coordinates.
    virtual void paint(Canvas& canvas) = 0;
    // Full WM_PAINT of a windowed control into its surface: erase with eraseColor, paint,
    // then paint graphic children (TWinControl.PaintControls).
    void paintWindow(TextRenderer* text);
    Bitmap& surface() { return surface_; }
    const Bitmap& surface() const { return surface_; }

    // ---------------------------------------------------------------- mouse
    // VCL dynamic methods (client coordinates). The defaults fire the TControl events.
    virtual void mouseDown(MouseButton button, ShiftState shift, int x, int y);
    virtual void mouseMove(ShiftState shift, int x, int y);
    virtual void mouseUp(MouseButton button, ShiftState shift, int x, int y);
    virtual void click();     // TControl.Click (dynamic -20)
    virtual void dblClick();  // TControl.DblClick (dynamic -21)

    // TControl events: OnMouseDown (+0xb8), OnMouseMove (+0xc0), OnMouseUp (+0xc8),
    // OnClick (+0x108), OnDblClick (+0x110). Sender first, like Delphi.
    std::function<void(Control&, MouseButton, ShiftState, int, int)> onMouseDown, onMouseUp;
    std::function<void(Control&, ShiftState, int, int)> onMouseMove;
    std::function<void(Control&)> onClick, onDblClick;

    // VCL state: csClicked in ControlState (set by WM_LBUTTONDOWN when csClickEvents).
    bool clicked = false;

protected:
    void setMouseCapture(Control* c) {
        if (host) host->setMouseCapture(c);
    }
    Control* mouseCaptureControl() const { return host ? host->mouseCapture() : nullptr; }
    void setFocus() {
        if (host) host->setFocus(this);
    }

    std::string name_;
    bool windowed_;
    int left_ = 0, top_ = 0, width_ = 0, height_ = 0;
    bool visible_ = true;
    bool invalid_ = true;
    Bitmap surface_;
};

}  // namespace sq8l::gui
