"""Headless USER32/GDI32 subset so the original SQ8L editor (Delphi VCL) can run inside
the emulator and be rendered to an image.

Windows have client-area surfaces (numpy RGB), GDI draws into them, and
`GuiState.screenshot(hwnd)` composes a window with its children. Guest window
procedures are called through `emu.chain_calls` (from API handlers) or directly with
`emu.call` (from the host).
"""
import struct
import sys

import numpy as np

from w32emu import EmuStop
from winapi import _REGISTRY, api

FONT_DIR = "/System/Library/Fonts/Supplemental/"
FONT_FILES = {
    ("ms sans serif", False, False): "Microsoft Sans Serif.ttf",
    ("microsoft sans serif", False, False): "Microsoft Sans Serif.ttf",
    ("arial", False, False): "Arial.ttf",
    ("arial", True, False): "Arial Bold.ttf",
    ("arial", False, True): "Arial Italic.ttf",
    ("arial", True, True): "Arial Bold Italic.ttf",
    ("tahoma", False, False): "Tahoma.ttf",
    ("tahoma", True, False): "Tahoma Bold.ttf",
    ("courier new", False, False): "Courier New.ttf",
    ("verdana", False, False): "Verdana.ttf",
}

WM_CREATE, WM_SIZE, WM_MOVE, WM_PAINT, WM_ERASEBKGND = 0x01, 0x05, 0x03, 0x0F, 0x14
WM_NCCREATE, WM_NCCALCSIZE, WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x81, 0x83, 0x0C, 0x0D, 0x0E
WM_SHOWWINDOW, WM_WINDOWPOSCHANGED, WM_NCHITTEST, WM_TIMER = 0x18, 0x47, 0x84, 0x113
GWL_WNDPROC, GWL_HWNDPARENT, GWL_ID, GWL_STYLE, GWL_EXSTYLE, GWL_USERDATA = -4, -8, -12, -16, -20, -21
WS_CHILD, WS_VISIBLE = 0x40000000, 0x10000000


def s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def colorref(c):
    c &= 0xFFFFFF if c & 0xFF000000 in (0x02000000, 0x01000000) else 0xFFFFFFFF
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


class Surface:
    """RGB pixels. Mono bitmaps store 0/255 per channel and have mono=True."""

    def __init__(self, w, h, mono=False):
        self.w, self.h, self.mono = max(w, 0), max(h, 0), mono
        self.px = np.zeros((self.h, self.w, 3), np.uint8)

    def load(self):
        return self.px

    def store(self, px):
        self.px = px


class DibSection(Surface):
    """Pixels live in guest memory (the guest may access them directly)."""

    def __init__(self, emu, w, h, bpp, bits, palette, topdown):
        self.emu, self.w, self.h, self.bpp, self.bits = emu, w, h, bpp, bits
        self.palette = palette  # list of (r,g,b)
        self.topdown = topdown
        self.mono = bpp == 1
        self.stride = ((w * bpp + 31) // 32) * 4

    def load(self):
        raw = np.frombuffer(self.emu.read(self.bits, self.stride * self.h), np.uint8).reshape(self.h, self.stride)
        if not self.topdown:
            raw = raw[::-1]
        w = self.w
        if self.bpp == 32:
            px = raw[:, :w * 4].reshape(self.h, w, 4)[:, :, 2::-1]
        elif self.bpp == 24:
            px = raw[:, :w * 3].reshape(self.h, w, 3)[:, :, ::-1]
        elif self.bpp == 16:
            v = raw[:, :w * 2].copy().view("<u2").reshape(self.h, w).astype(np.uint32)
            px = np.stack([((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31], -1)
        elif self.bpp in (8, 4, 1):
            if self.bpp == 8:
                idx = raw[:, :w]
            elif self.bpp == 4:
                idx = np.stack([raw >> 4, raw & 15], -1).reshape(self.h, -1)[:, :w]
            else:
                idx = np.unpackbits(raw, axis=1)[:, :w]
            pal = np.array(self.palette + [(0, 0, 0)] * (256 - len(self.palette)), np.uint8)
            px = pal[idx]
        else:
            raise EmuStop(f"DIB bpp {self.bpp} unsupported")
        return np.ascontiguousarray(px, np.uint8)

    def store(self, px):
        h, w = self.h, self.w
        if self.bpp == 32:
            out = np.zeros((h, w, 4), np.uint8)
            out[:, :, :3] = px[:, :, ::-1]
            raw = out.reshape(h, w * 4)
        elif self.bpp == 24:
            raw = px[:, :, ::-1].reshape(h, w * 3)
        elif self.bpp == 16:
            v = ((px[:, :, 0].astype(np.uint16) >> 3) << 10) | ((px[:, :, 1].astype(np.uint16) >> 3) << 5) | (px[:, :, 2].astype(np.uint16) >> 3)
            raw = v.astype("<u2").view(np.uint8).reshape(h, w * 2)
        elif self.bpp in (8, 4, 1):
            pal = np.array(self.palette or [(0, 0, 0), (255, 255, 255)], np.int32)
            d = ((px[:, :, None, :].astype(np.int32) - pal[None, None, :, :]) ** 2).sum(-1)
            idx = d.argmin(-1).astype(np.uint8)
            if self.bpp == 8:
                raw = idx
            elif self.bpp == 4:
                if w % 2:
                    idx = np.pad(idx, ((0, 0), (0, 1)))
                raw = (idx[:, 0::2] << 4) | idx[:, 1::2]
            else:
                raw = np.packbits(idx & 1, axis=1)
        full = np.zeros((h, self.stride), np.uint8)
        full[:, :raw.shape[1]] = raw
        if not self.topdown:
            full = full[::-1]
        self.emu.write(self.bits, full.tobytes())


class Window:
    def __init__(self, hwnd, cls, wndproc, parent, x, y, w, h, style, exstyle, text):
        self.hwnd, self.cls, self.wndproc, self.parent = hwnd, cls, wndproc, parent
        self.x, self.y, self.w, self.h = x, y, w, h
        self.style, self.exstyle, self.text = style, exstyle, text
        self.visible = bool(style & WS_VISIBLE)
        self.surface = Surface(w, h)
        self.longs = {}
        self.props = {}
        self.dirty = True
        self.children = []
        self.id = 0

    def resize(self, w, h):
        if (w, h) != (self.w, self.h):
            old = self.surface.px
            self.w, self.h = w, h
            self.surface = Surface(w, h)
            hh, ww = min(h, old.shape[0]), min(w, old.shape[1])
            self.surface.px[:hh, :ww] = old[:hh, :ww]


class DC:
    def __init__(self, gui, target=None, window=None):
        self.gui = gui
        self.window = window
        self.bitmap = target          # handle of selected bitmap for memory DCs
        self.pen = gui.stock(7)       # BLACK_PEN
        self.brush = gui.stock(0)     # WHITE_BRUSH
        self.font = gui.stock(13)     # SYSTEM_FONT
        self.text_color = (0, 0, 0)
        self.bk_color = (255, 255, 255)
        self.bk_mode = 2
        self.rop2 = 13
        self.pos = (0, 0)
        self.worg = (0, 0)
        self.vorg = (0, 0)
        self.brush_org = (0, 0)
        self.clip = None              # numpy bool mask in device coords or None
        self.saved = []
        self.align = 0

    def surface(self):
        if self.window is not None:
            return self.window.surface
        if self.bitmap is not None:
            return self.gui.objs[self.bitmap]["surface"]
        return None

    def dev(self, x, y):
        return x - self.worg[0] + self.vorg[0], y - self.worg[1] + self.vorg[1]

    def state(self):
        return (self.bitmap, self.pen, self.brush, self.font, self.text_color, self.bk_color, self.bk_mode,
                self.rop2, self.pos, self.worg, self.vorg, None if self.clip is None else self.clip.copy(), self.align)

    def restore(self, st):
        (self.bitmap, self.pen, self.brush, self.font, self.text_color, self.bk_color, self.bk_mode,
         self.rop2, self.pos, self.worg, self.vorg, self.clip, self.align) = st


def rop3(rop, P, S, D):
    """Generic ternary raster op on uint8 arrays (bitwise per channel)."""
    code = (rop >> 16) & 0xFF
    out = np.zeros(D.shape, np.uint8)
    nP, nS, nD = ~P, ~S, ~D
    for k in range(8):
        if code >> k & 1:
            p = P if k & 4 else nP
            s = S if k & 2 else nS
            d = D if k & 1 else nD
            out |= p & s & d
    return out


class GuiState:
    def __init__(self, emu):
        self.emu = emu
        self.objs = {}            # handle -> dict(kind=...)
        self.windows = {}
        self.classes = {}         # lower name -> dict(wndproc, style, brush, menu)
        self.next_handle = 0x20000
        self.next_hwnd = 0x30000
        self.dcs = {}
        self.timers = {}
        self.mm_timers = {}
        self.menus = {}
        self.posted = []
        self.focus = 0
        self.capture = 0
        self.cursor = (0, 0)          # screen coordinates
        self.menu_choice = None       # callable(menu_tree) -> command id or None
        self.popups = []              # recorded (menu_tree, x, y)
        self.fonts = {}
        self.log = []
        for i in range(18):
            self.objs[0x5000 + i] = self.make_stock(i)
        # system classes VCL superclasses
        self.sys_proc = emu.make_callback("SysWndProc", 4, self._sys_wndproc)
        for c in ("edit", "button", "static", "listbox", "combobox", "scrollbar", "#32770"):
            self.classes[c] = dict(wndproc=self.sys_proc, style=0, brush=0, name=c.upper())

    # ---------------------------------------------------------------- objects
    def stock(self, i):
        return 0x5000 + i

    def make_stock(self, i):
        if i <= 5:
            colors = [(255, 255, 255), (192, 192, 192), (128, 128, 128), (64, 64, 64), (0, 0, 0), None]
            return dict(kind="brush", style=1 if i == 5 else 0, color=colors[i])
        if 6 <= i <= 8:
            return dict(kind="pen", style=5 if i == 8 else 0, width=1, color=(255, 255, 255) if i == 6 else (0, 0, 0))
        if i == 15:
            return dict(kind="palette")
        return dict(kind="font", height=-11, weight=400, italic=False, face="MS Sans Serif")

    def new(self, obj):
        h = self.next_handle
        self.next_handle += 4
        self.objs[h] = obj
        return h

    def new_surface_bitmap(self, w, h, mono=False):
        return self.new(dict(kind="bitmap", surface=Surface(w, h, mono), w=w, h=h, bpp=1 if mono else 32))

    def pil_font(self, fh):
        f = self.objs.get(fh) or self.make_stock(13)
        key = (f["face"].lower(), f["weight"] >= 600, f["italic"], f["height"])
        if key not in self.fonts:
            from PIL import ImageFont
            name = FONT_FILES.get(key[:3]) or FONT_FILES.get((key[0], False, False)) or "Microsoft Sans Serif.ttf"
            h = f["height"]
            size = -h if h < 0 else max(int(round(h * 0.82)), 1) if h > 0 else 11
            font = ImageFont.truetype(FONT_DIR + name, size)
            self.fonts[key] = font
        return self.fonts[key]

    # ---------------------------------------------------------------- windows
    def win(self, hwnd):
        return self.windows.get(hwnd)

    def screen_pos(self, w):
        x, y = 0, 0
        while w is not None:
            x += w.x
            y += w.y
            w = self.windows.get(w.parent) if w.style & WS_CHILD else None
        return x, y

    def screenshot(self, hwnd):
        """Compose a window and its visible children into one RGB image."""
        w = self.windows[hwnd]
        img = w.surface.px.copy()
        self._compose(w, img, 0, 0)
        return img

    def _compose(self, w, img, ox, oy):
        for ch in w.children:
            c = self.windows.get(ch)
            if c is None or not c.visible:
                continue
            x0, y0 = ox + c.x, oy + c.y
            sub = np.copy(c.surface.px)
            self._compose(c, sub, 0, 0)
            H, W = img.shape[:2]
            ys, xs = max(y0, 0), max(x0, 0)
            ye, xe = min(y0 + c.h, H), min(x0 + c.w, W)
            if ye > ys and xe > xs:
                img[ys:ye, xs:xe] = sub[ys - y0:ye - y0, xs - x0:xe - x0]

    def fire_timers(self, rounds=1):
        """Run every registered timer callback once per round (host-level calls)."""
        for _ in range(rounds):
            for (hwnd, tid), (ms, proc) in list(self.timers.items()):
                w = self.windows.get(hwnd)
                if proc:
                    self.emu.call(proc, hwnd, WM_TIMER, tid, 0)
                elif w:
                    self.emu.call(w.wndproc, hwnd, WM_TIMER, tid, 0)
            for tid, (delay, proc, user) in list(self.mm_timers.items()):
                self.emu.call(proc, tid, 0, user, 0, 0)

    def pump(self, limit=1000):
        """Dispatch posted messages (host-level calls)."""
        n = 0
        while self.posted and n < limit:
            hwnd, msg, wp, lp = self.posted.pop(0)
            w = self.windows.get(hwnd)
            if w:
                self.emu.call(w.wndproc, hwnd, msg, wp, lp)
            n += 1
        return n

    def idle(self, ms=100):
        """Advance GUI time: fire timers every 20 ms and pump messages."""
        for _ in range(max(ms // 20, 1)):
            self.fire_timers(1)
            self.pump()

    def paint_all(self, hwnd):
        """Send WM_ERASEBKGND/WM_PAINT to a window tree, top-down (host-level calls)."""
        w = self.windows[hwnd]
        if not w.visible:
            return
        dc = self.new(dict(kind="dc", dc=DC(self, window=w)))
        self.emu.call(w.wndproc, hwnd, WM_ERASEBKGND, dc, 0)
        self.objs.pop(dc, None)
        self.emu.call(w.wndproc, hwnd, WM_PAINT, 0, 0)
        if w.cls in ("tedit", "edit"):
            self.paint_edit(w)
        for ch in list(w.children):
            self.paint_all(ch)

    def paint_edit(self, w):
        """System EDIT control look: ask the parent for colors (WM_CTLCOLOREDIT), draw the text."""
        dch = self.new(dict(kind="dc", dc=DC(self, window=w)))
        dc = self.objs[dch]["dc"]
        parent = self.windows.get(w.parent)
        brush = 0
        if parent:
            brush, _ = self.emu.call(parent.wndproc, parent.hwnd, 0x133, dch, w.hwnd)
        bo = self.objs.get(brush, {})
        color = bo.get("color") or (255, 255, 255)
        w.surface.px[:, :] = color
        if getattr(w, "font", 0):
            dc.font = w.font
        dc.bk_mode = 1
        import gui as _g
        _g._text_draw(self.emu, dc, w.surface, 1, 0, w.text)
        self.objs.pop(dch, None)

    def _sys_wndproc(self, emu, hwnd, msg, wp, lp):
        return self.def_window_proc(hwnd, msg, wp, lp)

    def def_window_proc(self, hwnd, msg, wp, lp):
        w = self.win(hwnd)
        if msg == WM_NCCREATE:
            return 1
        if msg == WM_SETTEXT and w:
            w.text = self.emu.cstr(lp) or ""
            return 1
        if msg == WM_GETTEXT and w:
            return self.emu.write_cstr(lp, w.text, wp) if wp else 0
        if msg == WM_GETTEXTLENGTH and w:
            return len(w.text)
        if msg == 0x30 and w:      # WM_SETFONT
            w.font = wp
            return 0
        if msg == 0x31 and w:      # WM_GETFONT
            return getattr(w, "font", 0)
        if msg == WM_NCHITTEST:
            return 1
        if msg == WM_ERASEBKGND:
            return 1
        if msg == 0x21:  # WM_MOUSEACTIVATE
            return 1
        return 0


GUI = None


def gui(e):
    global GUI
    if GUI is None or GUI.emu is not e:
        GUI = GuiState(e)
    return GUI


# ================================================================ USER32: classes & windows
@api("GetClassInfoA", 3)
def GetClassInfoA(e, hinst, name, wc):
    g = gui(e)
    key = (e.cstr(name) if name >= 0x10000 else f"#{name}").lower()
    c = g.classes.get(key)
    if not c:
        return 0
    e.write(wc, struct.pack("<10I", c["style"], c["wndproc"], 0, 0, hinst, 0, 0, c["brush"], 0, name))
    return 1


@api("RegisterClassA", 1)
def RegisterClassA(e, wc):
    g = gui(e)
    style, proc, _, _, hinst, icon, cursor, brush, menu, name = struct.unpack("<10I", e.read(wc, 40))
    key = (e.cstr(name) if name >= 0x10000 else f"#{name}").lower()
    g.classes[key] = dict(wndproc=proc, style=style, brush=brush, name=key)
    return 0xC000 + len(g.classes)


@api("UnregisterClassA", 2)
def UnregisterClassA(e, name, hinst):
    return 1


@api("CreateWindowExA", 12)
def CreateWindowExA(e, exstyle, cls, title, style, x, y, w, h, parent, menu, inst, param):
    g = gui(e)
    key = (e.cstr(cls) if cls >= 0x10000 else f"#{cls}").lower()
    c = g.classes.get(key)
    if c is None:
        raise EmuStop(f"CreateWindowEx: unknown class {key}")
    x, y, w, h = s32(x), s32(y), s32(w), s32(h)
    if x == -0x80000000:
        x = y = 0
    if w == -0x80000000:
        w, h = 640, 480
    hwnd = g.next_hwnd
    g.next_hwnd += 4
    win = Window(hwnd, key, c["wndproc"], parent, x, y, w, h, style, exstyle, e.cstr(title) or "")
    if style & WS_CHILD:
        win.id = menu
    g.windows[hwnd] = win
    if parent in g.windows:
        g.windows[parent].children.append(hwnd)
    cs = e.scratch(48)
    e.write(cs, struct.pack("<12I", param, inst, menu, parent, h & 0xFFFFFFFF, w & 0xFFFFFFFF,
                            y & 0xFFFFFFFF, x & 0xFFFFFFFF, style, title, cls, exstyle))
    rc = e.scratch(16)
    e.write(rc, struct.pack("<4i", x, y, x + w, y + h))

    def calls(results):
        n = len(results)
        if n == 0:
            return (win.wndproc, [hwnd, WM_NCCREATE, 0, cs])
        if n == 1:
            if results[0] == 0:
                return None
            return (g.windows[hwnd].wndproc, [hwnd, WM_NCCALCSIZE, 0, rc])
        if n == 2:
            return (g.windows[hwnd].wndproc, [hwnd, WM_CREATE, 0, cs])
        if n == 3:
            return (g.windows[hwnd].wndproc, [hwnd, WM_SIZE, 0, (win.h << 16) | (win.w & 0xFFFF)])
        if n == 4:
            return (g.windows[hwnd].wndproc, [hwnd, WM_MOVE, 0, ((win.y & 0xFFFF) << 16) | (win.x & 0xFFFF)])
        return None

    def finish(results):
        if results and results[0] == 0 or len(results) > 2 and s32(results[2]) == -1:
            g.windows.pop(hwnd, None)
            return 0
        return hwnd

    g.log.append(("create", hwnd, key))
    return e.chain_calls(calls, finish, 12)


@api("DestroyWindow", 1)
def DestroyWindow(e, hwnd):
    g = gui(e)
    w = g.windows.pop(hwnd, None)
    if w and w.parent in g.windows and hwnd in g.windows[w.parent].children:
        g.windows[w.parent].children.remove(hwnd)
    return 1


@api("IsWindow", 1)
def IsWindow(e, hwnd):
    return int(hwnd in gui(e).windows)


@api("IsWindowVisible", 1)
def IsWindowVisible(e, hwnd):
    w = gui(e).win(hwnd)
    return int(bool(w and w.visible))


@api("IsWindowEnabled", 1)
def IsWindowEnabled(e, hwnd):
    return 1


@api("EnableWindow", 2)
def EnableWindow(e, hwnd, en):
    return 0


@api("ShowWindow", 2)
def ShowWindow(e, hwnd, cmd):
    w = gui(e).win(hwnd)
    if not w:
        return 0
    was = w.visible
    w.visible = cmd != 0
    return int(was)


@api("SetWindowPos", 7)
def SetWindowPos(e, hwnd, after, x, y, cx, cy, flags):
    g = gui(e)
    w = g.win(hwnd)
    if not w:
        return 0
    if not flags & 2:
        w.x, w.y = s32(x), s32(y)
    if not flags & 1:
        w.resize(s32(cx), s32(cy))
    if flags & 0x40:
        w.visible = True
    if flags & 0x80:
        w.visible = False
    if not flags & 4 and w.parent in g.windows:
        sib = g.windows[w.parent].children
        if hwnd in sib and after in (0, 1):
            sib.remove(hwnd)
            if after == 0:     # HWND_TOP -> painted last
                sib.append(hwnd)
            else:
                sib.insert(0, hwnd)
    # Notify the window like Windows does (VCL updates its bounds from these).
    wp = e.scratch(28)
    e.write(wp, struct.pack("<7I", hwnd, after, w.x & 0xFFFFFFFF, w.y & 0xFFFFFFFF, w.w, w.h, flags))
    return e.chain_calls([(w.wndproc, [hwnd, WM_WINDOWPOSCHANGED, 0, wp])], lambda r: 1, 7)


@api("MoveWindow", 6)
def MoveWindow(e, hwnd, x, y, cx, cy, repaint):
    w = gui(e).win(hwnd)
    if not w:
        return 0
    w.x, w.y = s32(x), s32(y)
    w.resize(s32(cx), s32(cy))
    return e.chain_calls([(w.wndproc, [hwnd, WM_SIZE, 0, (w.h << 16) | (w.w & 0xFFFF)]),
                          (w.wndproc, [hwnd, WM_MOVE, 0, ((w.y & 0xFFFF) << 16) | (w.x & 0xFFFF)])],
                         lambda r: 1, 6)


@api("SetWindowPlacement", 2)
def SetWindowPlacement(e, hwnd, p):
    return 1


@api("GetWindowPlacement", 2)
def GetWindowPlacement(e, hwnd, p):
    w = gui(e).win(hwnd)
    e.write(p + 4, struct.pack("<2I", 0, 1))
    if w:
        e.write(p + 28, struct.pack("<4i", w.x, w.y, w.x + w.w, w.y + w.h))
    return 1


@api("GetClientRect", 2)
def GetClientRect(e, hwnd, p):
    w = gui(e).win(hwnd)
    e.write(p, struct.pack("<4i", 0, 0, w.w if w else 1024, w.h if w else 768))
    return 1


@api("GetWindowRect", 2)
def GetWindowRect(e, hwnd, p):
    g = gui(e)
    w = g.win(hwnd)
    if not w:
        e.write(p, struct.pack("<4i", 0, 0, 1024, 768))
        return 1
    x, y = g.screen_pos(w)
    e.write(p, struct.pack("<4i", x, y, x + w.w, y + w.h))
    return 1


@api("AdjustWindowRectEx", 4)
def AdjustWindowRectEx(e, p, style, menu, ex):
    return 1


def _map_point(g, hwnd, x, y, to_screen):
    w = g.win(hwnd)
    if not w:
        return x, y
    sx, sy = g.screen_pos(w)
    return (x + sx, y + sy) if to_screen else (x - sx, y - sy)


@api("ClientToScreen", 2)
def ClientToScreen(e, hwnd, p):
    x, y = struct.unpack("<2i", e.read(p, 8))
    e.write(p, struct.pack("<2i", *_map_point(gui(e), hwnd, x, y, True)))
    return 1


@api("ScreenToClient", 2)
def ScreenToClient(e, hwnd, p):
    x, y = struct.unpack("<2i", e.read(p, 8))
    e.write(p, struct.pack("<2i", *_map_point(gui(e), hwnd, x, y, False)))
    return 1


@api("MapWindowPoints", 4)
def MapWindowPoints(e, hfrom, hto, p, n):
    g = gui(e)
    for i in range(n):
        x, y = struct.unpack("<2i", e.read(p + 8 * i, 8))
        x, y = _map_point(g, hfrom, x, y, True) if hfrom else (x, y)
        x, y = _map_point(g, hto, x, y, False) if hto else (x, y)
        e.write(p + 8 * i, struct.pack("<2i", x, y))
    return 0


@api("GetParent", 1)
def GetParent(e, hwnd):
    w = gui(e).win(hwnd)
    return w.parent if w else 0


@api("IsChild", 2)
def IsChild(e, parent, hwnd):
    g = gui(e)
    w = g.win(hwnd)
    while w is not None:
        if w.parent == parent:
            return 1
        w = g.win(w.parent)
    return 0


@api("GetWindow", 2)
def GetWindow(e, hwnd, cmd):
    g = gui(e)
    w = g.win(hwnd)
    if cmd == 5 and w and w.children:      # GW_CHILD
        return w.children[-1]
    return 0


@api("GetTopWindow", 1)
def GetTopWindow(e, hwnd):
    w = gui(e).win(hwnd)
    return w.children[-1] if w and w.children else 0


@api("GetDlgItem", 2)
def GetDlgItem(e, hwnd, i):
    return 0


@api("SetParent", 2)
def SetParent(e, hwnd, parent):
    g = gui(e)
    w = g.win(hwnd)
    if not w:
        return 0
    old = w.parent
    if old in g.windows and hwnd in g.windows[old].children:
        g.windows[old].children.remove(hwnd)
    w.parent = parent
    if parent in g.windows:
        g.windows[parent].children.append(hwnd)
    return old


@api("GetWindowLongA", 2)
def GetWindowLongA(e, hwnd, idx):
    w = gui(e).win(hwnd)
    if not w:
        return 0
    idx = s32(idx)
    return {GWL_WNDPROC: w.wndproc, GWL_STYLE: w.style, GWL_EXSTYLE: w.exstyle, GWL_ID: w.id,
            GWL_HWNDPARENT: w.parent}.get(idx, w.longs.get(idx, 0))


@api("SetWindowLongA", 3)
def SetWindowLongA(e, hwnd, idx, v):
    w = gui(e).win(hwnd)
    if not w:
        return 0
    idx = s32(idx)
    old = GetWindowLongA(e, hwnd, idx & 0xFFFFFFFF)
    if idx == GWL_WNDPROC:
        w.wndproc = v
    elif idx == GWL_STYLE:
        w.style = v
        w.visible = bool(v & WS_VISIBLE) or w.visible and False
    elif idx == GWL_EXSTYLE:
        w.exstyle = v
    elif idx == GWL_ID:
        w.id = v
    else:
        w.longs[idx] = v
    return old


@api("SetClassLongA", 3)
def SetClassLongA(e, hwnd, idx, v):
    return 0


@api("SetPropA", 3)
def SetPropA(e, hwnd, name, v):
    w = gui(e).win(hwnd)
    if w:
        w.props[name if name < 0x10000 else e.cstr(name)] = v
    return 1


@api("GetPropA", 2)
def GetPropA(e, hwnd, name):
    w = gui(e).win(hwnd)
    return w.props.get(name if name < 0x10000 else e.cstr(name), 0) if w else 0


@api("RemovePropA", 2)
def RemovePropA(e, hwnd, name):
    w = gui(e).win(hwnd)
    return w.props.pop(name if name < 0x10000 else e.cstr(name), 0) if w else 0


@api("SetWindowTextA", 2)
def SetWindowTextA(e, hwnd, p):
    w = gui(e).win(hwnd)
    if w:
        w.text = e.cstr(p) or ""
    return 1


@api("GetWindowTextA", 3)
def GetWindowTextA(e, hwnd, buf, n):
    w = gui(e).win(hwnd)
    return e.write_cstr(buf, w.text if w else "", n) if n else 0


@api("GetWindowThreadProcessId", 2)
def GetWindowThreadProcessId(e, hwnd, pid):
    if pid:
        e.w32(pid, 0x800)
    return 0x1000


@api("WindowFromPoint", 2)
def WindowFromPoint(e, x, y):
    return 0


@api("IsIconic", 1)
def IsIconic(e, h):
    return 0


@api("IsZoomed", 1)
def IsZoomed(e, h):
    return 0


@api("GetDesktopWindow", 0)
def GetDesktopWindow(e):
    return 0x10010


# ---------------------------------------------------------------- messages
@api("SendMessageA", 4)
def SendMessageA(e, hwnd, msg, wp, lp):
    w = gui(e).win(hwnd)
    if not w:
        return 0
    return e.chain_calls([(w.wndproc, [hwnd, msg, wp, lp])], lambda r: r[0], 4)


@api("CallWindowProcA", 5)
def CallWindowProcA(e, proc, hwnd, msg, wp, lp):
    return e.chain_calls([(proc, [hwnd, msg, wp, lp])], lambda r: r[0], 5)


@api("DefWindowProcA", 4)
def DefWindowProcA(e, hwnd, msg, wp, lp):
    g = gui(e)
    w = g.win(hwnd)
    if msg == WM_WINDOWPOSCHANGED and w:
        flags = e.u32(lp + 24)
        calls = []
        if not flags & 1:
            calls.append((w.wndproc, [hwnd, WM_SIZE, 0, (w.h << 16) | (w.w & 0xFFFF)]))
        if not flags & 2:
            calls.append((w.wndproc, [hwnd, WM_MOVE, 0, ((w.y & 0xFFFF) << 16) | (w.x & 0xFFFF)]))
        if calls:
            return e.chain_calls(calls, lambda r: 0, 4)
        return 0
    return g.def_window_proc(hwnd, msg, wp, lp)


@api("DefFrameProcA", 5)
def DefFrameProcA(e, hwnd, client, msg, wp, lp):
    return gui(e).def_window_proc(hwnd, msg, wp, lp)


@api("DefMDIChildProcA", 4)
def DefMDIChildProcA(e, hwnd, msg, wp, lp):
    return gui(e).def_window_proc(hwnd, msg, wp, lp)


@api("PostMessageA", 4)
def PostMessageA(e, hwnd, msg, wp, lp):
    gui(e).posted.append((hwnd, msg, wp, lp))
    return 1


@api("PostQuitMessage", 1)
def PostQuitMessage(e, code):
    return None


for _n, _k in (("TranslateMessage", 1), ("DispatchMessageA", 1), ("IsDialogMessageA", 2),
               ("TranslateMDISysAccel", 2), ("WaitMessage", 0), ("CallNextHookEx", 4)):
    api(_n, _k)(lambda e, *a: 0)


@api("InvalidateRect", 3)
def InvalidateRect(e, hwnd, rect, erase):
    w = gui(e).win(hwnd)
    if w:
        w.dirty = True
    return 1


@api("UpdateWindow", 1)
def UpdateWindow(e, hwnd):
    return 1


@api("SetTimer", 4)
def SetTimer(e, hwnd, tid, ms, proc):
    gui(e).timers[(hwnd, tid)] = (ms, proc)
    return tid or 1


@api("timeSetEvent", 5)
def timeSetEvent(e, delay, res, proc, user, flags):
    g = gui(e)
    tid = 0x100 + len(g.mm_timers)
    g.mm_timers[tid] = (delay, proc, user)
    return tid


@api("timeKillEvent", 1)
def timeKillEvent(e, tid):
    gui(e).mm_timers.pop(tid, None)
    return 0


@api("KillTimer", 2)
def KillTimer(e, hwnd, tid):
    gui(e).timers.pop((hwnd, tid), None)
    return 1


@api("SetFocus", 1)
def SetFocus(e, hwnd):
    g = gui(e)
    old, g.focus = g.focus, hwnd
    return old


@api("GetFocus", 0)
def GetFocus(e):
    return gui(e).focus


@api("SetCapture", 1)
def SetCapture(e, hwnd):
    g = gui(e)
    old, g.capture = g.capture, hwnd
    return old


@api("GetCapture", 0)
def GetCapture(e):
    return gui(e).capture


@api("ReleaseCapture", 0)
def ReleaseCapture(e):
    gui(e).capture = 0
    return 1


@api("SetActiveWindow", 1)
def SetActiveWindow(e, hwnd):
    return 0


@api("SetCursorPos", 2)
def SetCursorPos(e, x, y):
    gui(e).cursor = (s32(x), s32(y))
    return 1


@api("GetCursorPos", 1)
def GetCursorPos(e, p):
    e.write(p, struct.pack("<2i", *gui(e).cursor))
    return 1


@api("ShowCursor", 1)
def ShowCursor(e, show):
    return 0


@api("ShowOwnedPopups", 2)
def ShowOwnedPopups(e, hwnd, show):
    return 1


@api("ScrollWindow", 5)
def ScrollWindow(e, *a):
    return 1


for _n, _k in (("ShowScrollBar", 3), ("EnableScrollBar", 3), ("SetScrollRange", 5), ("SetScrollInfo", 4),
               ("SetScrollPos", 4), ("GetScrollPos", 2), ("GetScrollRange", 3), ("GetScrollInfo", 3)):
    api(_n, _k)(lambda e, *a: 0)


# ---------------------------------------------------------------- painting
def _new_dc(e, window=None):
    g = gui(e)
    dc = DC(g, window=window)
    h = g.new(dict(kind="dc", dc=dc))
    return h


@api("BeginPaint", 2)
def BeginPaint(e, hwnd, ps):
    g = gui(e)
    w = g.win(hwnd)
    h = _new_dc(e, w)
    e.write(ps, struct.pack("<II4i", h, 1, 0, 0, w.w if w else 0, w.h if w else 0) + bytes(40))
    if w:
        w.dirty = False
    return h


@api("EndPaint", 2)
def EndPaint(e, hwnd, ps):
    gui(e).objs.pop(e.u32(ps), None)
    return 1


@api("GetDC", 1)
def GetDC(e, hwnd):
    return _new_dc(e, gui(e).win(hwnd) if hwnd else None)


@api("GetDCEx", 3)
def GetDCEx(e, hwnd, rgn, flags):
    return GetDC(e, hwnd)


@api("GetWindowDC", 1)
def GetWindowDC(e, hwnd):
    return GetDC(e, hwnd)


@api("ReleaseDC", 2)
def ReleaseDC(e, hwnd, hdc):
    return 1


@api("CreateCompatibleDC", 1)
def CreateCompatibleDC(e, hdc):
    return _new_dc(e)


@api("DeleteDC", 1)
def DeleteDC(e, hdc):
    gui(e).objs.pop(hdc, None)
    return 1


def DCof(e, hdc):
    o = gui(e).objs.get(hdc)
    if not o or o["kind"] != "dc":
        return None
    return o["dc"]


@api("SaveDC", 1)
def SaveDC(e, hdc):
    dc = DCof(e, hdc)
    if not dc:
        return 0
    dc.saved.append(dc.state())
    return len(dc.saved)


@api("RestoreDC", 2)
def RestoreDC(e, hdc, n):
    dc = DCof(e, hdc)
    if not dc or not dc.saved:
        return 0
    n = s32(n)
    idx = n - 1 if n > 0 else len(dc.saved) + n
    st = dc.saved[idx]
    del dc.saved[idx:]
    dc.restore(st)
    return 1


@api("SelectObject", 2)
def SelectObject(e, hdc, h):
    g = gui(e)
    dc = DCof(e, hdc)
    o = g.objs.get(h)
    if not dc or not o:
        return 0
    k = o["kind"]
    if k == "bitmap":
        old, dc.bitmap = dc.bitmap or g.new_surface_bitmap(1, 1, True), h
    elif k == "pen":
        old, dc.pen = dc.pen, h
    elif k == "brush":
        old, dc.brush = dc.brush, h
    elif k == "font":
        old, dc.font = dc.font, h
    elif k == "region":
        return 2
    else:
        return 0
    return old


@api("DeleteObject", 1)
def DeleteObject(e, h):
    g = gui(e)
    if h in g.objs and not 0x5000 <= h < 0x5020:
        del g.objs[h]
    return 1


@api("GetStockObject", 1)
def GetStockObject(e, i):
    return 0x5000 + i


@api("UnrealizeObject", 1)
def UnrealizeObject(e, h):
    return 1


@api("GdiFlush", 0)
def GdiFlush(e):
    return 1


@api("CreatePenIndirect", 1)
def CreatePenIndirect(e, p):
    style, w, _, color = struct.unpack("<IiiI", e.read(p, 16))
    return gui(e).new(dict(kind="pen", style=style & 0xF, width=max(w, 1), color=colorref(color)))


@api("CreateSolidBrush", 1)
def CreateSolidBrush(e, color):
    return gui(e).new(dict(kind="brush", style=0, color=colorref(color)))


@api("CreateBrushIndirect", 1)
def CreateBrushIndirect(e, p):
    style, color, hatch = struct.unpack("<III", e.read(p, 12))
    g = gui(e)
    b = dict(kind="brush", style=style, color=colorref(color))
    if style == 3:   # BS_PATTERN
        b["pattern"] = hatch
    return g.new(b)


@api("CreateFontIndirectA", 1)
def CreateFontIndirectA(e, p):
    lf = e.read(p, 60)
    height, width, esc, orient, weight = struct.unpack_from("<5i", lf, 0)
    italic, underline, strike = lf[20], lf[21], lf[22]
    face = lf[28:60].split(b"\0")[0].decode("latin1")
    return gui(e).new(dict(kind="font", height=height, weight=weight, italic=bool(italic),
                           underline=bool(underline), face=face or "MS Sans Serif", raw=lf))


@api("CreateHalftonePalette", 1)
def CreateHalftonePalette(e, dc):
    return gui(e).new(dict(kind="palette"))


@api("CreatePalette", 1)
def CreatePalette(e, p):
    return gui(e).new(dict(kind="palette"))


@api("GetObjectA", 3)
def GetObjectA(e, h, size, buf):
    g = gui(e)
    o = g.objs.get(h)
    if o is None:
        return 0
    k = o["kind"]
    if k == "font":
        data = o.get("raw") or (struct.pack("<5i", o["height"], 0, 0, 0, o["weight"]) + bytes(8)
                                + o["face"].encode().ljust(32, b"\0"))
    elif k == "bitmap":
        s = o["surface"]
        bpp = o.get("bpp", 32)
        bits = s.bits if isinstance(s, DibSection) else 0
        stride = ((s.w * bpp + 15) // 16) * 2
        data = struct.pack("<iiiiHHI", 0, s.w, s.h, stride, 1, bpp, bits)
        if size >= 84 and isinstance(s, DibSection):  # DIBSECTION
            data += struct.pack("<IiiHHIIiiII", 40, s.w, s.h if not s.topdown else -s.h, 1, bpp, 0,
                                s.stride * s.h, 0, 0, len(s.palette) if bpp <= 8 else 0, 0)
            data += bytes(12) + struct.pack("<II", 0, 0)
    elif k == "pen":
        data = struct.pack("<IiiI", o["style"], o["width"], 0, 0)
    elif k == "brush":
        c = o.get("color") or (0, 0, 0)
        data = struct.pack("<III", o["style"], c[0] | c[1] << 8 | c[2] << 16, 0)
    elif k == "palette":
        data = struct.pack("<H", 0)
    else:
        data = b""
    if buf:
        e.write(buf, data[:size])
        return min(size, len(data))
    return len(data)


@api("SelectPalette", 3)
def SelectPalette(e, dc, pal, force):
    return 0x5000 + 15


@api("RealizePalette", 1)
def RealizePalette(e, dc):
    return 0


# ---------------------------------------------------------------- bitmaps
def _bitmapinfo(e, p):
    hdr = struct.unpack("<IiiHHIIiiII", e.read(p, 40))
    size, w, h, planes, bpp, comp, _, _, _, used, _ = hdr
    ncolors = used or (1 << bpp if bpp <= 8 else 0)
    pal = []
    if ncolors:
        raw = e.read(p + size, 4 * ncolors)
        pal = [(raw[i * 4 + 2], raw[i * 4 + 1], raw[i * 4]) for i in range(ncolors)]
    return w, h, bpp, comp, pal


@api("CreateDIBSection", 6)
def CreateDIBSection(e, hdc, bmi, usage, ppv, section, off):
    g = gui(e)
    w, h, bpp, comp, pal = _bitmapinfo(e, bmi)
    topdown = h < 0
    h = abs(h)
    stride = ((w * bpp + 31) // 32) * 4
    bits = e.heap_alloc(stride * h + 16)
    if ppv:
        e.w32(ppv, bits)
    surf = DibSection(e, w, h, bpp, bits, pal, topdown)
    return g.new(dict(kind="bitmap", surface=surf, w=w, h=h, bpp=bpp))


@api("CreateCompatibleBitmap", 3)
def CreateCompatibleBitmap(e, hdc, w, h):
    return gui(e).new_surface_bitmap(s32(w), s32(h))


@api("CreateBitmap", 5)
def CreateBitmap(e, w, h, planes, bpp, bits):
    g = gui(e)
    mono = bpp == 1
    hb = g.new_surface_bitmap(s32(w), s32(h), mono)
    if bits:
        s = g.objs[hb]["surface"]
        stride = ((s.w * bpp + 15) // 16) * 2
        raw = np.frombuffer(e.read(bits, stride * s.h), np.uint8).reshape(s.h, stride)
        if mono:
            v = np.unpackbits(raw, axis=1)[:, :s.w] * 255
            s.px = np.repeat(v[:, :, None], 3, 2).astype(np.uint8)
    g.objs[hb]["bpp"] = bpp
    return hb


@api("CreateDIBitmap", 6)
def CreateDIBitmap(e, hdc, hdr, init, bits, bmi, usage):
    g = gui(e)
    w, h, bpp, comp, pal = _bitmapinfo(e, bmi if bmi else hdr)
    hb = g.new_surface_bitmap(w, abs(h))
    if init & 4 and bits:  # CBM_INIT
        tmp = DibSection(e, w, abs(h), bpp, bits, pal, h < 0)
        g.objs[hb]["surface"].px = tmp.load()
    return hb


@api("GetDIBits", 7)
def GetDIBits(e, hdc, hbm, start, lines, bits, bmi, usage):
    g = gui(e)
    o = g.objs.get(hbm)
    if not o:
        return 0
    s = o["surface"]
    size = e.u32(bmi)
    if not bits:
        # fill in the header only
        bpp = o.get("bpp", 32)
        e.write(bmi + 4, struct.pack("<iiHHI", s.w, s.h, 1, bpp if bpp != 32 else 32, 0))
        return s.h
    w, h, bpp, comp, pal = _bitmapinfo(e, bmi)
    if bpp <= 8 and not pal:
        pal = [(0, 0, 0), (255, 255, 255)] if bpp == 1 else [(i, i, i) for i in range(1 << bpp)]
    tmp = DibSection(e, w, abs(h), bpp, bits, pal, h < 0)
    px = s.load()
    if px.shape[:2] != (abs(h), w):
        nh, nw = abs(h), w
        out = np.zeros((nh, nw, 3), np.uint8)
        out[:min(nh, px.shape[0]), :min(nw, px.shape[1])] = px[:nh, :nw]
        px = out
    tmp.store(px)
    if bpp <= 8:
        for i, (r, gg, b) in enumerate(pal):
            e.write(bmi + size + 4 * i, bytes([b, gg, r, 0]))
    return abs(h)


@api("GetBitmapBits", 3)
def GetBitmapBits(e, hbm, n, bits):
    return 0


@api("SetDIBColorTable", 4)
def SetDIBColorTable(e, hdc, start, n, colors):
    dc = DCof(e, hdc)
    if dc and dc.bitmap:
        s = gui(e).objs[dc.bitmap]["surface"]
        if isinstance(s, DibSection):
            raw = e.read(colors, 4 * n)
            pal = list(s.palette) + [(0, 0, 0)] * max(0, start + n - len(s.palette))
            for i in range(n):
                pal[start + i] = (raw[4 * i + 2], raw[4 * i + 1], raw[4 * i])
            s.palette = pal
    return n


@api("GetDIBColorTable", 4)
def GetDIBColorTable(e, hdc, start, n, colors):
    dc = DCof(e, hdc)
    if dc and dc.bitmap:
        s = gui(e).objs[dc.bitmap]["surface"]
        if isinstance(s, DibSection):
            pal = s.palette[start:start + n]
            for i, (r, g, b) in enumerate(pal):
                e.write(colors + 4 * i, bytes([b, g, r, 0]))
            return len(pal)
    return 0


# ---------------------------------------------------------------- drawing helpers
def _target(e, hdc):
    dc = DCof(e, hdc)
    if dc is None:
        return None, None
    return dc, dc.surface()


def _brush_fill(g, dc, shape, origin=(0, 0)):
    b = g.objs.get(dc.brush)
    if not b or b.get("style") == 1:
        return None
    if b.get("style") == 3 and b.get("pattern") in g.objs:
        pat = g.objs[b["pattern"]]["surface"].load()
        ph, pw = pat.shape[:2]
        oy, ox = origin[1] % ph, origin[0] % pw
        reps = (shape[0] // ph + 3, shape[1] // pw + 3, 1)
        return np.tile(pat, reps)[oy:oy + shape[0], ox:ox + shape[1]]
    c = b.get("color") or (0, 0, 0)
    return np.broadcast_to(np.array(c, np.uint8), shape + (3,))


def _clip_rect(dc, s, x0, y0, x1, y1):
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, s.w), min(y1, s.h)
    return x0, y0, x1, y1


def _write(dc, s, px, x0, y0, region):
    """Write region into surface pixels px at (x0,y0), honoring the DC clip mask."""
    h, w = region.shape[:2]
    if dc.clip is not None:
        m = dc.clip[y0:y0 + h, x0:x0 + w]
        px[y0:y0 + h, x0:x0 + w][m] = region[m]
    else:
        px[y0:y0 + h, x0:x0 + w] = region


def _fill_rect(e, dc, s, x0, y0, x1, y1, color=None, brush=True):
    x0, y0 = dc.dev(x0, y0)
    x1, y1 = dc.dev(x1, y1)
    x0, y0, x1, y1 = _clip_rect(dc, s, x0, y0, x1, y1)
    if x1 <= x0 or y1 <= y0:
        return
    if color is not None:
        region = np.broadcast_to(np.array(color, np.uint8), (y1 - y0, x1 - x0, 3))
    else:
        region = _brush_fill(gui(e), dc, (y1 - y0, x1 - x0), origin=(x0, y0))
        if region is None:
            return
    px = s.load()
    _write(dc, s, px, x0, y0, np.array(region))
    s.store(px)


def _pen(g, dc):
    p = g.objs.get(dc.pen)
    if not p or p["style"] == 5:
        return None
    return p


def _line_points(x0, y0, x1, y1):
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    x, y = x0, y0
    while (x, y) != (x1, y1):     # GDI excludes the end point
        pts.append((x, y))
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return pts


def _draw_line(e, dc, s, x0, y0, x1, y1):
    g = gui(e)
    p = _pen(g, dc)
    if p is None or s is None:
        return
    X0, Y0 = dc.dev(x0, y0)
    X1, Y1 = dc.dev(x1, y1)
    px = s.load()
    c = np.array(p["color"], np.uint8)
    for (x, y) in _line_points(X0, Y0, X1, Y1):
        for ox in range(p["width"]):
            for oy in range(p["width"]):
                xx, yy = x + ox - p["width"] // 2, y + oy - p["width"] // 2
                if 0 <= xx < s.w and 0 <= yy < s.h and (dc.clip is None or dc.clip[yy, xx]):
                    if dc.rop2 == 13:
                        px[yy, xx] = c
                    elif dc.rop2 == 7:      # R2_XORPEN
                        px[yy, xx] ^= c
                    elif dc.rop2 == 6:      # R2_NOT
                        px[yy, xx] = ~px[yy, xx]
                    else:
                        px[yy, xx] = c
    s.store(px)


# ---------------------------------------------------------------- GDI drawing APIs
@api("SetTextColor", 2)
def SetTextColor(e, hdc, c):
    dc = DCof(e, hdc)
    if not dc:
        return 0
    old = dc.text_color
    dc.text_color = colorref(c)
    return old[0] | old[1] << 8 | old[2] << 16


@api("SetBkColor", 2)
def SetBkColor(e, hdc, c):
    dc = DCof(e, hdc)
    if not dc:
        return 0
    old = dc.bk_color
    dc.bk_color = colorref(c)
    return old[0] | old[1] << 8 | old[2] << 16


@api("SetBkMode", 2)
def SetBkMode(e, hdc, m):
    dc = DCof(e, hdc)
    old, dc.bk_mode = dc.bk_mode, m
    return old


@api("SetROP2", 2)
def SetROP2(e, hdc, m):
    dc = DCof(e, hdc)
    old, dc.rop2 = dc.rop2, m
    return old


@api("SetStretchBltMode", 2)
def SetStretchBltMode(e, hdc, m):
    return 3


@api("SetBrushOrgEx", 4)
def SetBrushOrgEx(e, hdc, x, y, p):
    dc = DCof(e, hdc)
    if p:
        e.write(p, struct.pack("<2i", *dc.brush_org))
    dc.brush_org = (s32(x), s32(y))
    return 1


@api("GetBrushOrgEx", 2)
def GetBrushOrgEx(e, hdc, p):
    e.write(p, struct.pack("<2i", *DCof(e, hdc).brush_org))
    return 1


@api("SetWindowOrgEx", 4)
def SetWindowOrgEx(e, hdc, x, y, p):
    dc = DCof(e, hdc)
    if p:
        e.write(p, struct.pack("<2i", *dc.worg))
    dc.worg = (s32(x), s32(y))
    return 1


@api("GetWindowOrgEx", 2)
def GetWindowOrgEx(e, hdc, p):
    e.write(p, struct.pack("<2i", *DCof(e, hdc).worg))
    return 1


@api("SetViewportOrgEx", 4)
def SetViewportOrgEx(e, hdc, x, y, p):
    dc = DCof(e, hdc)
    if p:
        e.write(p, struct.pack("<2i", *dc.vorg))
    dc.vorg = (s32(x), s32(y))
    return 1


@api("GetDCOrgEx", 2)
def GetDCOrgEx(e, hdc, p):
    e.write(p, struct.pack("<2i", 0, 0))
    return 1


@api("MoveToEx", 4)
def MoveToEx(e, hdc, x, y, p):
    dc = DCof(e, hdc)
    if p:
        e.write(p, struct.pack("<2i", *dc.pos))
    dc.pos = (s32(x), s32(y))
    return 1


@api("GetCurrentPositionEx", 2)
def GetCurrentPositionEx(e, hdc, p):
    e.write(p, struct.pack("<2i", *DCof(e, hdc).pos))
    return 1


@api("LineTo", 3)
def LineTo(e, hdc, x, y):
    dc, s = _target(e, hdc)
    x, y = s32(x), s32(y)
    if dc:
        _draw_line(e, dc, s, dc.pos[0], dc.pos[1], x, y)
        dc.pos = (x, y)
    return 1


@api("Polyline", 3)
def Polyline(e, hdc, pts, n):
    dc, s = _target(e, hdc)
    p = [struct.unpack("<2i", e.read(pts + 8 * i, 8)) for i in range(n)]
    for a, b in zip(p, p[1:]):
        _draw_line(e, dc, s, a[0], a[1], b[0], b[1])
    return 1


@api("Rectangle", 5)
def Rectangle(e, hdc, l, t, r, b):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    l, t, r, b = s32(l), s32(t), s32(r), s32(b)
    _fill_rect(e, dc, s, l, t, r, b)
    if _pen(gui(e), dc):
        for a, c in (((l, t), (r - 1, t)), ((r - 1, t), (r - 1, b - 1)), ((r - 1, b - 1), (l, b - 1)), ((l, b - 1), (l, t))):
            _draw_line(e, dc, s, a[0], a[1], c[0], c[1])
    return 1


@api("Ellipse", 5)
def Ellipse(e, hdc, l, t, r, b):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    l, t, r, b = s32(l), s32(t), s32(r), s32(b)
    X0, Y0 = dc.dev(l, t)
    X1, Y1 = dc.dev(r, b)
    yy, xx = np.mgrid[0:s.h, 0:s.w]
    cx, cy, rx, ry = (X0 + X1 - 1) / 2, (Y0 + Y1 - 1) / 2, max((X1 - X0) / 2, 0.5), max((Y1 - Y0) / 2, 0.5)
    inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    px = s.load()
    fill = _brush_fill(gui(e), dc, (s.h, s.w))
    m = inside if dc.clip is None else inside & dc.clip
    if fill is not None:
        px[m] = np.array(fill)[m]
    p = _pen(gui(e), dc)
    if p:
        inner = ((xx - cx) / max(rx - 1, 0.5)) ** 2 + ((yy - cy) / max(ry - 1, 0.5)) ** 2 <= 1.0
        edge = inside & ~inner
        if dc.clip is not None:
            edge &= dc.clip
        px[edge] = p["color"]
    s.store(px)
    return 1


@api("FillRect", 3)
def FillRect(e, hdc, rect, brush):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    l, t, r, b = struct.unpack("<4i", e.read(rect, 16))
    g = gui(e)
    if brush < 0x100:   # COLOR_xxx + 1
        _fill_rect(e, dc, s, l, t, r, b, color=(192, 192, 192))
        return 1
    bo = g.objs.get(brush, {})
    if bo.get("style") == 1:
        return 1
    if bo.get("style") == 3:
        saved, dc.brush = dc.brush, brush
        _fill_rect(e, dc, s, l, t, r, b)
        dc.brush = saved
        return 1
    _fill_rect(e, dc, s, l, t, r, b, color=bo.get("color") or (0, 0, 0))
    return 1


@api("FrameRect", 3)
def FrameRect(e, hdc, rect, brush):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    l, t, r, b = struct.unpack("<4i", e.read(rect, 16))
    color = gui(e).objs.get(brush, {}).get("color") or (0, 0, 0)
    for (a, bb, c, d) in ((l, t, r, t + 1), (l, b - 1, r, b), (l, t, l + 1, b), (r - 1, t, r, b)):
        _fill_rect(e, dc, s, a, bb, c, d, color=color)
    return 1


@api("DrawFocusRect", 2)
def DrawFocusRect(e, hdc, rect):
    return 1


@api("DrawEdge", 4)
def DrawEdge(e, hdc, rect, edge, flags):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    l, t, r, b = struct.unpack("<4i", e.read(rect, 16))
    light, dark = (255, 255, 255), (128, 128, 128)
    if edge & 0x0A and not edge & 0x05:   # sunken
        light, dark = dark, light
    _fill_rect(e, dc, s, l, t, r, t + 1, color=light)
    _fill_rect(e, dc, s, l, t, l + 1, b, color=light)
    _fill_rect(e, dc, s, l, b - 1, r, b, color=dark)
    _fill_rect(e, dc, s, r - 1, t, r, b, color=dark)
    return 1


@api("DrawFrameControl", 4)
def DrawFrameControl(e, hdc, rect, typ, state):
    return 1


@api("PatBlt", 6)
def PatBlt(e, hdc, x, y, w, h, rop):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    x, y, w, h = s32(x), s32(y), s32(w), s32(h)
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    X0, Y0 = dc.dev(x, y)
    x0, y0, x1, y1 = _clip_rect(dc, s, X0, Y0, X0 + w, Y0 + h)
    if x1 <= x0 or y1 <= y0:
        return 1
    px = s.load()
    D = px[y0:y1, x0:x1]
    P = _brush_fill(gui(e), dc, (y1 - y0, x1 - x0))
    P = np.zeros_like(D) if P is None else np.array(P)
    out = rop3(rop, P, np.zeros_like(D), D)
    _write(dc, s, px, x0, y0, out)
    s.store(px)
    return 1


def _blt(e, hdcd, xd, yd, wd, hd, hdcs, xs, ys, ws, hs, rop, mask=None):
    g = gui(e)
    dd, sd = _target(e, hdcd)
    if sd is None:
        return 1
    xd, yd, wd, hd = s32(xd), s32(yd), s32(wd), s32(hd)
    Xd, Yd = dd.dev(xd, yd)
    if hdcs:
        ds, ss = _target(e, hdcs)
        if ss is None:
            return 1
        xs, ys, ws, hs = s32(xs), s32(ys), s32(ws), s32(hs)
        Xs, Ys = ds.dev(xs, ys)
        spx = ss.load()
        # source rect (clamped with black outside)
        src = np.zeros((abs(hs), abs(ws), 3), np.uint8)
        sx0, sy0 = max(Xs, 0), max(Ys, 0)
        sx1, sy1 = min(Xs + abs(ws), ss.w), min(Ys + abs(hs), ss.h)
        if sx1 > sx0 and sy1 > sy0:
            src[sy0 - Ys:sy1 - Ys, sx0 - Xs:sx1 - Xs] = spx[sy0:sy1, sx0:sx1]
        if ws < 0:
            src = src[:, ::-1]
        if hs < 0:
            src = src[::-1]
        if (abs(ws), abs(hs)) != (abs(wd), abs(hd)) and abs(wd) and abs(hd):
            yi = (np.arange(abs(hd)) * abs(hs) // abs(hd)).clip(0, abs(hs) - 1)
            xi = (np.arange(abs(wd)) * abs(ws) // abs(wd)).clip(0, abs(ws) - 1)
            src = src[yi][:, xi]
        if wd < 0:
            src = src[:, ::-1]
        if hd < 0:
            src = src[::-1]
        # mono <-> color conversions
        if ss.mono and not sd.mono:
            bits = src[:, :, 0] > 127
            src = np.where(bits[:, :, None], np.array(dd.bk_color, np.uint8), np.array(dd.text_color, np.uint8)).astype(np.uint8)
        elif sd.mono and not ss.mono:
            eq = (src == np.array(ds.bk_color, np.uint8)).all(-1)
            src = np.repeat((eq * 255).astype(np.uint8)[:, :, None], 3, 2)
    else:
        src = np.zeros((abs(hd), abs(wd), 3), np.uint8)
    if wd < 0:
        Xd += wd
    if hd < 0:
        Yd += hd
    wd, hd = abs(wd), abs(hd)
    x0, y0, x1, y1 = _clip_rect(dd, sd, Xd, Yd, Xd + wd, Yd + hd)
    if x1 <= x0 or y1 <= y0:
        return 1
    dpx = sd.load()
    D = dpx[y0:y1, x0:x1]
    S = src[y0 - Yd:y1 - Yd, x0 - Xd:x1 - Xd]
    P = _brush_fill(g, dd, (y1 - y0, x1 - x0))
    P = np.zeros_like(D) if P is None else np.array(P)
    if mask is not None:
        fore = rop3(rop, P, S, D)
        back = rop3((rop >> 8) & 0xFF0000, P, S, D)
        m = mask[y0 - Yd:y1 - Yd, x0 - Xd:x1 - Xd]
        out = np.where(m[:, :, None], fore, back)
    else:
        out = rop3(rop, P, S, D)
    _write(dd, sd, dpx, x0, y0, out)
    sd.store(dpx)
    return 1


@api("BitBlt", 9)
def BitBlt(e, hdcd, x, y, w, h, hdcs, xs, ys, rop):
    return _blt(e, hdcd, x, y, w, h, hdcs, xs, ys, w, h, rop)


@api("StretchBlt", 11)
def StretchBlt(e, hdcd, x, y, w, h, hdcs, xs, ys, ws, hs, rop):
    return _blt(e, hdcd, x, y, w, h, hdcs, xs, ys, ws, hs, rop)


@api("MaskBlt", 12)
def MaskBlt(e, hdcd, x, y, w, h, hdcs, xs, ys, hmask, xm, ym, rop):
    g = gui(e)
    mo = g.objs.get(hmask)
    mask = None
    if mo:
        mpx = mo["surface"].load()
        xm, ym = s32(xm), s32(ym)
        mask = mpx[ym:ym + s32(h), xm:xm + s32(w), 0] > 127
    return _blt(e, hdcd, x, y, w, h, hdcs, xs, ys, w, h, rop, mask=mask)


@api("GetPixel", 3)
def GetPixel(e, hdc, x, y):
    dc, s = _target(e, hdc)
    if s is None:
        return 0xFFFFFFFF
    X, Y = dc.dev(s32(x), s32(y))
    if not (0 <= X < s.w and 0 <= Y < s.h):
        return 0xFFFFFFFF
    r, g, b = s.load()[Y, X]
    return int(r) | int(g) << 8 | int(b) << 16


@api("SetPixel", 4)
def SetPixel(e, hdc, x, y, c):
    dc, s = _target(e, hdc)
    if s is None:
        return 0
    X, Y = dc.dev(s32(x), s32(y))
    if 0 <= X < s.w and 0 <= Y < s.h:
        px = s.load()
        px[Y, X] = colorref(c)
        s.store(px)
    return c


# ---------------------------------------------------------------- clipping
def _ensure_clip(dc, s):
    if dc.clip is None:
        dc.clip = np.ones((s.h, s.w), bool)


@api("IntersectClipRect", 5)
def IntersectClipRect(e, hdc, l, t, r, b):
    dc, s = _target(e, hdc)
    if s is None:
        return 2
    _ensure_clip(dc, s)
    X0, Y0 = dc.dev(s32(l), s32(t))
    X1, Y1 = dc.dev(s32(r), s32(b))
    m = np.zeros_like(dc.clip)
    m[max(Y0, 0):max(Y1, 0), max(X0, 0):max(X1, 0)] = True
    dc.clip &= m
    return 2


@api("ExcludeClipRect", 5)
def ExcludeClipRect(e, hdc, l, t, r, b):
    dc, s = _target(e, hdc)
    if s is None:
        return 2
    _ensure_clip(dc, s)
    X0, Y0 = dc.dev(s32(l), s32(t))
    X1, Y1 = dc.dev(s32(r), s32(b))
    dc.clip[max(Y0, 0):max(Y1, 0), max(X0, 0):max(X1, 0)] = False
    return 2


@api("GetClipBox", 2)
def GetClipBox(e, hdc, p):
    dc, s = _target(e, hdc)
    w, h = (s.w, s.h) if s else (1024, 768)
    l, t = dc.dev(0, 0) if dc else (0, 0)
    e.write(p, struct.pack("<4i", -l, -t, w - l, h - t))
    return 2


@api("RectVisible", 2)
def RectVisible(e, hdc, p):
    return 1


# ---------------------------------------------------------------- text
def _text_draw(e, dc, s, x, y, text, opaque_rect=None, clip_rect=None):
    from PIL import Image, ImageDraw
    g = gui(e)
    font = g.pil_font(dc.font)
    X, Y = dc.dev(x, y)
    px = s.load()
    if opaque_rect is not None:
        l, t, r, b = opaque_rect
        L, T = dc.dev(l, t)
        R, B = dc.dev(r, b)
        L, T, R, B = _clip_rect(dc, s, L, T, R, B)
        if R > L and B > T:
            region = np.broadcast_to(np.array(dc.bk_color, np.uint8), (B - T, R - L, 3))
            _write(dc, s, px, L, T, np.array(region))
    if text:
        asc, desc = font.getmetrics()
        width = int(font.getlength(text)) + 4
        height = asc + desc + 2
        mask = Image.new("1", (width, height), 0)
        ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=1)
        m = np.array(mask, bool)
        if dc.bk_mode == 2 and opaque_rect is None:
            bg = np.zeros_like(m)
            bg[:asc + desc, :int(font.getlength(text))] = True
        else:
            bg = None
        if dc.align & 8:            # TA_BOTTOM
            Y -= asc + desc
        if dc.align & 0x18 == 0x18:  # TA_BASELINE
            Y += desc
        for yy in range(height):
            ty = Y + yy
            if not 0 <= ty < s.h:
                continue
            for xx in range(width):
                tx = X + xx
                if not 0 <= tx < s.w or (dc.clip is not None and not dc.clip[ty, tx]):
                    continue
                if clip_rect is not None:
                    cl, ct, cr, cb = clip_rect
                    if not (cl <= tx < cr and ct <= ty < cb):
                        continue
                if m[yy, xx]:
                    px[ty, tx] = dc.text_color
                elif bg is not None and bg[yy, xx]:
                    px[ty, tx] = dc.bk_color
    s.store(px)


@api("ExtTextOutA", 8)
def ExtTextOutA(e, hdc, x, y, opts, rect, text, n, dx):
    dc, s = _target(e, hdc)
    if s is None:
        return 1
    t = e.read(text, n).decode("latin1") if text and n else ""
    r = struct.unpack("<4i", e.read(rect, 16)) if rect else None
    _text_draw(e, dc, s, s32(x), s32(y), t, opaque_rect=r if opts & 2 else None,
               clip_rect=None)
    return 1


@api("DrawTextA", 5)
def DrawTextA(e, hdc, text, n, rect, fmt):
    dc, s = _target(e, hdc)
    g = gui(e)
    n = s32(n)
    t = e.cstr(text) if n < 0 else e.read(text, n).decode("latin1")
    t = t.replace("&&", "\0").replace("&", "").replace("\0", "&") if not fmt & 0x800 else t
    l, top, r, b = struct.unpack("<4i", e.read(rect, 16))
    font = g.pil_font(dc.font if dc else 0)
    asc, desc = font.getmetrics()
    w = int(font.getlength(t))
    if fmt & 0x400:   # DT_CALCRECT
        e.write(rect, struct.pack("<4i", l, top, l + w, top + asc + desc))
        return asc + desc
    if s is None:
        return asc + desc
    x = l + (r - l - w) // 2 if fmt & 1 else (r - w if fmt & 2 else l)
    y = top + (b - top - asc - desc) // 2 if fmt & 4 else (b - asc - desc if fmt & 8 else top)
    _text_draw(e, dc, s, x, y, t)
    return asc + desc


@api("GetTextExtentPoint32A", 4)
def GetTextExtentPoint32A(e, hdc, text, n, size):
    dc = DCof(e, hdc)
    font = gui(e).pil_font(dc.font if dc else 0)
    t = e.read(text, n).decode("latin1") if n else ""
    asc, desc = font.getmetrics()
    e.write(size, struct.pack("<2i", int(round(font.getlength(t))), asc + desc))
    return 1


@api("GetTextMetricsA", 2)
def GetTextMetricsA(e, hdc, p):
    dc = DCof(e, hdc)
    g = gui(e)
    font = g.pil_font(dc.font if dc else 0)
    asc, desc = font.getmetrics()
    avg = max(int(font.getlength("x")), 1)
    mx = max(int(font.getlength("W")), 1)
    fo = g.objs.get(dc.font if dc else 0, {})
    tm = struct.pack("<11i", asc + desc, asc, desc, 0, 0, avg, mx, fo.get("weight", 400), 0, 96, 96)
    tm += bytes([32, 255, 31, 32]) + bytes([1 if fo.get("italic") else 0, 0, 0, 0x22, 0])
    e.write(p, tm[:56].ljust(56, b"\0"))
    return 1


@api("SetTextAlign", 2)
def SetTextAlign(e, hdc, a):
    dc = DCof(e, hdc)
    old, dc.align = dc.align, a
    return old


# ---------------------------------------------------------------- misc rect helpers
@api("SetRect", 5)
def SetRect(e, p, l, t, r, b):
    e.write(p, struct.pack("<4I", l, t, r, b))
    return 1


@api("OffsetRect", 3)
def OffsetRect(e, p, dx, dy):
    l, t, r, b = struct.unpack("<4i", e.read(p, 16))
    dx, dy = s32(dx), s32(dy)
    e.write(p, struct.pack("<4i", l + dx, t + dy, r + dx, b + dy))
    return 1


@api("InflateRect", 3)
def InflateRect(e, p, dx, dy):
    l, t, r, b = struct.unpack("<4i", e.read(p, 16))
    dx, dy = s32(dx), s32(dy)
    e.write(p, struct.pack("<4i", l - dx, t - dy, r + dx, b + dy))
    return 1


@api("IntersectRect", 3)
def IntersectRect(e, d, a, b):
    A = struct.unpack("<4i", e.read(a, 16))
    B = struct.unpack("<4i", e.read(b, 16))
    r = (max(A[0], B[0]), max(A[1], B[1]), min(A[2], B[2]), min(A[3], B[3]))
    if r[2] <= r[0] or r[3] <= r[1]:
        e.write(d, bytes(16))
        return 0
    e.write(d, struct.pack("<4i", *r))
    return 1


@api("IsRectEmpty", 1)
def IsRectEmpty(e, p):
    l, t, r, b = struct.unpack("<4i", e.read(p, 16))
    return int(r <= l or b <= t)


@api("EqualRect", 2)
def EqualRect(e, a, b):
    return int(e.read(a, 16) == e.read(b, 16))


@api("PtInRect", 3)
def PtInRect(e, p, x, y):
    l, t, r, b = struct.unpack("<4i", e.read(p, 16))
    return int(l <= s32(x) < r and t <= s32(y) < b)


# ---------------------------------------------------------------- icons, menus (recorded only)
@api("CreateIcon", 7)
def CreateIcon(e, *a):
    return gui(e).new(dict(kind="icon"))


@api("GetIconInfo", 2)
def GetIconInfo(e, h, p):
    e.write(p, struct.pack("<IIIII", 1, 0, 0, 0, 0))
    return 1


for _n, _k in (("DrawIcon", 4), ("DrawIconEx", 9), ("DrawMenuBar", 1), ("SetMenu", 2)):
    api(_n, _k)(lambda e, *a: 1)


def _menu(e, h):
    return gui(e).menus.setdefault(h, [])


@api("CreatePopupMenu", 0)
def CreatePopupMenu(e):
    g = gui(e)
    h = g.new(dict(kind="menu"))
    g.menus[h] = []
    return h


@api("CreateMenu", 0)
def CreateMenu(e):
    return CreatePopupMenu(e)


@api("DestroyMenu", 1)
def DestroyMenu(e, h):
    gui(e).menus.pop(h, None)
    return 1


@api("InsertMenuItemA", 4)
def InsertMenuItemA(e, h, item, bypos, mii):
    cb, mask, ftype, fstate, wid, sub, chk, unchk, data, typedata, cch = struct.unpack("<11I", e.read(mii, 44))
    text = e.cstr(typedata) if mask & 0x10 and not ftype & 0x800 and typedata else ""
    m = _menu(e, h)
    entry = dict(text=text, id=wid, sub=sub, type=ftype, state=fstate)
    pos = item if bypos and item <= len(m) else len(m)
    m.insert(pos, entry)
    return 1


@api("InsertMenuA", 5)
def InsertMenuA(e, h, pos, flags, idn, text):
    _menu(e, h).append(dict(text=e.cstr(text) if text >= 0x10000 and not flags & 0x800 else "", id=idn,
                            sub=idn if flags & 0x10 else 0, type=flags, state=0))
    return 1


@api("GetMenuItemCount", 1)
def GetMenuItemCount(e, h):
    return len(_menu(e, h))


@api("GetMenuItemID", 2)
def GetMenuItemID(e, h, pos):
    m = _menu(e, h)
    return m[pos]["id"] if pos < len(m) else 0xFFFFFFFF


@api("GetSubMenu", 2)
def GetSubMenu(e, h, pos):
    m = _menu(e, h)
    return m[pos]["sub"] if pos < len(m) else 0


@api("RemoveMenu", 3)
def RemoveMenu(e, h, pos, flags):
    m = _menu(e, h)
    if flags & 0x400 and pos < len(m):
        m.pop(pos)
    return 1


@api("DeleteMenu", 3)
def DeleteMenu(e, h, pos, flags):
    return RemoveMenu(e, h, pos, flags)


def _find_item(e, h, item, bypos):
    m = _menu(e, h)
    if bypos:
        return m[item] if item < len(m) else None
    for it in m:
        if it["id"] == item:
            return it
    return None


@api("CheckMenuItem", 3)
def CheckMenuItem(e, h, item, flags):
    it = _find_item(e, h, item, flags & 0x400)
    if not it:
        return 0xFFFFFFFF
    old = it["state"] & 8
    it["state"] = (it["state"] & ~8) | (8 if flags & 8 else 0)
    return old


@api("EnableMenuItem", 3)
def EnableMenuItem(e, h, item, flags):
    it = _find_item(e, h, item, flags & 0x400)
    if not it:
        return 0xFFFFFFFF
    old = it["state"] & 3
    it["state"] = (it["state"] & ~3) | (flags & 3)
    return old


@api("GetMenuState", 3)
def GetMenuState(e, h, item, flags):
    it = _find_item(e, h, item, flags & 0x400)
    return it["state"] if it else 0xFFFFFFFF


@api("SetMenuItemInfoA", 4)
def SetMenuItemInfoA(e, h, item, bypos, mii):
    it = _find_item(e, h, item, bypos)
    if not it:
        return 0
    cb, mask, ftype, fstate, wid, sub = struct.unpack("<6I", e.read(mii, 24))
    typedata = e.u32(mii + 36)
    if mask & 1:
        it["state"] = fstate
    if mask & 2:
        it["id"] = wid
    if mask & 4:
        it["sub"] = sub
    if mask & 0x10:
        it["type"] = ftype
        if not ftype & 0x800 and typedata:
            it["text"] = e.cstr(typedata)
    return 1


for _n, _k in (("GetMenuItemInfoA", 4), ("GetMenuStringA", 5), ("GetSystemMenu", 2)):
    api(_n, _k)(lambda e, *a: 0)


def menu_tree(e, h, depth=0):
    out = []
    for it in _menu(e, h):
        node = dict(text=it["text"], id=it["id"], checked=bool(it["state"] & 8),
                    disabled=bool(it["state"] & 3), separator=bool(it["type"] & 0x800),
                    radio=bool(it["type"] & 0x200), default=bool(it["state"] & 0x1000),
                    **{"break": bool(it["type"] & 0x60)})
        if it["sub"] and it["sub"] in gui(e).menus and depth < 6:
            node["sub"] = menu_tree(e, it["sub"], depth + 1)
        out.append(node)
    return out


@api("TrackPopupMenu", 7)
def TrackPopupMenu(e, h, flags, x, y, res, hwnd, rect):
    g = gui(e)
    tree = menu_tree(e, h)
    g.popups.append((tree, s32(x), s32(y)))
    g.log.append(("popup", h, s32(x), s32(y)))
    cmd = g.menu_choice(tree) if g.menu_choice else None
    g.menu_choice = None
    if not cmd:
        return 0
    if flags & 0x100:          # TPM_RETURNCMD
        return cmd
    g.posted.append((hwnd, 0x111, cmd & 0xFFFF, 0))   # WM_COMMAND
    return 1


# ---------------------------------------------------------------- comctl32 image lists (minimal)
@api("ImageList_Create", 5)
def ImageList_Create(e, cx, cy, flags, n, grow):
    return gui(e).new(dict(kind="imagelist", cx=cx, cy=cy, images=[]))


for _n, _k in (("ImageList_Destroy", 1), ("ImageList_SetBkColor", 2), ("ImageList_GetBkColor", 1),
               ("ImageList_Add", 3), ("ImageList_ReplaceIcon", 3), ("ImageList_Remove", 2),
               ("ImageList_Draw", 6), ("ImageList_DrawEx", 10), ("ImageList_GetImageCount", 1),
               ("ImageList_SetIconSize", 3), ("ImageList_GetIconSize", 3)):
    api(_n, _k)(lambda e, *a: 0)


# ---------------------------------------------------------------- other
@api("GetKeyNameTextA", 3)
def GetKeyNameTextA(e, lp, buf, n):
    return 0


@api("LoadKeyboardLayoutA", 2)
def LoadKeyboardLayoutA(e, name, flags):
    return 0x04090409


@api("GetClipboardData", 1)
def GetClipboardData(e, f):
    return 0


@api("GetOpenFileNameA", 1)
def GetOpenFileNameA(e, p):
    return 0


@api("GetSaveFileNameA", 1)
def GetSaveFileNameA(e, p):
    return 0


@api("VariantCopyInd", 2)
def VariantCopyInd(e, d, s):
    e.write(d, e.read(s, 16))
    return 0


@api("VariantChangeTypeEx", 5)
def VariantChangeTypeEx(e, *a):
    return 0x80020005


for _n, _k in (("midiInGetDevCapsA", 3), ("midiOutGetDevCapsA", 3), ("midiInOpen", 5), ("midiOutOpen", 5),
               ("midiInClose", 1), ("midiOutClose", 1), ("midiInStart", 1), ("midiInStop", 1),
               ("midiOutReset", 1), ("midiInPrepareHeader", 3), ("midiOutPrepareHeader", 3),
               ("midiInAddBuffer", 3), ("midiOutLongMsg", 3)):
    api(_n, _k)(lambda e, *a: 1)
