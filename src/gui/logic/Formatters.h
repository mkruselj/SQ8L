// Parameter value formatters of the VFD pages and the Delphi string helpers they use.
//
// Original: the default formatters of unit lcdControl (0x458ff8 / 0x45903c / 0x45907c, chosen
// by FUN_00458f98 from the parameter range) and the custom formatters / popup text functions of
// unit editBuffer (0x461114..0x461bac, all "procedure(Sender; Page: Integer; Param: TParam;
// var S: string; Value: Integer)"). Like the originals they write into a `var` string: a few of
// them leave it untouched for unexpected values, and the callers rely on the previous content.
#pragma once

#include <string>

#include "LcdData.h"

namespace sq8l::gui {

namespace delphi {
std::string intToStr(int v);                  // IntToStr / Str(v)
std::string strWidth(int v, int width);       // Str(v:width): right justified with spaces
void zeroPad(std::string& s, int n);          // FUN_00417954: prepend '0' up to length n
std::string intToStrZ(int v, int n);          // FUN_00417994: zero padded, sign counts in n
int numDigits(int v);                         // FUN_00417a04: decimal digits (1 for v < 10)
std::string stringOfChar(char c, int n);      // StringOfChar (n <= 0: empty)
}  // namespace delphi

// Value text of a parameter: `width` is ClcdCtr_param +0x20 (used by the defaults).
void formatValue(Fmt f, int width, int value, std::string& s);
// Popup menu item text (ClcdCtr_param +0x40).
void formatPopupText(PopText f, int value, std::string& s);

}  // namespace sq8l::gui
