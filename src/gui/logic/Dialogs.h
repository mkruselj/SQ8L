// Logic of the editor's dialogs and info windows:
//   uSelSingleForm (TSelSingleForm, 0x47dabc..0x47e32f): WRITE dialog (program list, COMPARE
//                  check box, Bank button); also "Select destination/source..." of the SysEx
//                  bank import/export;
//   uMidiSelForm   (TmidiSelForm, 0x47d40c..0x47dabb): MIDI port selection for SEND/REQ;
//   uModInfoForm   (TModInfoForm, 0x47c80c..0x47d0ff): modulation source usage text;
//   uAboutForm     (TAboutForm): the About text (DFM memo lines).
// uMessageBox (TMessageForm, a non-modal "Ok" box) is never shown by the SQ8L editor; the
// message boxes it uses are Application.MessageBox (PlatformUi::messageBox).
//
// A modal dialog is driven by the platform: runModal() shows it with the state below and
// calls the event methods for the user's actions until modalResult != 0.
#pragma once

#include <string>
#include <vector>

#include "PlatformUi.h"

namespace sq8l {
struct Program;
}

namespace sq8l::gui {

class EditorHost;

// VCL modal results
enum : int { kMrNone = 0, kMrOk = 1, kMrCancel = 2 };

// The dialogs are created centred on this form point (0x47dd28 / 0x47c9c0 / 0x47d260:
// Left := ClientToScreen(315, 214).X - Width div 2, Top likewise), with the form's colour/font.
constexpr int kDialogCenterX = 0x13b, kDialogCenterY = 0xd6;

class ModalDialog {
public:
    enum class Kind { SelectProgram, SelectMidiPort };
    virtual ~ModalDialog() = default;
    virtual Kind kind() const = 0;

    std::string caption;        // TForm.Caption
    std::string okCaption = "Ok";
    std::string cancelCaption = "Cancel";
    std::vector<std::string> items;  // the list box
    int itemIndex = -1;         // TListBox.ItemIndex
    int modalResult = kMrNone;  // TForm.ModalResult

    // TCustomForm.ShowModal: OnShow, the modal loop (platform), OnClose.
    int showModal(PlatformUi& ui);

    // user actions common to both dialogs
    virtual void clickItem(int index);      // list box click (selection)
    virtual void dblClickItem(int index);   // OnDblClick = Ok
    virtual void keyDown(int key);          // list box OnKeyDown: VK_RETURN = Ok, VK_ESCAPE = cancel
    virtual void clickOk() = 0;
    void clickCancel() { modalResult = kMrCancel; }  // cancelButton.ModalResult = mrCancel

protected:
    virtual void onShow() {}
    virtual void onClose() {}
};

// TSelSingleForm (func 0x47dd28 creates and runs it).
class SelSingleDialog : public ModalDialog {
public:
    SelSingleDialog(EditorHost& host, const std::string& caption, const std::string& okCaption,
                    const std::string& cancelCaption, int bank, int prog);
    Kind kind() const override { return Kind::SelectProgram; }

    int bank;                   // +0x2ec
    int prog;                   // +0x2f0
    bool compareChecked = false;// compCheck.Checked

    void clickItem(int index) override;   // SingleListClick
    void clickOk() override;              // okButtonClick
    void clickCompare();                  // toggles compCheck (compCheckClick)
    void clickBank();                     // BankButtonClick

private:
    void onShow() override;               // FormShow -> FUN_0047e010
    void onClose() override;              // FormClose
    void fill();                          // FUN_0047e010
    void compareOn();                     // FUN_0047e1e0
    void compareOff();                    // FUN_0047e20c
    EditorHost& host_;
    bool first_ = true;                   // +0x2e4
    bool attached_ = false;               // +0x2f4 (edit buffer set after the check box)
};

// TmidiSelForm (func FUN_0047d5f4): returns the chosen port or -1.
class MidiSelDialog : public ModalDialog {
public:
    MidiSelDialog(const std::string& caption, bool output, int current, std::vector<std::string> ports);
    Kind kind() const override { return Kind::SelectMidiPort; }
    bool output;                // +0x2e8
    int current;                // +0x2e4
    void clickOk() override;    // ModalResult := ItemIndex + 9
    int result() const { return modalResult >= 9 ? modalResult - 9 : -1; }

private:
    void onShow() override;
    std::vector<std::string> ports_;
    bool first_ = true;
};

// TModInfoForm.FormShow (FUN_0047cb80): the lines of the modulation usage list.
std::vector<std::string> modulationUsage(const Program& p);
// TAboutForm: the lines of its memo (DFM).
std::string aboutText();

}  // namespace sq8l::gui
