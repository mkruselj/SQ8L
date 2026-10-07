// TextRenderer for the editor's Windows-font text (status bar, program name, menus),
// using stb_truetype and the embedded Liberation Sans (metric-compatible with Arial).
// Integer advances like GDI (lfHeight < 0 means character (em) height in pixels).
// antialias = false renders like Windows XP with ClearType off (binary glyphs); true
// blends the glyph coverage with the background (smoother, as preferred by the user).
#pragma once

#include <map>
#include <memory>
#include <string>
#include <tuple>

#include "../TextRenderer.h"

namespace sq8l::gui {

class StbTextRenderer final : public TextRenderer {
public:
    explicit StbTextRenderer(bool antialias = false);
    ~StbTextRenderer() override;

    int textWidth(const Font& font, const std::string& text) override;
    int textHeight(const Font& font) override;
    void drawText(Bitmap& target, int x, int y, const Rect& clip, const Font& font, const std::string& text) override;

private:
    struct Face;
    struct Glyph {
        int advance = 0, xoff = 0, yoff = 0, w = 0, h = 0;
        std::basic_string<uint8_t> mask;  // w*h coverage 0..255 (binary mode: 0 or 255)
    };
    struct Sized;
    Sized& sized(const Font& font);

    bool antialias_;
    std::unique_ptr<Face> regular_, italic_;
    std::map<std::tuple<bool, int>, std::unique_ptr<Sized>> cache_;
};

}  // namespace sq8l::gui
