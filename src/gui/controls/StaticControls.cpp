#include "StaticControls.h"

#include <cctype>

namespace sq8l::gui {

void Panel::paint(Canvas& canvas) {
    canvas.brushColor = color;
    canvas.fillRect(clientRect());
}

void Label::setCaption(const std::string& text, TextRenderer* measure) {
    caption_ = text;
    if (autoSize && measure) {
        // DoDrawText with DT_CALCRECT measures an empty caption as " ".
        std::string t = text.empty() ? std::string(" ") : text;
        int w = measure->textWidth(font, t);
        int h = measure->textHeight(font);
        int x = left_;
        if (alignment == RightJustify) x += width_ - w;
        setBounds(x, top_, w, h);
    }
    invalidate();
    if (parent) parent->invalidate();
}

void Label::paint(Canvas& canvas) {
    if (!transparent) {
        canvas.brushColor = color;
        canvas.fillRect(clientRect());
    }
    canvas.font = font;
    int x = 0;
    if (alignment != LeftJustify) {
        int w = canvas.textWidth(caption_);
        x = alignment == RightJustify ? width_ - w : (width_ - w) / 2;
    }
    canvas.textOut(x, 0, caption_);
}

void NameEdit::setText(const std::string& t) {
    std::string s = t.substr(0, static_cast<size_t>(maxLength));
    if (upperCase)
        for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    text = s;
    invalidate();
}

void NameEdit::paint(Canvas& canvas) {
    canvas.brushColor = color;
    canvas.fillRect(clientRect());
    canvas.font = font;
    canvas.textOut(1, 0, text);
}

}  // namespace sq8l::gui
