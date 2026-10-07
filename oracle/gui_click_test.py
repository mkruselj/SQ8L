import sys
from PIL import Image
from vsthost import *
h=SQ8LHost(gui=True); h.load(); h.start()
host,_=h.open_editor(); g=h.gui
def click(hwnd, x=5, y=5):
    w=g.windows[hwnd]; lp=(y<<16)|x
    for msg,wp in ((0x200,0),(0x201,1),(0x202,0)):
        h.emu.call(w.wndproc, hwnd, msg, wp, lp)
def shot(name, ms=200):
    g.idle(ms); g.paint_all(0x30000); Image.fromarray(g.screenshot(0x30000)).save(f"out/{name}.png")
g.fire_timers(1); print("posted after 1 tick:", g.posted[:5])
shot("editor_start", 3000)
click(0x30060); shot("editor_osc1")
click(0x30090); shot("editor_lfo1")
click(0x30080); shot("editor_modes")
