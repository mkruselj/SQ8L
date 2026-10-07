#include "LcdDisplay.h"

namespace sq8l::gui {

LcdDisplay::LcdDisplay(std::string name) : Control(std::move(name), true) {
    // TLCD3_v011 (0x4579c0)
    charMap_.fill(-1);
    numAttr_ = 1;
    redrawAll_ = true;
    anyDirty_ = true;
    charW_ = 8;
    charH_ = 8;
    gapX_ = 0;
    gapY_ = 0;
    frameW_ = 4;
    frameH_ = 4;
    setWidthChar(8);
    setHeightChar(1);
}

void LcdDisplay::setAniGif(const Sprite* gif) {
    gif_ = gif;
    if (!gif) {
        charW_ = 8;
        charH_ = 8;
    } else {
        charW_ = gif->frameWidth();
        charH_ = gif->frameHeight();
    }
    setWidthChar(cols_);
    setHeightChar(rows_);
    redrawAll_ = true;
    invalidate();
}

void LcdDisplay::resizeCells() {
    // DynArraySetLength(cells, 2, [rows, cols]) keeps overlapping cells, then FUN_00457f38
    // resets every cell anyway.
    cells_.resize(rows_);
    for (auto& r : cells_) r.resize(cols_);
    clearCells();
}

void LcdDisplay::setWidthChar(int cols) {
    cols_ = cols < 1 ? 1 : cols;
    resizeCells();
    widthPix_ = (charW_ + gapX_) * cols_ - gapX_;
    setWidth(frameW_ * 2 + widthPix_);
    redrawAll_ = true;
    invalidate();
}

void LcdDisplay::setHeightChar(int rows) {
    rows_ = rows < 1 ? 1 : rows;
    resizeCells();
    heightPix_ = (charH_ + gapY_) * rows_ - gapY_;
    setHeight(frameH_ * 2 + heightPix_);
    redrawAll_ = true;
    invalidate();
}

void LcdDisplay::setWidthPix(int px) { setWidthChar(px < 1 ? 0 : px / charW_); }

void LcdDisplay::setHeightPix(int px) { setHeightChar(px < 1 ? 0 : px / charH_); }

void LcdDisplay::setCharGapX(int gap) {
    gapX_ = gap;
    setWidthChar(cols_);
}

void LcdDisplay::setCharGapY(int gap) {
    gapY_ = gap;
    setHeightChar(rows_);
}

void LcdDisplay::setFrameWidth(int w) {
    frameW_ = w < 1 ? 0 : w;
    setWidthChar(cols_);
}

void LcdDisplay::setFrameHeight(int h) {
    frameH_ = h < 1 ? 0 : h;
    setHeightChar(rows_);
}

void LcdDisplay::setColor(int which, Color c) {
    auto half = [](Color v) { return ((v >> 1) & 0x7F0000) | ((v >> 1) & 0x7F00) | ((v & 0xFF) >> 1); };
    if (which == 0) {
        colors_[0] = c;
    } else if (which == 1) {
        colors_[1] = c;
        colors_[3] = half(c);
    } else if (which == 2) {
        colors_[2] = c;
        colors_[4] = half(c);
    } else {
        return;
    }
    redrawAll_ = true;
    invalidate();
}

void LcdDisplay::setCharMap(const std::string& chars, int numAttr) {
    charMap_.fill(0);
    for (int i = 0; i < 256 && i < static_cast<int>(chars.size()); i++) {
        auto c = static_cast<uint8_t>(chars[i]);
        if (c == 0) break;
        charMap_[c] = i;
    }
    numAttr_ = numAttr;
}

void LcdDisplay::mapLowerToUpper() {
    for (int i = 0; i < 26; i++) charMap_['a' + i] = charMap_['A' + i];
}

void LcdDisplay::clearCells() {
    if (cells_.empty()) return;
    for (auto& r : cells_)
        for (auto& c : r) c = Cell{1, 0, 0};
    anyDirty_ = true;
}

void LcdDisplay::setCell(int col, int row, uint8_t ch, int32_t attr, bool dirty) {
    Cell& c = cells_[row][col];
    c.ch = ch;
    c.attr = attr;
    c.dirty = dirty ? 1 : 0;
    if (dirty) anyDirty_ = true;
}

void LcdDisplay::writeText(int col, int row, const std::string& text, int attr) {
    int len = static_cast<int>(text.size());
    if (len <= 0 || cells_.empty() || row < 0 || row >= rows_ || col >= cols_) return;
    std::vector<Cell>& r = cells_[row];
    if (r.empty()) return;
    int i = 1;            // 1-based index of the first character written
    int n = len;          // 1-based index of the last one
    int last = len + col - 1;
    if (col < 0) {
        i = 1 - col;
        col = 0;
    }
    if (last >= cols_) n -= last - cols_ + 1;
    for (int k = i; k <= n; k++, col++) {
        auto ch = static_cast<uint8_t>(text[k - 1]);
        Cell& c = r[col];
        if (ch != c.ch || attr != c.attr) {
            c.ch = ch;
            c.attr = attr;
            c.dirty = 1;
        }
    }
    anyDirty_ = true;
}

void LcdDisplay::update() {
    if (redrawAll_ || anyDirty_) invalidate();
}

void LcdDisplay::cellAt(int x, int y, int& col, int& row) const {
    int cw = charW_ + gapX_;
    col = cw < 1 ? 0 : (x - frameW_) / cw;
    int ch = charH_ + gapY_;
    row = ch < 1 ? 0 : (y - frameH_) / ch;
}

void LcdDisplay::drawBorder() {
    // FUN_00458180: background and a two-pixel bevel, drawn into the back buffer.
    Canvas c(back_);
    int w = back_.width(), h = back_.height();
    c.brushColor = colors_[0];
    c.penColor = colors_[0];
    c.fillRect(Rect{0, 0, width_, height_});
    c.penColor = colors_[2];
    c.moveTo(0, h - 1);
    c.lineTo(w - 1, h - 1);
    c.lineTo(w - 1, 0);
    c.penColor = colors_[1];
    c.lineTo(0, 0);
    c.lineTo(0, h - 1);
    c.penColor = colors_[4];
    c.moveTo(1, h - 2);
    c.lineTo(w - 2, h - 2);
    c.lineTo(w - 2, 1);
    c.penColor = colors_[3];
    c.lineTo(1, 1);
    c.lineTo(1, h - 2);
}

void LcdDisplay::paint(Canvas& canvas) {
    // FUN_00458118: follow the control size (width first, then height), redraw everything.
    bool resized = false;
    if (back_.width() != width_ || back_.height() != height_) {
        back_.resize(width_, height_, 0);
        redrawAll_ = true;
        resized = true;
    }
    if (resized) {
        drawBorder();
        canvas.draw(0, 0, back_.view());
    }
    if (redrawAll_ || anyDirty_) {
        if (cells_.empty() || gif_ == nullptr) return;  // flags stay set, nothing drawn
        Canvas bc(back_);
        int y = frameH_;
        for (int row = 0; row < rows_; row++) {
            std::vector<Cell>& r = cells_[row];
            if (!r.empty()) {
                int x = frameW_;
                for (int col = 0; col < cols_; col++) {
                    Cell& c = r[col];
                    if (redrawAll_ || c.dirty) {
                        c.dirty = 0;
                        int glyph = charMap_[c.ch];
                        if (glyph >= 0) {
                            int attr = c.attr;
                            if (attr >= numAttr_) attr = 0;
                            bc.draw(x, y, gif_->frame(attr + glyph * numAttr_));
                        }
                    }
                    x += charW_ + gapX_;
                }
            }
            y += charH_ + gapY_;
        }
    }
    canvas.draw(0, 0, back_.view());
    redrawAll_ = false;
    anyDirty_ = false;
}

void LcdDisplay::mouseDown(MouseButton button, ShiftState shift, int x, int y) {
    Control::mouseDown(button, shift, x, y);
    setFocus();
    if (onCellMouseDown) {
        int col, row;
        cellAt(x, y, col, row);
        onCellMouseDown(*this, button, shift, col, row);
    }
}

void LcdDisplay::mouseUp(MouseButton button, ShiftState shift, int x, int y) {
    Control::mouseUp(button, shift, x, y);
    if (onCellMouseUp) {
        int col, row;
        cellAt(x, y, col, row);
        onCellMouseUp(*this, button, shift, col, row);
    }
}

void LcdDisplay::mouseMove(ShiftState shift, int x, int y) {
    lastMouseX = x;
    lastMouseY = y;
    Control::mouseMove(shift, x, y);
    if (onCellMouseMove) {
        int col, row;
        cellAt(x, y, col, row);
        onCellMouseMove(*this, shift, col, row);
    }
}

void LcdDisplay::dblClick() {
    Control::dblClick();
    if (onDblClickPos) onDblClickPos(*this, lastMouseX, lastMouseY);
    if (onCellDblClick) {
        int col, row;
        cellAt(lastMouseX, lastMouseY, col, row);
        onCellDblClick(*this, col, row);
    }
}

}  // namespace sq8l::gui
