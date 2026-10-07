// SQ8L port: DPF UI hosting the editor.
//
// The editor (sq8l::gui::EditorView + EditorController, ported from the original) is
// software rendered into a 626x430 RGB frame — exactly the original's pixels — shown as
// one OpenGL texture with nearest-neighbour scaling. Mouse input is translated to the
// Win32-style events the ported controls expect; a 20 ms tick stands in for the
// original's CsimpleTimer. Native menus, dialogs and file pickers come from PlatformUi.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <vector>

#include "DistrhoUI.hpp"
#include "EditorView.h"
#include "SQ8LPlugin.hpp"
#include "logic/EditorController.h"
#include "logic/EditorHost.h"
#include "text/StbTextRenderer.h"

#if defined(__APPLE__)
#include <OpenGL/gl.h>
#include "mac/PlatformUiMac.h"
using PlatformImpl = sq8l::gui::PlatformUiMac;
#elif defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <GL/gl.h>
#include "win/PlatformUiWin.h"
using PlatformImpl = sq8l::gui::PlatformUiWin;
#else
#error "no PlatformUi implementation for this platform yet"
#endif
#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif

START_NAMESPACE_DISTRHO

using sq8l::gui::EditorView;
using sq8l::gui::MouseButton;

namespace {

int gEditorsOpened = 0;  // DAT_004c3070: the welcome text only shows in the first editor

// The engine side of the editor (the original reaches it through the form's master and
// edit buffer pointers, the global sound library and CplugConfig).
class PluginEditorHost final : public sq8l::gui::EditorHost {
public:
    explicit PluginEditorHost(SQ8LPlugin& p) : p_(p) {}
    void setController(sq8l::gui::EditorController* c) { controller_ = c; }

    sq8l::EditBuffer& editBuffer() override { return p_.synth().editBuffer(); }
    sq8l::SoundLibrary& library() override { return p_.synth().library(); }
    const sq8l::Settings& settings() override { return p_.settings(); }

    void setGuiSetting(int index, bool value) override {
        if (index < 0 || index >= sq8l::Settings::kNumGui) return;
        const int v = value ? 1 : 0;
        if (p_.settings().gui[index] == v) return;
        p_.settings().gui[index] = v;
        sq8l::SharedLibrary::saveSettings();
        if (controller_) controller_->post(sq8l::gui::kMsgNotify, 0, index);
    }

    void setSynthSetting(int index, int value) override {
        if (index < 0 || index >= sq8l::Settings::kNumSynth) return;
        if (p_.settings().synth[index] == value) return;
        p_.settings().synth[index] = value;
        p_.synth().master().loadOverrides(p_.settings().synth);
        sq8l::SharedLibrary::saveSettings();
        if (controller_) controller_->post(sq8l::gui::kMsgNotify, 0, 0x10 + index);
    }

    void setPortSetting(int index, int value) override {
        if (!p_.settings().setPort(index, value)) return;
        if (index == 1) p_.synth().setPolyphony(p_.settings().polyphony());  // (engine lock held)
        sq8l::SharedLibrary::saveSettings();
        if (controller_) controller_->post(sq8l::gui::kMsgNotify, 0, 0x20 + index);
    }

    void panic() override { p_.synth().master().panic(); }
    int voicesUsed() override { return p_.synth().master().activeVoiceCount(); }
    int voicesMax() override { return p_.synth().polyphony(); }

    std::string pluginDirectory() override {
        std::string d = sq8l::userDataDir();
        if (!d.empty() && (d.back() == '/' || d.back() == '\\')) d.pop_back();
        return d;
    }

    // MIDI ports for SEND/REQ: not available yet in the port (the dialogs list no ports).
    std::vector<std::string> midiInPorts() override { return {}; }
    std::vector<std::string> midiOutPorts() override { return {}; }
    void midiCloseAll() override {}
    bool midiOpenOut(int) override { return false; }
    void midiSendOut(const std::vector<uint8_t>&) override {}
    bool midiOpenIn(int) override { return false; }

private:
    SQ8LPlugin& p_;
    sq8l::gui::EditorController* controller_ = nullptr;
};

}  // namespace

class SQ8LUI : public UI {
public:
    SQ8LUI()
        : UI(EditorView::kWidth, EditorView::kHeight),
          plugin_(*static_cast<SQ8LPlugin*>(getPluginInstancePointer())),
          host_(plugin_),
          view_(std::make_unique<EditorView>()),
          frame_(EditorView::kWidth, EditorView::kHeight) {
        const double scale = getScaleFactor();
        if (scale != 1.0) setSize(EditorView::kWidth * scale, EditorView::kHeight * scale);
        rgb_.resize(static_cast<size_t>(EditorView::kWidth) * EditorView::kHeight * 3);
        view_->setTextRenderer(&text_);

        PlatformImpl::Hooks hooks;
        hooks.releaseEngine = [this] { releaseEngine(); };
        hooks.acquireEngine = [this] { acquireEngine(); };
        hooks.pump = [this] {
            if (!controller_) return;
            deliverNotifications();
            controller_->pump();
        };
        hooks.nameFocus = [this](bool focused, const std::string& text) {
            Engine lock(*this);
            if (controller_) controller_->nameEditFocus(focused, text);
            repaint();
        };
        hooks.nameKey = [this](int key) {
            Engine lock(*this);
            if (controller_) controller_->nameEditKeyDown(key);
        };
        platform_ = std::make_unique<PlatformImpl>(reinterpret_cast<void*>(getWindow().getNativeWindowHandle()), hooks);
        Engine lock(*this);
        controller_ = std::make_unique<sq8l::gui::EditorController>(*view_, host_, *platform_, gEditorsOpened++ == 0);
        host_.setController(controller_.get());
        view_->onContextMenu = [this](sq8l::gui::Control&, int x, int y) { controller_->contextMenu(x, y); };
        controller_->show();
        // The LEDs size themselves on their first paint, which mouse hit-testing depends on.
        view_->render(frame_);
    }

    ~SQ8LUI() override {
        // Debugging aid: SQ8L_UI_DUMP=/path/frame.ppm saves the last editor frame.
        if (const char* dump = std::getenv("SQ8L_UI_DUMP")) {
            if (FILE* f = std::fopen(dump, "wb")) {
                std::fprintf(f, "P6\n%d %d\n255\n", EditorView::kWidth, EditorView::kHeight);
                std::fwrite(rgb_.data(), 1, rgb_.size(), f);
                std::fclose(f);
            }
        }
        {
            Engine lock(*this);
            host_.setController(nullptr);
            controller_.reset();
        }
        if (texture_) glDeleteTextures(1, &texture_);
    }

protected:
    void parameterChanged(uint32_t, float) override {}

    void uiIdle() override {
        const auto now = std::chrono::steady_clock::now();
        if (now - lastTick_ < std::chrono::milliseconds(20)) return;
        int ms = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(now - lastTick_).count());
        lastTick_ = now;
        {
            Engine lock(*this);
            deliverNotifications();
            controller_->idle(ms > 200 ? 200 : ms);
        }
        repaint();
    }

    void onDisplay() override {
        {
            Engine lock(*this);
            view_->render(frame_);
        }
        frame_.toRGB24(rgb_.data());
        if (!texture_) {
            glGenTextures(1, &texture_);
            glBindTexture(GL_TEXTURE_2D, texture_);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        }
        glBindTexture(GL_TEXTURE_2D, texture_);
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, EditorView::kWidth, EditorView::kHeight, 0, GL_RGB,
                     GL_UNSIGNED_BYTE, rgb_.data());
        const float w = static_cast<float>(getWidth()), h = static_cast<float>(getHeight());
        glEnable(GL_TEXTURE_2D);
        glColor4f(1, 1, 1, 1);
        glBegin(GL_QUADS);
        glTexCoord2f(0, 0); glVertex2f(0, 0);
        glTexCoord2f(1, 0); glVertex2f(w, 0);
        glTexCoord2f(1, 1); glVertex2f(w, h);
        glTexCoord2f(0, 1); glVertex2f(0, h);
        glEnd();
        glDisable(GL_TEXTURE_2D);
        glBindTexture(GL_TEXTURE_2D, 0);
    }

    bool onMouse(const MouseEvent& ev) override {
        int x, y;
        toForm(ev.pos.getX(), ev.pos.getY(), x, y);
        const MouseButton b = ev.button == kMouseButtonRight ? MouseButton::Right
                              : ev.button == kMouseButtonMiddle ? MouseButton::Middle
                                                                : MouseButton::Left;
        const uint32_t keys = keysOf(ev.mod);
        if (std::getenv("SQ8L_UI_DEBUG"))
            std::fprintf(stderr, "[sq8l-ui] mouse %s button %u at form %d,%d\n", ev.press ? "down" : "up", ev.button, x, y);
        {
            Engine lock(*this);
            if (ev.press) {
                buttons_ |= bit(b);
                // The program name box is a native edit control: a left click focuses it.
                const sq8l::gui::NameEdit& edit = view_->progNameEdit();
                const auto r = edit.bounds();
                const bool inEdit = x >= r.left && x < r.right && y >= r.top && y < r.bottom;
                if (b == MouseButton::Left && inEdit && platform_) {
                    platform_->beginNameEdit(r.left, r.top, r.width(), r.height(), edit.text);
                    repaint();
                    return true;
                }
                if (platform_ && platform_->nameEditing()) platform_->focusForm();
                // Windows double click: same button within 500 ms and a few pixels.
                const bool dbl = b == lastButton_ && ev.time - lastTime_ <= 500 && std::abs(x - lastX_) <= 2 &&
                                 std::abs(y - lastY_) <= 2;
                if (dbl) {
                    view_->mouseDoubleClick(b, x, y, keys | buttons_);
                    lastTime_ = 0;
                } else {
                    view_->mouseDown(b, x, y, keys | buttons_);
                    lastTime_ = ev.time;
                }
                lastButton_ = b;
                lastX_ = x;
                lastY_ = y;
            } else {
                buttons_ &= ~bit(b);
                view_->mouseUp(b, x, y, keys | buttons_);
            }
            deliverNotifications();
            controller_->pump();
        }
        repaint();
        return true;
    }

    bool onMotion(const MotionEvent& ev) override {
        int x, y;
        toForm(ev.pos.getX(), ev.pos.getY(), x, y);
        {
            Engine lock(*this);
            view_->mouseMove(x, y, keysOf(ev.mod) | buttons_);
            controller_->pump();
        }
        repaint();
        return true;
    }

private:
    // Post the master's queued notifications to the editor (engine lock held).
    void deliverNotifications() {
        for (const auto& m : plugin_.takeEditorMessages()) {
            if (std::getenv("SQ8L_UI_DEBUG")) std::fprintf(stderr, "[sq8l-ui] notify %#x %d\n", m.wParam, m.lParam);
            controller_->post(sq8l::gui::kMsgNotify, m.wParam, m.lParam);
        }
    }

    // Engine lock held while the editor logic runs; released around native modal loops.
    struct Engine {
        SQ8LUI& ui;
        explicit Engine(SQ8LUI& u) : ui(u) { ui.acquireEngine(); }
        ~Engine() { ui.releaseEngine(); }
    };
    void acquireEngine() { plugin_.engineMutex().lock(); ++depth_; }
    void releaseEngine() { --depth_; plugin_.engineMutex().unlock(); }

    void toForm(double px, double py, int& x, int& y) const {
        x = static_cast<int>(px * EditorView::kWidth / static_cast<double>(getWidth()));
        y = static_cast<int>(py * EditorView::kHeight / static_cast<double>(getHeight()));
    }

    static uint32_t bit(MouseButton b) {
        return b == MouseButton::Left ? sq8l::gui::MK_LBUTTON_
               : b == MouseButton::Right ? sq8l::gui::MK_RBUTTON_ : sq8l::gui::MK_MBUTTON_;
    }

    static uint32_t keysOf(uint mod) {
        uint32_t k = 0;
        if (mod & kModifierShift) k |= sq8l::gui::MK_SHIFT_;
        if (mod & kModifierControl) k |= sq8l::gui::MK_CONTROL_;
        return k;
    }

    SQ8LPlugin& plugin_;
    PluginEditorHost host_;
    sq8l::gui::StbTextRenderer text_{true};  // antialiased text
    std::unique_ptr<EditorView> view_;
    std::unique_ptr<PlatformImpl> platform_;
    std::unique_ptr<sq8l::gui::EditorController> controller_;
    sq8l::gui::Bitmap frame_;
    std::vector<uint8_t> rgb_;
    GLuint texture_ = 0;
    int depth_ = 0;
    uint32_t buttons_ = 0;
    MouseButton lastButton_ = MouseButton::Left;
    uint lastTime_ = 0;
    int lastX_ = 0, lastY_ = 0;
    std::chrono::steady_clock::time_point lastTick_ = std::chrono::steady_clock::now();

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(SQ8LUI)
};

UI* createUI() { return new SQ8LUI(); }

END_NAMESPACE_DISTRHO
