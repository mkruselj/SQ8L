// Text drawn with Windows fonts (status bar labels, program name edit box, menus) is the
// only part of the editor that is not made of the original's bitmaps. It is delegated to a
// TextRenderer supplied by the platform layer; without one, text is simply not drawn.
#pragma once

#include <string>

#include "Bitmap.h"

namespace sq8l::gui {

// A VCL TFont as used by the form (Font.Name, Font.Height in pixels: negative = character
// height without internal leading, Font.Style, Font.Color).
struct Font {
    std::string face = "MS Sans Serif";
    int height = -11;
    bool bold = false, italic = false;
    Color color = 0;
};

class TextRenderer {
public:
    virtual ~TextRenderer() = default;
    // Advance width of `text` in pixels.
    virtual int textWidth(const Font& font, const std::string& text) = 0;
    // Line height (tmHeight = ascent + descent) in pixels.
    virtual int textHeight(const Font& font) = 0;
    // Draw `text` with its top-left cell corner at (x, y) (TA_TOP | TA_LEFT, transparent
    // background), clipped to `clip` (target coordinates).
    virtual void drawText(Bitmap& target, int x, int y, const Rect& clip, const Font& font,
                          const std::string& text) = 0;
};

}  // namespace sq8l::gui
