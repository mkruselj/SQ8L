// Graphics core: RGB bitmaps (layer 1 of docs/GUI_ARCHITECTURE.md).
//
// Pixels are 0x00RRGGBB (`Color`). Delphi's TColor is 0x00BBGGRR: convert with fromTColor().
#pragma once

#include <algorithm>
#include <cstdint>
#include <vector>

namespace sq8l::gui {

using Color = uint32_t;  // 0x00RRGGBB

constexpr Color rgb(int r, int g, int b) {
    return (static_cast<Color>(r & 0xFF) << 16) | (static_cast<Color>(g & 0xFF) << 8) | static_cast<Color>(b & 0xFF);
}
// Delphi TColor / Win32 COLORREF (0x00BBGGRR) <-> Color.
constexpr Color fromTColor(uint32_t c) { return ((c & 0xFF) << 16) | (c & 0xFF00) | ((c >> 16) & 0xFF); }
constexpr uint32_t toTColor(Color c) { return ((c & 0xFF) << 16) | (c & 0xFF00) | ((c >> 16) & 0xFF); }

// GDI-style rectangle: right/bottom exclusive.
struct Rect {
    int left = 0, top = 0, right = 0, bottom = 0;
    constexpr int width() const { return right - left; }
    constexpr int height() const { return bottom - top; }
    constexpr bool empty() const { return right <= left || bottom <= top; }
    constexpr bool contains(int x, int y) const { return x >= left && x < right && y >= top && y < bottom; }
    Rect intersect(const Rect& o) const {
        return Rect{std::max(left, o.left), std::max(top, o.top), std::min(right, o.right), std::min(bottom, o.bottom)};
    }
    Rect offset(int dx, int dy) const { return Rect{left + dx, top + dy, right + dx, bottom + dy}; }
};

inline Rect boundsRect(int x, int y, int w, int h) { return Rect{x, y, x + w, y + h}; }

// Read-only view of pixels (a sprite frame, a bitmap). Null view == Delphi nil bitmap.
struct ImageView {
    const Color* px = nullptr;
    int width = 0, height = 0, stride = 0;  // stride in pixels
    explicit operator bool() const { return px != nullptr; }
    Color at(int x, int y) const { return px[y * stride + x]; }
};

class Bitmap {
public:
    Bitmap() = default;
    Bitmap(int w, int h, Color fill = 0) { resize(w, h, fill); }

    int width() const { return w_; }
    int height() const { return h_; }
    Color* row(int y) { return px_.data() + static_cast<size_t>(y) * w_; }
    const Color* row(int y) const { return px_.data() + static_cast<size_t>(y) * w_; }
    Color pixel(int x, int y) const { return px_[static_cast<size_t>(y) * w_ + x]; }
    void setPixel(int x, int y, Color c) {
        if (x >= 0 && y >= 0 && x < w_ && y < h_) px_[static_cast<size_t>(y) * w_ + x] = c;
    }
    std::vector<Color>& pixels() { return px_; }
    const std::vector<Color>& pixels() const { return px_; }
    ImageView view() const { return ImageView{px_.data(), w_, h_, w_}; }

    void fill(Color c) { std::fill(px_.begin(), px_.end(), c); }

    // Change the size keeping the overlapping top-left content; new pixels get `fill`
    // (like a window surface being resized).
    void resize(int w, int h, Color fill = 0) {
        w = std::max(w, 0);
        h = std::max(h, 0);
        if (w == w_ && h == h_) return;
        std::vector<Color> n(static_cast<size_t>(w) * h, fill);
        int cw = std::min(w, w_), ch = std::min(h, h_);
        for (int y = 0; y < ch; y++)
            std::copy(px_.begin() + static_cast<size_t>(y) * w_, px_.begin() + static_cast<size_t>(y) * w_ + cw,
                      n.begin() + static_cast<size_t>(y) * w);
        px_.swap(n);
        w_ = w;
        h_ = h;
    }

    // Copy `src` with its top-left at (x, y), clipped to this bitmap.
    void blit(int x, int y, const ImageView& src) {
        if (!src) return;
        int x0 = std::max(x, 0), y0 = std::max(y, 0);
        int x1 = std::min(x + src.width, w_), y1 = std::min(y + src.height, h_);
        for (int yy = y0; yy < y1; yy++)
            std::copy(src.px + (yy - y) * src.stride + (x0 - x), src.px + (yy - y) * src.stride + (x1 - x),
                      row(yy) + x0);
    }

    // Export as packed RGB24 (3 bytes per pixel, row-major).
    void toRGB24(uint8_t* out) const {
        for (size_t i = 0; i < px_.size(); i++) {
            out[3 * i] = static_cast<uint8_t>(px_[i] >> 16);
            out[3 * i + 1] = static_cast<uint8_t>(px_[i] >> 8);
            out[3 * i + 2] = static_cast<uint8_t>(px_[i]);
        }
    }

private:
    int w_ = 0, h_ = 0;
    std::vector<Color> px_;
};

}  // namespace sq8l::gui
