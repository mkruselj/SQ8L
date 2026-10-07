"""High-level driver for the original SQ8L editor running in the emulator.

    ed = Editor()                  # loads the plugin, opens the editor
    ed.click("lfo1Button")         # component names from the original form (re/extracted/forms)
    ed.drag("lcdKnob0", dy=-20)    # knob drag (pixels, negative = up)
    ed.screenshot("out/x.png")
    tree = ed.popup_of(lambda: ed.click("menuFileImage"))   # capture a popup menu
"""
import os
import re

import numpy as np
from PIL import Image

from vsthost import SQ8LHost, effSetProgram

HERE = os.path.dirname(os.path.abspath(__file__))
FORM_TXT = os.path.join(os.path.dirname(HERE), "re", "extracted", "forms", "TPLUGEDITFORM.txt")
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
WM_LBUTTONDBLCLK, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x203, 0x204, 0x205
MK_LBUTTON, MK_RBUTTON = 1, 2


def form_components():
    """name -> (class, left, top, width, height) for direct children of the editor form."""
    comps = {}
    cur = None
    for line in open(FORM_TXT):
        m = re.match(r"^  object (\w+): (\w+)", line)
        if m:
            cur = [m.group(1), m.group(2), None, None, None, None]
            comps[cur[0]] = cur
            continue
        m = re.match(r"^    (Left|Top|Width|Height) = (-?\d+)", line)
        if m and cur is not None:
            cur[2 + ["Left", "Top", "Width", "Height"].index(m.group(1))] = int(m.group(2))
    return {k: tuple(v[1:]) for k, v in comps.items()}


class Editor:
    def __init__(self, program=None, sample_rate=44100.0):
        self.host = SQ8LHost(sample_rate=sample_rate, gui=True)
        self.host.load()
        self.host.start()
        if program is not None:
            self.host.dispatch(effSetProgram, value=program)
        self.parent, _ = self.host.open_editor()
        self.g = self.host.gui
        self.emu = self.host.emu
        self.form = next(h for h, w in self.g.windows.items() if w.cls == "tplugeditform")
        self.comps = form_components()
        self.idle(300)

    # ------------------------------------------------------------ lookup
    def window_at(self, x, y):
        """Deepest visible child window of the form containing form point (x, y)."""
        best = self.form
        f = self.g.windows[self.form]
        for ch in reversed(f.children):
            w = self.g.windows.get(ch)
            if w and w.visible and w.x <= x < w.x + w.w and w.y <= y < w.y + w.h:
                return ch
        return best

    def center(self, name):
        cls, l, t, w, h = self.comps[name]
        return l + w // 2, t + h // 2

    # ------------------------------------------------------------ input
    def _send(self, hwnd, msg, wp, x, y):
        w = self.g.windows[hwnd]
        lx, ly = x - (w.x if hwnd != self.form else 0), y - (w.y if hwnd != self.form else 0)
        sx, sy = self.g.screen_pos(w)
        self.g.cursor = (sx + lx, sy + ly)
        target = self.g.capture if self.g.capture in self.g.windows else hwnd
        tw = self.g.windows[target]
        if target != hwnd:
            tx, ty = self.g.screen_pos(tw)
            lx, ly = sx + lx - tx, sy + ly - ty
        self.emu.call(tw.wndproc, target, msg, wp, ((ly & 0xFFFF) << 16) | (lx & 0xFFFF))

    def mouse(self, msg, x, y, wp=0):
        self._send(self.window_at(x, y), msg, wp, x, y)

    def click_at(self, x, y, double=False, right=False):
        self.mouse(WM_MOUSEMOVE, x, y)
        if right:
            self.mouse(WM_RBUTTONDOWN, x, y, MK_RBUTTON)
            self.mouse(WM_RBUTTONUP, x, y)
        else:
            self.mouse(WM_LBUTTONDOWN, x, y, MK_LBUTTON)
            self.mouse(WM_LBUTTONUP, x, y)
            if double:
                self.mouse(WM_LBUTTONDBLCLK, x, y, MK_LBUTTON)
                self.mouse(WM_LBUTTONUP, x, y)
        self.idle(100)

    def click(self, name, **kw):
        self.click_at(*self.center(name), **kw)

    def drag(self, name, dy=0, dx=0, steps=8):
        x, y = self.center(name)
        self.mouse(WM_MOUSEMOVE, x, y)
        self.mouse(WM_LBUTTONDOWN, x, y, MK_LBUTTON)
        for i in range(1, steps + 1):
            self._send(self.g.capture or self.window_at(x, y), WM_MOUSEMOVE, MK_LBUTTON,
                       x + dx * i // steps, y + dy * i // steps)
            self.idle(20)
        self._send(self.g.capture or self.window_at(x, y), WM_LBUTTONUP, 0, x + dx, y + dy)
        self.idle(100)

    def popup_of(self, action, choose=None):
        """Run action (which opens a popup menu); return the captured menu tree.
        choose: callable(tree) -> command id to select, or None to dismiss."""
        n = len(self.g.popups)
        self.g.menu_choice = choose
        action()
        self.idle(100)
        return self.g.popups[n][0] if len(self.g.popups) > n else None

    # ------------------------------------------------------------ time & output
    def idle(self, ms=100):
        self.g.idle(ms)

    def frame(self):
        self.g.paint_all(self.form)
        return self.g.screenshot(self.form)

    def screenshot(self, path):
        img = self.frame()
        Image.fromarray(img).save(path)
        return img

    def lcd_text(self):
        """Text currently shown by the main VFD (read from the TLCD3 object is TODO)."""
        raise NotImplementedError


def find_command(tree, text):
    for it in tree:
        if it["text"].replace("&", "") == text:
            return it["id"]
        if "sub" in it:
            r = find_command(it["sub"], text)
            if r:
                return r
    return None
