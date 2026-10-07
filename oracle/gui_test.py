import sys, numpy as np
from vsthost import *
h=SQ8LHost(gui=True)
h.load(); h.start()
host, r = h.open_editor()
print("editOpen ->", r)
g=h.gui
print("windows:", len(g.windows))
for hw,w in list(g.windows.items())[:60]: print(hex(hw), w.cls, w.x, w.y, w.w, w.h, "vis" if w.visible else "-", "parent", hex(w.parent), w.text[:20])
from PIL import Image
g.paint_all(0x30000)
img=g.screenshot(0x30000)
Image.fromarray(img).save("out/editor_v1.png")
print("saved", img.shape)
