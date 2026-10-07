// Port of TLCD3 (unit LCD3, 0x45733c-0x4587d8): a character display made of sprite
// characters (the main VFD and the red 7-segment program number).
//
// Model: a grid of cells {dirty, char, attribute}. The character code is mapped to a glyph
// index with a 256-entry table built from a charset string (setCharMap); each glyph has
// `numAttr` consecutive frames in the GIF (charGIF: 4 = plain, underline, dot, underline+dot),
// so the frame is  attr + glyph * numAttr  (attr >= numAttr -> 0).
// Painting is double buffered exactly like the original: the back buffer persists, only
// dirty cells are redrawn, the border/background is drawn when the back buffer is resized.
#pragma once

#include <array>
#include <functional>
#include <string>
#include <vector>

#include "../Sprite.h"
#include "Control.h"

namespace sq8l::gui {

class LcdDisplay : public Control {
public:
    struct Cell {             // 6-byte record of the row arrays at +0x628
        uint8_t dirty = 1;    // +0
        uint8_t ch = 0;       // +1
        int32_t attr = 0;     // +2
    };

    explicit LcdDisplay(std::string name);  // TLCD3.Create defaults

    // ------------------------------------------------------------ configuration
    void setAniGif(const Sprite* gif);  // FUN_00457bf4: char size from the GIF (8x8 without)
    void setWidthChar(int cols);        // FUN_00457cb4 (cols < 1 -> 1). Clears all cells.
    void setHeightChar(int rows);       // FUN_00457d3c (rows < 1 -> 1). Clears all cells.
    void setWidthPix(int px);           // FUN_00457dc4: setWidthChar(px / charWidth)
    void setHeightPix(int px);          // FUN_00457dec
    void setCharGapX(int gap);          // FUN_00457e14
    void setCharGapY(int gap);          // FUN_00457e28
    void setFrameWidth(int w);          // FUN_00457e3c (< 1 -> 0)
    void setFrameHeight(int h);         // FUN_00457e5c
    // FUN_00457e7c. which: 0 = colBack, 1 = colBorderLo (top/left lines), 2 = colBorderHi
    // (bottom/right lines). Border colours also get a half-intensity inner line.
    // Note: like the original, colours are only used when the back buffer is (re)created.
    void setColor(int which, Color c);
    // FUN_00457b90: map = 0 for every code, then map[chars[i]] = i; numAttr frames per glyph.
    void setCharMap(const std::string& chars, int numAttr);
    void mapLowerToUpper();  // FUN_00457bd4: map['a'+i] = map['A'+i]
    void setCharMapRaw(const std::array<int32_t, 256>& map, int numAttr) {
        charMap_ = map;
        numAttr_ = numAttr;
    }
    bool captureKeys = false;  // +0x678 (CM_WANTSPECIALKEY)

    // ------------------------------------------------------------ content
    // FUN_00457fb4: write `text` at (col, row) with attribute `attr`; clipped to the grid
    // (col may be negative). Cells whose char/attr change become dirty.
    void writeText(int col, int row, const std::string& text, int attr);
    void clearCells();  // FUN_00457f38: every cell = {dirty, 0, 0}
    void update();      // FUN_0045832c: repaint if something changed
    // Direct cell access (does not touch dirty flags unless noted).
    const Cell& cell(int col, int row) const { return cells_[row][col]; }
    void setCell(int col, int row, uint8_t ch, int32_t attr, bool dirty = true);
    bool needsRepaint() const { return redrawAll_ || anyDirty_; }
    bool redrawAllPending() const { return redrawAll_; }  // +0x630
    bool anyDirtyPending() const { return anyDirty_; }    // +0x631

    // ------------------------------------------------------------ geometry
    int cols() const { return cols_; }
    int rows() const { return rows_; }
    int charWidth() const { return charW_; }
    int charHeight() const { return charH_; }
    int charGapX() const { return gapX_; }
    int charGapY() const { return gapY_; }
    int frameWidth() const { return frameW_; }
    int frameHeight() const { return frameH_; }
    int widthPix() const { return widthPix_; }
    int heightPix() const { return heightPix_; }
    int numAttr() const { return numAttr_; }
    const std::array<int32_t, 256>& charMap() const { return charMap_; }
    const Sprite* aniGif() const { return gif_; }
    Color color(int which) const { return colors_[which]; }
    int charX(int col) const { return (charW_ + gapX_) * col + frameW_; }  // FUN_00458528
    int charY(int row) const { return (charH_ + gapY_) * row + frameH_; }  // FUN_00458540
    void cellAt(int x, int y, int& col, int& row) const;                    // FUN_00458558

    // ------------------------------------------------------------ events
    // +0x648 / +0x650: mouse down/up converted to cells (Sender, Button, Shift, Col, Row).
    std::function<void(LcdDisplay&, MouseButton, ShiftState, int, int)> onCellMouseDown, onCellMouseUp;
    // +0x658: mouse move in cells (Sender, Shift, Col, Row).
    std::function<void(LcdDisplay&, ShiftState, int, int)> onCellMouseMove;
    // +0x660: double click at the last mouse position in pixels; +0x668: in cells.
    std::function<void(LcdDisplay&, int, int)> onDblClickPos, onCellDblClick;
    int lastMouseX = 0, lastMouseY = 0;  // +0x670 / +0x674

    void paint(Canvas& canvas) override;  // TLCD3.Paint (0x458344)
    void mouseDown(MouseButton button, ShiftState shift, int x, int y) override;  // 0x4585c0
    void mouseUp(MouseButton button, ShiftState shift, int x, int y) override;    // 0x458630
    void mouseMove(ShiftState shift, int x, int y) override;                      // 0x458694
    void dblClick() override;                                                     // 0x458708

    const Bitmap& backBuffer() const { return back_; }

private:
    void resizeCells();      // SetLength(cells, rows, cols) + clearCells
    void drawBorder();       // FUN_00458180 (into the back buffer)

    const Sprite* gif_ = nullptr;                  // +0x220
    int charW_ = 8, charH_ = 8;                    // +0x1f8 / +0x1fc
    int gapX_ = 0, gapY_ = 0;                      // +0x200 / +0x204
    int frameW_ = 4, frameH_ = 4;                  // +0x208 / +0x20c
    int widthPix_ = 0, heightPix_ = 0;             // +0x210 / +0x214 (glyph area)
    int cols_ = 1, rows_ = 1;                      // +0x218 / +0x21c
    int numAttr_ = 1;                              // +0x224
    std::array<int32_t, 256> charMap_;             // +0x228
    std::vector<std::vector<Cell>> cells_;         // +0x628
    Bitmap back_;                                  // +0x62c
    bool redrawAll_ = true;                        // +0x630
    bool anyDirty_ = true;                         // +0x631
    // +0x634 colBack, +0x638 colBorderLo, +0x63c colBorderHi, +0x640 / +0x644 halves
    std::array<Color, 5> colors_{};
};

}  // namespace sq8l::gui
