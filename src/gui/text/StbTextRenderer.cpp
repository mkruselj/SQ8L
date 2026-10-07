#include "StbTextRenderer.h"

#include <cmath>
#include <vector>

#define STB_TRUETYPE_IMPLEMENTATION
#define STBTT_STATIC
#include "../../../third_party/stb/stb_truetype.h"

namespace sq8l::gui {

namespace fonts {
extern const uint8_t kLiberationSansRegular[];
extern const size_t kLiberationSansRegularSize;
extern const uint8_t kLiberationSansItalic[];
extern const size_t kLiberationSansItalicSize;
}  // namespace fonts

struct StbTextRenderer::Face {
    stbtt_fontinfo info{};
    explicit Face(const uint8_t* data) { stbtt_InitFont(&info, data, stbtt_GetFontOffsetForIndex(data, 0)); }
};

struct StbTextRenderer::Sized {
    float scale = 1;
    int ascent = 0, descent = 0;
    const Face* face = nullptr;
    bool antialias = false;
    std::map<int, Glyph> glyphs;

    const Glyph& glyph(int cp) {
        auto it = glyphs.find(cp);
        if (it != glyphs.end()) return it->second;
        Glyph g;
        int adv = 0, lsb = 0;
        stbtt_GetCodepointHMetrics(&face->info, cp, &adv, &lsb);
        g.advance = static_cast<int>(std::lround(adv * scale));
        int x0, y0, x1, y1;
        stbtt_GetCodepointBitmapBox(&face->info, cp, scale, scale, &x0, &y0, &x1, &y1);
        g.w = x1 - x0;
        g.h = y1 - y0;
        g.xoff = x0;
        g.yoff = y0;
        if (g.w > 0 && g.h > 0) {
            std::vector<uint8_t> cov(static_cast<size_t>(g.w) * g.h);
            stbtt_MakeCodepointBitmap(&face->info, cov.data(), g.w, g.h, g.w, scale, scale, cp);
            g.mask.resize(cov.size());
            for (size_t i = 0; i < cov.size(); i++) g.mask[i] = antialias ? cov[i] : (cov[i] >= 128 ? 255 : 0);
        }
        return glyphs.emplace(cp, std::move(g)).first->second;
    }
};

StbTextRenderer::StbTextRenderer(bool antialias)
    : antialias_(antialias),
      regular_(std::make_unique<Face>(fonts::kLiberationSansRegular)),
      italic_(std::make_unique<Face>(fonts::kLiberationSansItalic)) {}

StbTextRenderer::~StbTextRenderer() = default;

StbTextRenderer::Sized& StbTextRenderer::sized(const Font& font) {
    const int px = font.height < 0 ? -font.height : (font.height > 0 ? font.height : 11);
    const auto key = std::make_tuple(font.italic, font.height);
    auto it = cache_.find(key);
    if (it != cache_.end()) return *it->second;
    auto s = std::make_unique<Sized>();
    s->face = font.italic ? italic_.get() : regular_.get();
    s->antialias = antialias_;
    // Negative lfHeight: em height; positive: cell height (ascent + descent).
    s->scale = font.height < 0 ? stbtt_ScaleForMappingEmToPixels(&s->face->info, static_cast<float>(px))
                               : stbtt_ScaleForPixelHeight(&s->face->info, static_cast<float>(px));
    int a, d, g;
    stbtt_GetFontVMetrics(&s->face->info, &a, &d, &g);
    s->ascent = static_cast<int>(std::lround(a * s->scale));
    s->descent = static_cast<int>(std::lround(-d * s->scale));
    return *cache_.emplace(key, std::move(s)).first->second;
}

int StbTextRenderer::textWidth(const Font& font, const std::string& text) {
    Sized& s = sized(font);
    int w = 0;
    for (unsigned char c : text) w += s.glyph(c).advance;
    return w;
}

int StbTextRenderer::textHeight(const Font& font) {
    Sized& s = sized(font);
    return s.ascent + s.descent;
}

void StbTextRenderer::drawText(Bitmap& target, int x, int y, const Rect& clip, const Font& font, const std::string& text) {
    Sized& s = sized(font);
    const Rect c = clip.intersect(Rect{0, 0, target.width(), target.height()});
    int pen = x;
    const int baseline = y + s.ascent;
    for (unsigned char ch : text) {
        const Glyph& g = s.glyph(ch);
        for (int gy = 0; gy < g.h; gy++) {
            const int py = baseline + g.yoff + gy;
            if (py < c.top || py >= c.bottom) continue;
            for (int gx = 0; gx < g.w; gx++) {
                const int px = pen + g.xoff + gx;
                if (px < c.left || px >= c.right) continue;
                const unsigned a = g.mask[static_cast<size_t>(gy) * g.w + gx];
                if (a == 0) continue;
                if (a == 255) {
                    target.setPixel(px, py, font.color);
                    continue;
                }
                const Color bg = target.pixel(px, py);
                const auto mix = [a](unsigned f, unsigned b) { return (f * a + b * (255 - a) + 127) / 255; };
                target.setPixel(px, py, rgb(static_cast<int>(mix((font.color >> 16) & 0xFF, (bg >> 16) & 0xFF)),
                                            static_cast<int>(mix((font.color >> 8) & 0xFF, (bg >> 8) & 0xFF)),
                                            static_cast<int>(mix(font.color & 0xFF, bg & 0xFF))));
            }
        }
        pen += g.advance;
    }
}

}  // namespace sq8l::gui
