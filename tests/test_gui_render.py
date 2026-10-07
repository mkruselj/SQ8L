"""Pixel comparison: original editor (emulated, oracle/gui_driver) vs C++ EditorView.

For each scenario the ORIGINAL editor is driven into a state (programs and banks, every page
button, scrolled sub-pages, knob drags, hover frames, pressed buttons, MONO/SYNC/AM LEDs,
voices counter while notes play, LCD clicks...). Then:
  1. the oracle renders the window (all WM_PAINTs, composed like Windows),
  2. the internal state of every original control is read from emulator memory
     (tests/test_gui_state.py),
  3. that state is loaded into a fresh C++ EditorView (tests/capi_gui.cpp), which renders,
  4. the two 626x430 frames must be identical everywhere except the text drawn with Windows
     fonts (status labels, program name edit box), which is masked and reported.

Usage: SQ8L_TESTAPI=build/libsq8l_testapi.dylib python tests/test_gui_render.py [--save DIR]
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gui_state import CppGui, OriginalGui, text_masks  # noqa: E402

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x200, 0x201, 0x202, 0x204, 0x205
MK_LBUTTON, MK_SHIFT = 1, 4

PAGE_BUTTONS = ("buttWav", "buttOsc1", "buttOsc2", "buttOsc3", "buttDca1", "buttDca2", "buttDca3", "buttFilt",
                "buttDca4", "buttModes", "buttLfo1", "buttLfo2", "buttLfo3", "buttLfo4", "buttEnv1", "buttEnv2",
                "buttEnv3", "buttEnv4", "buttMat1", "buttMat2", "buttMat3")

SAVE = None
if "--save" in sys.argv:
    SAVE = sys.argv[sys.argv.index("--save") + 1]
    os.makedirs(SAVE, exist_ok=True)


class Stats:
    def __init__(self):
        self.rows = []
        self.frames = set()     # coverage: distinct images / LCD contents / knob frames / button looks
        self.lcds = set()
        self.knob_frames = set()
        self.button_looks = set()

    def add(self, name, img, st):
        cg = CppGui()
        cg.load_state(st)
        out = cg.render()
        mask = np.zeros(img.shape[:2], bool)
        for (_, x0, y0, x1, y1) in text_masks(st):
            mask[y0:y1, x0:x1] = True
        self.frames.add(hash(img[~mask].tobytes()))
        self.lcds.add(tuple("".join(chr(c[0]) for c in row) for row in st["lcd"]["lcd"]["cells"]))
        for n in st["knobs"]:
            self.knob_frames.add(cg.L.sq8l_gui_knob_frame(cg.c(n)))
        for n in st["buttons"]:
            b = cg.button(n)
            self.button_looks.add((n, b["frameIndex"], b["hover"]))
        d = (img != out).any(-1)
        outside = int((d & ~mask).sum())
        inside = int((d & mask).sum())
        self.rows.append((name, outside, inside, int(mask.sum()), int((~mask).sum())))
        status = "OK " if outside == 0 else "FAIL"
        print(f"  {status} {name:42s} differing px outside masks: {outside:6d}   text px differing in masks: "
              f"{inside:4d}/{int(mask.sum())}")
        if SAVE and outside:
            from PIL import Image
            Image.fromarray(img).save(os.path.join(SAVE, name + "_orig.png"))
            Image.fromarray(out).save(os.path.join(SAVE, name + "_cpp.png"))
            dd = np.zeros_like(img)
            dd[d & ~mask] = 255
            Image.fromarray(dd).save(os.path.join(SAVE, name + "_diff.png"))
        return outside


def snap(stats, og, name):
    img = og.frame()
    return stats.add(name, img, og.state())


def press_and_hold(og, name):
    """Mouse down on a control and leave the button pressed (no mouse up)."""
    x, y = og.ed.center(name)
    og.ed.mouse(WM_MOUSEMOVE, x, y)
    og.ed.mouse(WM_LBUTTONDOWN, x, y, MK_LBUTTON)
    og.ed.idle(40)


def release(og, name):
    x, y = og.ed.center(name)
    og.ed._send(og.g.capture or og.ed.window_at(x, y), WM_LBUTTONUP, 0, x, y)
    og.ed.idle(100)


def hover(og, x, y):
    og.ed.mouse(WM_MOUSEMOVE, x, y)
    og.ed.idle(40)


def drag(og, name, dy, dx=0, keys=0, steps=8):
    ed = og.ed
    x, y = ed.center(name) if isinstance(name, str) else name
    ed.mouse(WM_MOUSEMOVE, x, y, keys)
    ed.mouse(WM_LBUTTONDOWN, x, y, MK_LBUTTON | keys)
    for i in range(1, steps + 1):
        ed._send(og.g.capture or ed.window_at(x, y), WM_MOUSEMOVE, MK_LBUTTON | keys, x + dx * i // steps,
                 y + dy * i // steps)
        ed.idle(20)
    ed._send(og.g.capture or ed.window_at(x, y), WM_LBUTTONUP, keys, x + dx, y + dy)
    ed.idle(100)


def lcd_point(og, col, row):
    s = og.lcd_state("lcd")
    x0, y0 = s["bounds"][:2]
    return (x0 + s["frameW"] + col * (s["charW"] + s["gapX"]) + 5,
            y0 + s["frameH"] + row * (s["charH"] + s["gapY"]) + 9)


def play_notes(og, notes, blocks=40):
    h = og.ed.host
    h.send_midi([(0, bytes([0x90, n, 100])) for n in notes])
    for _ in range(blocks):
        h.process()
    og.ed.idle(200)


def main():
    t0 = time.time()
    stats = Stats()

    print("[1] start-up, program 0")
    og = OriginalGui()
    snap(stats, og, "start")
    og.ed.idle(1500)                   # the welcome message gives way to the page display
    snap(stats, og, "start_idle")

    print("[2] every page button (+ sub-pages via the scroll arrows)")
    for b in PAGE_BUTTONS:
        og.ed.click(b)
        snap(stats, og, f"page_{b}")
        og.ed.click("pscrDownButton")
        snap(stats, og, f"page_{b}_scroll1")
        og.ed.click("pscrDownButton")
        snap(stats, og, f"page_{b}_scroll2")
        og.ed.click("pscrUpButton")
        snap(stats, og, f"page_{b}_scrollup")

    print("[3] knob drags (vertical, with horizontal distance, with Shift, both rows)")
    for page in ("buttFilt", "buttLfo1", "buttEnv1", "buttWav", "buttMat1"):
        og.ed.click(page)
        drag(og, "lcdKnob0", dy=-30)
        snap(stats, og, f"drag_{page}_k0_up30")
        drag(og, "lcdKnob1", dy=25, dx=60)
        snap(stats, og, f"drag_{page}_k1_down25_dx60")
        drag(og, "lcdKnob3", dy=-40, keys=MK_SHIFT)
        snap(stats, og, f"drag_{page}_k3_shift")
        drag(og, "lcdKnob7", dy=-200)
        snap(stats, og, f"drag_{page}_k7_up200")
        drag(og, "lcdKnob9", dy=150)
        snap(stats, og, f"drag_{page}_k9_down150")
        drag(og, "lcdKnob5", dy=-12, dx=-30)
        snap(stats, og, f"drag_{page}_k5_small")

    print("[4] hover frames, hints, pressed buttons")
    og.ed.click("buttLfo2")
    hover(og, *og.ed.center("pscrDownButton"))
    snap(stats, og, "hover_pscrDown")
    hover(og, *og.ed.center("pscrUpButton"))
    snap(stats, og, "hover_pscrUp")
    hover(og, *og.ed.center("buttEnv3"))
    snap(stats, og, "hover_buttEnv3_hint")
    hover(og, *og.ed.center("menuFileImage"))
    snap(stats, og, "hover_menuFile_hint")
    for b in ("buttEnv2", "upButton", "buttSync", "writeButton", "pscrDownButton", "BankButton"):
        press_and_hold(og, b)
        snap(stats, og, f"pressed_{b}")
        hover(og, 5, 300)              # leave while pressed: the press is cancelled
        snap(stats, og, f"pressed_{b}_left")
        release(og, b)

    print("[5] MONO / SYNC / AM LEDs")
    for b in ("buttMono", "buttSync", "buttAm"):
        og.ed.click(b)
        snap(stats, og, f"led_{b}_on")
    for b in ("buttSync", "buttMono", "buttAm"):
        og.ed.click(b)
        snap(stats, og, f"led_{b}_off")

    print("[6] programs and banks")
    for i in range(3):
        og.ed.click("upButton")
        snap(stats, og, f"prog_up{i + 1}")
    og.ed.click("downButton")
    snap(stats, og, "prog_down")
    og.ed.click("BankButton")
    snap(stats, og, "bank_switch")
    og.ed.click("upButton")
    snap(stats, og, "bank_switch_up")
    og.ed.click("BankButton")
    snap(stats, og, "bank_switch_back")

    print("[7] LCD mouse: clicks on parameters, drag on the display, double click")
    og.ed.click("buttFilt")
    for (c, r) in ((9, 0), (20, 1), (30, 0), (40, 1)):
        og.ed.click_at(*lcd_point(og, c, r))
        snap(stats, og, f"lcd_click_{c}_{r}")
    drag(og, lcd_point(og, 9, 0), dy=-20)
    snap(stats, og, "lcd_drag_9_0")
    og.ed.click_at(*lcd_point(og, 9, 1), double=True)
    snap(stats, og, "lcd_dblclick_9_1")
    og.ed.click_at(*lcd_point(og, 20, 0), right=True)
    snap(stats, og, "lcd_rclick_20_0")

    print("[8] voices counter while notes play")
    og.ed.click("buttWav")
    play_notes(og, [60])
    snap(stats, og, "voices_1")
    play_notes(og, [64, 67, 72])
    snap(stats, og, "voices_4")

    print("[9] other programs and banks at start-up")
    for prog in (45, 128 + 7, 256 + 45, 383):
        og2 = OriginalGui(program=prog)
        snap(stats, og2, f"open_program_{prog}")
        og2.ed.idle(1500)
        og2.ed.click("buttOsc2")
        snap(stats, og2, f"open_program_{prog}_osc2")

    n = len(stats.rows)
    bad = [r for r in stats.rows if r[1]]
    masked = stats.rows[0][3]
    compared = sum(r[4] for r in stats.rows)
    print()
    print(f"{n} states compared, {compared} pixels outside text masks: "
          f"{sum(r[1] for r in stats.rows)} differing; {len(bad)} failing states.")
    print(f"Masked text regions (Windows fonts): StatusLabel1, StatusLabel2, progNameEdit "
          f"({masked} px at start-up); differing text pixels inside masks: "
          f"min {min(r[2] for r in stats.rows)}, max {max(r[2] for r in stats.rows)}")
    print(f"Coverage: {len(stats.frames)} distinct frames, {len(stats.lcds)} distinct LCD contents, "
          f"{len(stats.knob_frames)} distinct knob sprite frames, {len(stats.button_looks)} button (frame, hover) "
          f"combinations")
    print(f"time {time.time() - t0:.0f}s")
    if bad:
        print("FAIL:", [r[0] for r in bad])
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
