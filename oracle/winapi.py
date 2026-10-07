"""Win32 API subset for running SQ8L.dll headless (no window ever shown).

Each API is registered with its stdcall argument count. Return None to leave
EAX untouched, an int for EAX, or a tuple (eax, edx).
"""
import configparser
import os
import struct
import sys

from w32emu import EmuStop

INVALID_HANDLE = 0xFFFFFFFF

_REGISTRY = {}


def api(name, nargs):
    def deco(fn):
        _REGISTRY[name] = (nargs, fn)
        return fn
    return deco


class WinApi:
    def __init__(self, emu, sandbox, dll_win_path="C:\\VST\\SQ8L.dll", verbose=False):
        self.emu = emu
        self.sandbox = sandbox
        self.dll_win_path = dll_win_path
        self.verbose = verbose
        self.last_error = 0
        self.next_handle = 0x1000
        self.handles = {}
        self.tls = {}
        self.next_tls = 0
        self.atoms = {}
        self.window_longs = {}
        self.tick = 1000
        self.modules = {"sq8l.dll": emu.base}
        self.missing_procs = set()
        emu.api = self

    def lookup(self, name):
        return _REGISTRY.get(name)

    def new_handle(self, obj=None):
        h = self.next_handle
        self.next_handle += 4
        self.handles[h] = obj
        return h

    def log(self, *a):
        if self.verbose:
            print("   ", *a, file=sys.stderr)

    # map a Windows path into the sandbox directory
    def host_path(self, winpath):
        p = winpath.replace("/", "\\")
        if len(p) > 1 and p[1] == ":":
            p = p[0].upper() + p[2:]
        else:
            p = "C\\VST\\" + p
        return os.path.join(self.sandbox, *[x for x in p.split("\\") if x])


# ---------------------------------------------------------------- modules & resources
@api("GetModuleHandleA", 1)
def GetModuleHandleA(e, lpName):
    if not lpName:
        return e.base
    name = e.cstr(lpName).lower()
    if not name.endswith(".dll"):
        name += ".dll"
    mods = e.api.modules
    if name not in mods:
        mods[name] = 0x70000000 + 0x10000 * len(mods)
    return mods[name]


@api("LoadLibraryA", 1)
def LoadLibraryA(e, lpName):
    return GetModuleHandleA(e, lpName)


@api("LoadLibraryExA", 3)
def LoadLibraryExA(e, lpName, hFile, flags):
    name = e.cstr(lpName)
    # resource-only language DLLs (SQ8L.ENU etc.) don't exist
    if not name.lower().endswith(".dll"):
        e.api.last_error = 2
        return 0
    return GetModuleHandleA(e, lpName)


@api("FreeLibrary", 1)
def FreeLibrary(e, h):
    return 1


@api("GetModuleFileNameA", 3)
def GetModuleFileNameA(e, hMod, buf, size):
    name = e.api.dll_win_path if hMod in (e.base,) else "C:\\host\\host.exe"
    return e.write_cstr(buf, name, size)


@api("GetProcAddress", 2)
def GetProcAddress(e, hMod, lpName):
    if lpName < 0x10000:
        return 0
    name = e.cstr(lpName)
    if name in _REGISTRY:
        return e.thunk_for("dyn", name)
    e.api.missing_procs.add(name)
    e.api.log(f"GetProcAddress({name}) -> NULL")
    return 0


def _res_name(e, v):
    if v < 0x10000:
        return v
    s = e.cstr(v)
    if s.startswith("#"):
        return int(s[1:])
    return s.upper()


def _find_resource(e, rtype, rname):
    pe = e.pe
    for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        tid = t.id if t.name is None else str(t.name).upper()
        if tid != rtype:
            continue
        for n in t.directory.entries:
            nid = n.id if n.name is None else str(n.name).upper()
            if nid == rname:
                return n.directory.entries[0]
    return None


@api("FindResourceA", 3)
def FindResourceA(e, hMod, lpName, lpType):
    if hMod not in (0, e.base):
        return 0
    rname, rtype = _res_name(e, lpName), _res_name(e, lpType)
    ent = _find_resource(e, rtype, rname)
    if ent is None:
        e.api.log(f"FindResource({rname!r}, {rtype!r}) -> not found")
        return 0
    return e.api.new_handle(("res", ent.data.struct.OffsetToData, ent.data.struct.Size))


@api("LoadResource", 2)
def LoadResource(e, hMod, hRes):
    _, rva, size = e.api.handles[hRes]
    return e.base + rva


@api("SizeofResource", 2)
def SizeofResource(e, hMod, hRes):
    return e.api.handles[hRes][2]


@api("LockResource", 1)
def LockResource(e, h):
    return h


@api("FreeResource", 1)
def FreeResource(e, h):
    return 0


@api("LoadStringA", 4)
def LoadStringA(e, hInst, uID, buf, maxlen):
    ent = _find_resource(e, 6, (uID >> 4) + 1)
    if ent is None:
        return 0
    data = e.pe.get_data(ent.data.struct.OffsetToData, ent.data.struct.Size)
    off = 0
    for i in range(16):
        n = struct.unpack_from("<H", data, off)[0]
        off += 2
        if i == (uID & 15):
            s = data[off:off + 2 * n].decode("utf-16-le")
            return e.write_cstr(buf, s, maxlen)
        off += 2 * n
    return 0


# ---------------------------------------------------------------- process / version / locale
@api("GetVersion", 0)
def GetVersion(e):
    return 0x0A280105  # Windows XP SP2 (5.1.2600)


@api("GetVersionExA", 1)
def GetVersionExA(e, p):
    e.write(p + 4, struct.pack("<IIII", 5, 1, 2600, 2))
    e.write_cstr(p + 20, "Service Pack 2")
    return 1


@api("GetCommandLineA", 0)
def GetCommandLineA(e):
    if not hasattr(e.api, "_cmdline"):
        e.api._cmdline = e.scratch(b"C:\\host\\host.exe\0")
    return e.api._cmdline


@api("GetStartupInfoA", 1)
def GetStartupInfoA(e, p):
    e.write(p, struct.pack("<I", 68) + b"\0" * 64)


@api("GetThreadLocale", 0)
def GetThreadLocale(e):
    return 0x0409


@api("SetThreadLocale", 1)
def SetThreadLocale(e, l):
    return 1


_LOCALE = {
    0x0001: "0409", 0x0002: "English (United States)", 0x0003: "ENU", 0x0004: "English",
    0x0005: "1", 0x0007: "USA", 0x000E: ".", 0x000F: ",", 0x0010: "3;0", 0x0011: "2",
    0x0014: "$", 0x0015: ".", 0x0016: ",", 0x0019: "2", 0x001A: "", 0x001B: "0", 0x001C: "1",
    0x001D: "/", 0x001E: ":", 0x001F: "M/d/yyyy", 0x0020: "dddd, MMMM dd, yyyy",
    0x0021: "0", 0x0022: "0", 0x0023: "0", 0x0024: "0", 0x0025: "0", 0x0026: "0",
    0x0028: "AM", 0x0029: "PM", 0x1001: "1", 0x1002: "0",
    0x1003: "h:mm:ss tt", 0x1009: "1", 0x100B: "0", 0x1004: "1252", 0x000B: "437",
}
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
for i, d in enumerate(_DAYS):
    _LOCALE[0x2A + i] = d
    _LOCALE[0x31 + i] = d[:3]
for i, m in enumerate(_MONTHS):
    _LOCALE[0x38 + i] = m
    _LOCALE[0x44 + i] = m[:3]


@api("GetLocaleInfoA", 4)
def GetLocaleInfoA(e, lcid, lctype, buf, cch):
    s = _LOCALE.get(lctype & 0xFFFF, "")
    if cch == 0:
        return len(s) + 1
    e.write_cstr(buf, s, cch)
    return min(len(s), cch - 1) + 1


@api("GetCPInfo", 2)
def GetCPInfo(e, cp, p):
    e.write(p, struct.pack("<I", 1) + b"?\0" + b"\0" * 12)
    return 1


@api("GetKeyboardType", 1)
def GetKeyboardType(e, t):
    return {0: 4, 1: 0, 2: 12}.get(t, 0)


@api("EnumCalendarInfoA", 4)
def EnumCalendarInfoA(e, proc, lcid, cal, ctype):
    return 1


@api("GetDateFormatA", 6)
def GetDateFormatA(e, lcid, flags, st, fmt, buf, cch):
    s = "6/12/2008"
    if cch == 0:
        return len(s) + 1
    e.write_cstr(buf, s, cch)
    return len(s) + 1


@api("GetCurrentThreadId", 0)
def GetCurrentThreadId(e):
    return 0x1000


@api("GetCurrentProcessId", 0)
def GetCurrentProcessId(e):
    return 0x800


@api("GetLastError", 0)
def GetLastError(e):
    return e.api.last_error


@api("SetErrorMode", 1)
def SetErrorMode(e, m):
    return 0


@api("GetTickCount", 0)
def GetTickCount(e):
    e.api.tick += 10
    return e.api.tick


@api("GetLocalTime", 1)
def GetLocalTime(e, p):
    e.write(p, struct.pack("<8H", 2008, 6, 4, 12, 12, 0, 0, 0))


@api("GetSystemInfo", 1)
def GetSystemInfo(e, p):
    e.write(p, struct.pack("<HHIIIIIIIHH", 0, 0, 0x1000, 0x10000, 0x7FFEFFFF, 1, 1, 586, 0x10000, 6, 0))


@api("MulDiv", 3)
def MulDiv(e, a, b, c):
    def s(x):
        return x - (1 << 32) if x & 0x80000000 else x
    a, b, c = s(a), s(b), s(c)
    if c == 0:
        return -1
    n = a * b
    q = (abs(n) + abs(c) // 2) // abs(c)
    return q if (n >= 0) == (c > 0) else -q


@api("ExitProcess", 1)
def ExitProcess(e, code):
    raise EmuStop(f"ExitProcess({code})")


@api("UnhandledExceptionFilter", 1)
def UnhandledExceptionFilter(e, p):
    raise EmuStop("UnhandledExceptionFilter")


@api("Sleep", 1)
def Sleep(e, ms):
    return None


# ---------------------------------------------------------------- sync / threads / tls
for _n in ("InitializeCriticalSection", "EnterCriticalSection", "LeaveCriticalSection", "DeleteCriticalSection"):
    api(_n, 1)(lambda e, p: None)


@api("InterlockedIncrement", 1)
def InterlockedIncrement(e, p):
    v = (e.s32(p) + 1) & 0xFFFFFFFF
    e.w32(p, v)
    return v


@api("InterlockedDecrement", 1)
def InterlockedDecrement(e, p):
    v = (e.s32(p) - 1) & 0xFFFFFFFF
    e.w32(p, v)
    return v


@api("TlsAlloc", 0)
def TlsAlloc(e):
    i = e.api.next_tls
    e.api.next_tls += 1
    return i


@api("TlsFree", 1)
def TlsFree(e, i):
    return 1


@api("TlsGetValue", 1)
def TlsGetValue(e, i):
    return e.api.tls.get(i, 0)


@api("TlsSetValue", 2)
def TlsSetValue(e, i, v):
    e.api.tls[i] = v
    return 1


@api("CreateThread", 6)
def CreateThread(e, sec, stack, start, param, flags, ptid):
    e.api.log(f"CreateThread(start={start:#x}) ignored")
    if ptid:
        e.w32(ptid, 0x2000)
    return e.api.new_handle(("thread", start, param))


@api("CreateEventA", 4)
def CreateEventA(e, sec, manual, initial, name):
    return e.api.new_handle(("event",))


@api("SetEvent", 1)
def SetEvent(e, h):
    return 1


@api("WaitForSingleObject", 2)
def WaitForSingleObject(e, h, ms):
    return 0


# ---------------------------------------------------------------- memory
@api("VirtualAlloc", 4)
def VirtualAlloc(e, addr, size, typ, prot):
    if addr:
        if typ & 0x2000:
            # MEM_RESERVE at a given address (the Delphi memory manager tries to grow a region in
            # place): the emulated heap behind it is in use, fail like Windows would
            return 0
        return addr  # commit inside an already mapped reservation
    a = e.heap_alloc(size + 0x10000, zero=False)
    return (a + 0xFFFF) & ~0xFFFF


@api("VirtualFree", 3)
def VirtualFree(e, addr, size, typ):
    return 1


@api("VirtualQuery", 3)
def VirtualQuery(e, addr, buf, length):
    if e.base <= addr < e.base + e.pe.OPTIONAL_HEADER.SizeOfImage:
        info = struct.pack("<IIIIIII", addr & ~0xFFF, e.base, 0x80, 0x1000, 0x1000, 0x20, 0x1000000)
    else:
        info = struct.pack("<IIIIIII", addr & ~0xFFF, addr & ~0xFFFF, 0x04, 0x1000, 0x1000, 0x04, 0x20000)
    e.write(buf, info)
    return 28


@api("LocalAlloc", 2)
def LocalAlloc(e, flags, size):
    return e.heap_alloc(size)


@api("LocalFree", 1)
def LocalFree(e, h):
    return 0


@api("GlobalAlloc", 2)
def GlobalAlloc(e, flags, size):
    a = e.heap_alloc(size + 16)
    e.w32(a, size)
    return a + 16


@api("GlobalReAlloc", 3)
def GlobalReAlloc(e, h, size, flags):
    old = e.u32(h - 16)
    n = GlobalAlloc(e, flags, size)
    e.write(n, e.read(h, min(old, size)))
    return n


@api("GlobalLock", 1)
def GlobalLock(e, h):
    return h


@api("GlobalUnlock", 1)
def GlobalUnlock(e, h):
    return 1


@api("GlobalFree", 1)
def GlobalFree(e, h):
    return 0


@api("GlobalHandle", 1)
def GlobalHandle(e, p):
    return p


@api("GlobalAddAtomA", 1)
def GlobalAddAtomA(e, p):
    s = e.cstr(p)
    return e.api.atoms.setdefault(s, 0xC000 + len(e.api.atoms))


@api("GlobalDeleteAtom", 1)
def GlobalDeleteAtom(e, a):
    return 0


# ---------------------------------------------------------------- strings
@api("lstrlenA", 1)
def lstrlenA(e, p):
    return len(e.cstr(p)) if p else 0


@api("lstrcpyA", 2)
def lstrcpyA(e, d, s):
    e.write_cstr(d, e.cstr(s))
    return d


@api("lstrcpynA", 3)
def lstrcpynA(e, d, s, n):
    e.write_cstr(d, e.cstr(s), n)
    return d


@api("MultiByteToWideChar", 6)
def MultiByteToWideChar(e, cp, flags, mb, cb, wc, cch):
    s = e.cstr(mb) + "\0" if cb == 0xFFFFFFFF else e.read(mb, cb).decode("latin1")
    if cch == 0:
        return len(s)
    out = s[:cch].encode("utf-16-le")
    e.write(wc, out)
    return len(out) // 2


@api("WideCharToMultiByte", 8)
def WideCharToMultiByte(e, cp, flags, wc, cch, mb, cb, defc, used):
    if cch == 0xFFFFFFFF:
        raw = bytearray()
        while True:
            c = e.read(wc + len(raw), 2)
            raw += c
            if c == b"\0\0":
                break
    else:
        raw = e.read(wc, 2 * cch)
    s = raw.decode("utf-16-le").encode("latin1", "replace")
    if cb == 0:
        return len(s)
    e.write(mb, s[:cb])
    return min(len(s), cb)


@api("CompareStringA", 6)
def CompareStringA(e, lcid, flags, s1, n1, s2, n2):
    a = e.cstr(s1) if n1 == 0xFFFFFFFF else e.read(s1, n1).decode("latin1")
    b = e.cstr(s2) if n2 == 0xFFFFFFFF else e.read(s2, n2).decode("latin1")
    if flags & 1:
        a, b = a.lower(), b.lower()
    return 1 if a < b else 3 if a > b else 2


@api("CharNextA", 1)
def CharNextA(e, p):
    return p + 1 if e.u8(p) else p


@api("CharLowerA", 1)
def CharLowerA(e, p):
    if p < 0x10000:
        return ord(chr(p & 0xFF).lower()[0]) & 0xFF
    s = e.cstr(p)
    e.write_cstr(p, s.lower())
    return p


@api("CharLowerBuffA", 2)
def CharLowerBuffA(e, p, n):
    e.write(p, e.read(p, n).decode("latin1").lower().encode("latin1"))
    return n


@api("OemToCharA", 2)
def OemToCharA(e, s, d):
    e.write_cstr(d, e.cstr(s))
    return 1


@api("FormatMessageA", 7)
def FormatMessageA(e, *a):
    return 0


# ---------------------------------------------------------------- files (sandboxed)
class _File:
    def __init__(self, f, path):
        self.f, self.path = f, path


@api("CreateFileA", 7)
def CreateFileA(e, name, access, share, sec, disp, flags, tmpl):
    w = e.api
    winpath = e.cstr(name)
    hp = w.host_path(winpath)
    exists = os.path.isfile(hp)
    # dispositions: 1 CREATE_NEW 2 CREATE_ALWAYS 3 OPEN_EXISTING 4 OPEN_ALWAYS 5 TRUNCATE_EXISTING
    if disp in (3, 5) and not exists or disp == 1 and exists:
        w.last_error = 2 if not exists else 80
        w.log(f"CreateFile({winpath}) -> not found")
        return INVALID_HANDLE
    os.makedirs(os.path.dirname(hp), exist_ok=True)
    if disp in (2, 5) or not exists:
        open(hp, "wb").close()
    writable = bool(access & 0x40000000)
    f = open(hp, "r+b" if writable else "rb")
    w.log(f"CreateFile({winpath}) -> {hp}")
    return w.new_handle(_File(f, hp))


@api("ReadFile", 5)
def ReadFile(e, h, buf, n, pread, ovl):
    obj = e.api.handles.get(h)
    data = obj.f.read(n) if isinstance(obj, _File) else b""
    e.write(buf, data)
    if pread:
        e.w32(pread, len(data))
    return 1


@api("WriteFile", 5)
def WriteFile(e, h, buf, n, pwritten, ovl):
    obj = e.api.handles.get(h)
    data = e.read(buf, n)
    if isinstance(obj, _File):
        obj.f.write(data)
    else:
        sys.stderr.write(data.decode("latin1"))
    if pwritten:
        e.w32(pwritten, n)
    return 1


@api("SetFilePointer", 4)
def SetFilePointer(e, h, dist, phigh, method):
    obj = e.api.handles[h]
    d = dist - (1 << 32) if dist & 0x80000000 else dist
    obj.f.seek(d, method)
    return obj.f.tell()


@api("GetFileSize", 2)
def GetFileSize(e, h, phigh):
    obj = e.api.handles[h]
    if phigh:
        e.w32(phigh, 0)
    return os.path.getsize(obj.path)


@api("SetEndOfFile", 1)
def SetEndOfFile(e, h):
    obj = e.api.handles[h]
    obj.f.truncate()
    return 1


@api("GetFileType", 1)
def GetFileType(e, h):
    return 1 if isinstance(e.api.handles.get(h), _File) else 2


@api("GetStdHandle", 1)
def GetStdHandle(e, n):
    return 0xF000 + (n & 0xFF)


@api("CloseHandle", 1)
def CloseHandle(e, h):
    obj = e.api.handles.pop(h, None)
    if isinstance(obj, _File):
        obj.f.close()
    return 1


@api("FindFirstFileA", 2)
def FindFirstFileA(e, name, data):
    w = e.api
    winpath = e.cstr(name)
    hp = w.host_path(winpath)
    if "*" in hp or not os.path.exists(hp):
        w.last_error = 2
        w.log(f"FindFirstFile({winpath}) -> none")
        return INVALID_HANDLE
    attr = 0x10 if os.path.isdir(hp) else 0x20
    size = 0 if os.path.isdir(hp) else os.path.getsize(hp)
    rec = struct.pack("<I", attr) + b"\0" * 24 + struct.pack("<IIII", 0, size, 0, 0)
    rec += os.path.basename(hp).encode("latin1")[:259].ljust(260, b"\0") + b"\0" * 14
    e.write(data, rec)
    return w.new_handle(("find",))


@api("FindClose", 1)
def FindClose(e, h):
    return 1


@api("FileTimeToLocalFileTime", 2)
def FileTimeToLocalFileTime(e, a, b):
    e.write(b, e.read(a, 8))
    return 1


@api("FileTimeToDosDateTime", 3)
def FileTimeToDosDateTime(e, ft, pdate, ptime):
    e.write(pdate, struct.pack("<H", (28 << 9) | (6 << 5) | 12))
    e.write(ptime, struct.pack("<H", 12 << 11))
    return 1


@api("GetDiskFreeSpaceA", 5)
def GetDiskFreeSpaceA(e, root, spc, bps, free, total):
    for p, v in ((spc, 8), (bps, 512), (free, 1000000), (total, 2000000)):
        if p:
            e.w32(p, v)
    return 1


def _ini(e, fname):
    hp = e.api.host_path(fname)
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    if os.path.isfile(hp):
        cp.read(hp, encoding="latin1")
    return cp, hp


@api("GetPrivateProfileStringA", 6)
def GetPrivateProfileStringA(e, app, key, default, buf, size, fname):
    cp, hp = _ini(e, e.cstr(fname))
    sec, k = e.cstr(app), e.cstr(key)
    d = e.cstr(default) or ""
    if sec is None or k is None:
        raise EmuStop("GetPrivateProfileString enumeration not implemented")
    val = cp.get(sec, k, fallback=d) if cp.has_section(sec) else d
    e.api.log(f"ini [{sec}] {k} = {val!r}")
    return e.write_cstr(buf, val, size)


@api("WritePrivateProfileStringA", 4)
def WritePrivateProfileStringA(e, app, key, val, fname):
    cp, hp = _ini(e, e.cstr(fname))
    sec = e.cstr(app)
    if not cp.has_section(sec):
        cp.add_section(sec)
    if key:
        if val:
            cp.set(sec, e.cstr(key), e.cstr(val))
        else:
            cp.remove_option(sec, e.cstr(key))
    os.makedirs(os.path.dirname(hp), exist_ok=True)
    with open(hp, "w", encoding="latin1") as f:
        cp.write(f, space_around_delimiters=False)
    return 1


# ---------------------------------------------------------------- registry (always empty)
@api("RegOpenKeyExA", 5)
def RegOpenKeyExA(e, *a):
    return 2


@api("RegQueryValueExA", 6)
def RegQueryValueExA(e, *a):
    return 2


@api("RegCloseKey", 1)
def RegCloseKey(e, h):
    return 0


# ---------------------------------------------------------------- OLE automation strings
@api("SysAllocStringLen", 2)
def SysAllocStringLen(e, s, n):
    a = e.heap_alloc(2 * n + 8)
    e.w32(a, 2 * n)
    if s:
        e.write(a + 4, e.read(s, 2 * n))
    return a + 4


@api("SysReAllocStringLen", 3)
def SysReAllocStringLen(e, pbstr, s, n):
    e.w32(pbstr, SysAllocStringLen(e, s, n))
    return 1


@api("SysFreeString", 1)
def SysFreeString(e, b):
    return None


@api("SysStringLen", 1)
def SysStringLen(e, b):
    return e.u32(b - 4) // 2 if b else 0


@api("VariantClear", 1)
def VariantClear(e, v):
    e.write(v, b"\0" * 16)
    return 0


@api("IsEqualGUID", 2)
def IsEqualGUID(e, a, b):
    return int(e.read(a, 16) == e.read(b, 16))


# ---------------------------------------------------------------- GDI / USER (headless fakes)
@api("GetDC", 1)
def GetDC(e, hwnd):
    return e.api.new_handle(("dc",))


@api("GetWindowDC", 1)
def GetWindowDC(e, hwnd):
    return e.api.new_handle(("dc",))


@api("ReleaseDC", 2)
def ReleaseDC(e, hwnd, dc):
    return 1


@api("CreateCompatibleDC", 1)
def CreateCompatibleDC(e, dc):
    return e.api.new_handle(("dc",))


@api("DeleteDC", 1)
def DeleteDC(e, dc):
    return 1


_DEVCAPS = {8: 1024, 10: 768, 12: 32, 14: 1, 24: -1, 38: 0x7E99, 88: 96, 90: 96, 104: 0, 106: 0}


@api("GetDeviceCaps", 2)
def GetDeviceCaps(e, dc, idx):
    return _DEVCAPS.get(idx, 0)


def _logfont(name="MS Sans Serif", height=-11):
    return struct.pack("<iiiii", height, 0, 0, 0, 400) + bytes(8) + name.encode().ljust(32, b"\0")


@api("GetStockObject", 1)
def GetStockObject(e, i):
    return 0x5000 + i


@api("GetObjectA", 3)
def GetObjectA(e, h, size, buf):
    obj = e.api.handles.get(h)
    if (0x5000 <= h < 0x5020 and h - 0x5000 in (10, 11, 12, 13, 14, 16, 17)) or (isinstance(obj, tuple) and obj[0] == "font"):
        data = obj[1] if isinstance(obj, tuple) else _logfont()
    elif isinstance(obj, tuple) and obj[0] == "bitmap":
        data = obj[1]
    else:
        data = bytes(size)
    if buf:
        e.write(buf, data[:size])
    return min(size, len(data)) if buf else len(data)


@api("CreateFontIndirectA", 1)
def CreateFontIndirectA(e, p):
    return e.api.new_handle(("font", e.read(p, 60)))


for _n, _k in (("CreatePenIndirect", 1), ("CreateBrushIndirect", 1), ("CreateSolidBrush", 1),
               ("CreateHalftonePalette", 1), ("CreatePalette", 1)):
    api(_n, _k)(lambda e, *a, _n=_n: e.api.new_handle((_n,)))


@api("DeleteObject", 1)
def DeleteObject(e, h):
    return 1


@api("SelectObject", 2)
def SelectObject(e, dc, h):
    return 0x5000


@api("SelectPalette", 3)
def SelectPalette(e, dc, pal, force):
    return 0x5000 + 15


@api("RealizePalette", 1)
def RealizePalette(e, dc):
    return 0


@api("GetSystemPaletteEntries", 4)
def GetSystemPaletteEntries(e, dc, start, n, p):
    if p:
        e.write(p, bytes(4 * n))
    return 0


@api("GetPaletteEntries", 4)
def GetPaletteEntries(e, pal, start, n, p):
    if p:
        e.write(p, bytes(4 * n))
    return n


_METRICS = {0: 1024, 1: 768, 2: 16, 3: 16, 4: 19, 11: 32, 12: 32, 13: 32, 14: 32, 15: 19,
            19: 1, 20: 16, 21: 16, 32: 4, 33: 4, 36: 4, 37: 4, 42: 0, 43: 3, 49: 16, 50: 16,
            75: 1, 76: 0, 77: 0, 78: 1024, 79: 768, 80: 1}


@api("GetSystemMetrics", 1)
def GetSystemMetrics(e, i):
    return _METRICS.get(i, 0)


@api("SystemParametersInfoA", 4)
def SystemParametersInfoA(e, action, ui, pv, ini):
    if action == 0x68:          # SPI_GETWHEELSCROLLLINES
        e.w32(pv, 3)
    elif action == 0x1F:        # SPI_GETICONTITLELOGFONT
        e.write(pv, _logfont("Tahoma", -11))
    elif action == 0x29:        # SPI_GETNONCLIENTMETRICS
        cb = e.u32(pv)
        m = struct.pack("<I5i", cb, 1, 16, 16, 18, 18) + _logfont("Tahoma", -11)
        m += struct.pack("<2i", 12, 15) + _logfont("Tahoma", -11)
        m += struct.pack("<2i", 18, 18) + _logfont("Tahoma", -11) * 3
        e.write(pv, m[:cb])
    elif action == 0x30:        # SPI_GETWORKAREA
        e.write(pv, struct.pack("<4i", 0, 0, 1024, 740))
    elif pv:
        e.w32(pv, 0)
    return 1


@api("GetSysColor", 1)
def GetSysColor(e, i):
    return 0xC0C0C0


@api("LoadCursorA", 2)
def LoadCursorA(e, hInst, name):
    return e.api.new_handle(("cursor", name))


@api("LoadIconA", 2)
def LoadIconA(e, hInst, name):
    return e.api.new_handle(("icon", name))


@api("LoadBitmapA", 2)
def LoadBitmapA(e, hInst, name):
    return e.api.new_handle(("bitmap", bytes(24)))


for _n, _k in (("DestroyCursor", 1), ("DestroyIcon", 1), ("DestroyWindow", 1), ("DestroyMenu", 1),
               ("UnhookWindowsHookEx", 1), ("KillTimer", 2), ("ShowWindow", 2), ("UpdateWindow", 1),
               ("EnumWindows", 2), ("EnumThreadWindows", 3), ("WinHelpA", 4), ("InvalidateRect", 3),
               ("UnregisterClassA", 2), ("timeKillEvent", 1), ("timeEndPeriod", 1), ("timeBeginPeriod", 1),
               ("SetForegroundWindow", 1), ("ReleaseCapture", 0), ("ActivateKeyboardLayout", 2)):
    api(_n, _k)(lambda e, *a: 1)

for _n, _k in (("GetFocus", 0), ("GetActiveWindow", 0), ("GetForegroundWindow", 0), ("GetCapture", 0),
               ("PeekMessageA", 5), ("SendMessageA", 4), ("PostMessageA", 4), ("GetKeyState", 1),
               ("MapVirtualKeyA", 2), ("GetClassInfoA", 3), ("IsWindow", 1), ("IsWindowVisible", 1),
               ("GetParent", 1), ("GetWindow", 2), ("GetTopWindow", 1), ("GetLastActivePopup", 1),
               ("midiInGetNumDevs", 0), ("midiOutGetNumDevs", 0), ("GetMenu", 1), ("SetCursor", 1),
               ("GetCursor", 0), ("SetFocus", 1), ("GetPropA", 2), ("RemovePropA", 2), ("FindWindowA", 2)):
    api(_n, _k)(lambda e, *a: 0)


@api("GetDesktopWindow", 0)
def GetDesktopWindow(e):
    return 0x10010


@api("RegisterWindowMessageA", 1)
def RegisterWindowMessageA(e, p):
    return e.api.atoms.setdefault("msg:" + e.cstr(p), 0xC000 + len(e.api.atoms))


@api("RegisterClipboardFormatA", 1)
def RegisterClipboardFormatA(e, p):
    return e.api.atoms.setdefault("cf:" + e.cstr(p), 0xC000 + len(e.api.atoms))


@api("GetKeyboardLayoutList", 2)
def GetKeyboardLayoutList(e, n, p):
    if n and p:
        e.w32(p, 0x04090409)
    return 1


@api("GetKeyboardLayout", 1)
def GetKeyboardLayout(e, tid):
    return 0x04090409


@api("GetKeyboardState", 1)
def GetKeyboardState(e, p):
    e.write(p, bytes(256))
    return 1


@api("SetWindowsHookExA", 4)
def SetWindowsHookExA(e, idh, proc, hmod, tid):
    return e.api.new_handle(("hook", idh, proc))


@api("RegisterClassA", 1)
def RegisterClassA(e, p):
    return 0xC100 + len(e.api.atoms)


@api("CreateWindowExA", 12)
def CreateWindowExA(e, exstyle, cls, title, style, x, y, w, h, parent, menu, inst, param):
    c = e.cstr(cls) if cls >= 0x10000 else f"#{cls}"
    e.api.log(f"CreateWindowEx(class={c}) -> fake hwnd")
    return e.api.new_handle(("hwnd", c))


@api("SetWindowLongA", 3)
def SetWindowLongA(e, h, idx, v):
    old = e.api.window_longs.get((h, idx), 0)
    e.api.window_longs[(h, idx)] = v
    return old


@api("GetWindowLongA", 2)
def GetWindowLongA(e, h, idx):
    return e.api.window_longs.get((h, idx), 0)


@api("SetPropA", 3)
def SetPropA(e, h, name, v):
    return 1


@api("GetCursorPos", 1)
def GetCursorPos(e, p):
    e.write(p, bytes(8))
    return 1


@api("SetTimer", 4)
def SetTimer(e, hwnd, tid, ms, proc):
    return tid or 1


@api("timeSetEvent", 5)
def timeSetEvent(e, delay, res, proc, user, flags):
    e.api.log(f"timeSetEvent({delay}ms, proc={proc:#x}) ignored")
    return 1


@api("MessageBoxA", 4)
def MessageBoxA(e, hwnd, text, caption, typ):
    print(f"[MessageBox] {e.cstr(caption)}: {e.cstr(text)}", file=sys.stderr)
    return 1


@api("GetClientRect", 2)
def GetClientRect(e, h, p):
    e.write(p, struct.pack("<4i", 0, 0, 1024, 768))
    return 1


@api("GetWindowRect", 2)
def GetWindowRect(e, h, p):
    e.write(p, struct.pack("<4i", 0, 0, 1024, 768))
    return 1


@api("CreatePopupMenu", 0)
def CreatePopupMenu(e):
    return e.api.new_handle(("menu",))


@api("CreateMenu", 0)
def CreateMenu(e):
    return e.api.new_handle(("menu",))


# ---------------------------------------------------------------- exceptions
@api("RaiseException", 4)
def RaiseException(e, code, flags, nargs, pargs):
    args = [e.u32(pargs + 4 * i) for i in range(nargs)] if pargs else []
    msg = f"RaiseException(code={code:#x}, args={[hex(a) for a in args]})"
    if code == 0x0EEDFADE and len(args) >= 2:   # Delphi exception: [ExceptAddr, ExceptObject]
        obj = args[1]
        vmt = e.u32(obj)
        cls = e.read(e.u32(vmt - 44) + 1, e.u8(e.u32(vmt - 44))).decode("latin1")
        m = e.u32(obj + 4)
        text = e.read(m, e.u32(m - 4)).decode("latin1") if m else ""
        msg = f"Delphi exception {cls}: {text!r} raised at {args[0]:#x}"
    raise EmuStop(msg)
