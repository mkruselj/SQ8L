// The standard VCL controls of the form: TPanel + TLabel (status bar, voices counter),
// TEdit (program name) and TImage without a picture (menu click areas on the background).
// Their text uses Windows fonts and goes through the TextRenderer (see TextRenderer.h).
#pragma once

#include <string>

#include "Control.h"

namespace sq8l::gui {

// TPanel, BevelOuter = bvNone, empty caption: a colour fill (TCustomPanel.Paint).
class Panel : public Control {
public:
    explicit Panel(std::string name) : Control(std::move(name), true) {}
    Color color = rgb(0x1A, 0x1B, 0x24);
    void paint(Canvas& canvas) override;
};

// TLabel (graphic control painted on its parent panel's canvas).
class Label : public Control {
public:
    enum Alignment { LeftJustify, RightJustify, Center };
    explicit Label(std::string name) : Control(std::move(name), false) {}

    const std::string& caption() const { return caption_; }
    // Sets the caption; with autoSize the bounds follow the text extent (AdjustBounds:
    // DT_CALCRECT, keeping the right edge for taRightJustify). Needs a TextRenderer to measure.
    void setCaption(const std::string& text, TextRenderer* measure = nullptr);
    Font font;
    Color color = rgb(0x1A, 0x1B, 0x24);  // ParentColor
    Alignment alignment = LeftJustify;
    bool autoSize = true;
    bool transparent = false;
    void paint(Canvas& canvas) override;  // TCustomLabel.Paint

private:
    std::string caption_;
};

// TEdit with BorderStyle = bsNone (program name). Only the look: colour fill + text at (1, 0)
// (the oracle's EDIT rendering). Keyboard editing belongs to the platform layer.
class NameEdit : public Control {
public:
    explicit NameEdit(std::string name) : Control(std::move(name), true) {}
    std::string text;
    Color color = 0;  // clBlack
    Font font;
    int maxLength = 15;
    bool upperCase = true;  // CharCase = ecUpperCase
    void setText(const std::string& t);
    void paint(Canvas& canvas) override;
};

// TImage without a picture: an invisible click/hint area on the form (File, Options, Info,
// Panic). Paints nothing.
class ImageArea : public Control {
public:
    explicit ImageArea(std::string name) : Control(std::move(name), false) {}
    void paint(Canvas&) override {}
};

}  // namespace sq8l::gui
