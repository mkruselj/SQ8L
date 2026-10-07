"""Delphi binary form (TPF0) parser: converts the RCDATA form resources of SQ8L.dll
to text and extracts embedded pictures.

Usage: dfm.py  -> writes re/extracted/forms/<FORM>.txt and images to re/extracted/forms/img/
"""
import os
import struct
import sys

import pefile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "re", "extracted", "forms")

VA_NULL, VA_LIST, VA_INT8, VA_INT16, VA_INT32, VA_EXTENDED, VA_STRING, VA_IDENT, VA_FALSE, VA_TRUE, \
    VA_BINARY, VA_SET, VA_LSTRING, VA_NIL, VA_COLLECTION, VA_SINGLE, VA_CURRENCY, VA_DATE, VA_WSTRING, \
    VA_INT64, VA_UTF8STRING = range(21)


class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def unpack(self, fmt):
        v = struct.unpack_from(fmt, self.d, self.p)
        self.p += struct.calcsize(fmt)
        return v[0]

    def sstr(self):
        n = self.u8()
        s = self.d[self.p:self.p + n].decode("latin1")
        self.p += n
        return s

    def value(self, t=None):
        if t is None:
            t = self.u8()
        if t == VA_LIST:
            items = []
            while self.d[self.p] != VA_NULL:
                items.append(self.value())
            self.p += 1
            return ("list", items)
        if t == VA_INT8:
            return self.unpack("<b")
        if t == VA_INT16:
            return self.unpack("<h")
        if t == VA_INT32:
            return self.unpack("<i")
        if t == VA_EXTENDED:
            b = self.d[self.p:self.p + 10]
            self.p += 10
            m, se = struct.unpack("<QH", b)
            e = se & 0x7FFF
            v = 0.0 if e == 0 else m / (1 << 63) * 2.0 ** (e - 16383)
            return -v if se & 0x8000 else v
        if t in (VA_STRING,):
            return ("str", self.sstr())
        if t == VA_LSTRING:
            n = self.unpack("<I")
            s = self.d[self.p:self.p + n].decode("latin1")
            self.p += n
            return ("str", s)
        if t == VA_WSTRING:
            n = self.unpack("<I")
            s = self.d[self.p:self.p + 2 * n].decode("utf-16-le")
            self.p += 2 * n
            return ("str", s)
        if t == VA_IDENT:
            return ("ident", self.sstr())
        if t == VA_FALSE:
            return False
        if t == VA_TRUE:
            return True
        if t == VA_NIL:
            return None
        if t == VA_BINARY:
            n = self.unpack("<I")
            b = self.d[self.p:self.p + n]
            self.p += n
            return ("binary", b)
        if t == VA_SET:
            items = []
            while True:
                s = self.sstr()
                if not s:
                    break
                items.append(s)
            return ("set", items)
        if t == VA_COLLECTION:
            items = []
            while self.d[self.p] != VA_NULL:
                if self.d[self.p] in (VA_INT8, VA_INT16, VA_INT32):
                    self.value()
                assert self.u8() == VA_LIST
                props = []
                while self.d[self.p] != VA_NULL:
                    name = self.sstr()
                    props.append((name, self.value()))
                self.p += 1
                items.append(props)
            self.p += 1
            return ("collection", items)
        if t == VA_SINGLE:
            return self.unpack("<f")
        if t == VA_INT64:
            return self.unpack("<q")
        if t == VA_NULL:
            return None
        raise ValueError(f"unknown value type {t} at {self.p - 1:#x}")

    def obj(self):
        flags = 0
        if self.d[self.p] & 0xF0 == 0xF0:
            flags = self.u8() & 0x0F
            if flags & 2:
                self.value()
        cls = self.sstr()
        name = self.sstr()
        props = []
        while self.d[self.p] != VA_NULL:
            pname = self.sstr()
            props.append((pname, self.value()))
        self.p += 1
        children = []
        while self.d[self.p] != VA_NULL:
            children.append(self.obj())
        self.p += 1
        return {"class": cls, "name": name, "props": props, "children": children, "flags": flags}


def parse_form(data):
    assert data[:4] == b"TPF0", data[:4]
    r = Reader(data)
    r.p = 4
    return r.obj()


def fmt_value(v, img_cb, path):
    if isinstance(v, tuple):
        kind, x = v
        if kind == "str":
            return repr(x)
        if kind == "ident":
            return x
        if kind == "set":
            return "[" + ", ".join(x) + "]"
        if kind == "binary":
            return img_cb(path, x)
        if kind == "list":
            return "(" + " ".join(fmt_value(i, img_cb, path) for i in x) + ")"
        if kind == "collection":
            return "<" + " | ".join(", ".join(f"{n}={fmt_value(pv, img_cb, path)}" for n, pv in item) for item in x) + ">"
    return repr(v)


def dump(o, img_cb, indent=0, out=None):
    pad = "  " * indent
    out.append(f"{pad}object {o['name']}: {o['class']}")
    for n, v in o["props"]:
        out.append(f"{pad}  {n} = {fmt_value(v, img_cb, o['name'] + '.' + n)}")
    for c in o["children"]:
        dump(c, img_cb, indent + 1, out)
    out.append(f"{pad}end")


def main():
    pe = pefile.PE(os.path.join(ROOT, "original", "SQ8L.dll"))
    os.makedirs(os.path.join(OUT, "img"), exist_ok=True)
    for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if t.id != 10:  # RT_RCDATA
            continue
        for e in t.directory.entries:
            name = str(e.name) if e.name is not None else str(e.id)
            ent = e.directory.entries[0]
            data = pe.get_data(ent.data.struct.OffsetToData, ent.data.struct.Size)
            if not data.startswith(b"TPF0"):
                continue
            form = parse_form(data)

            def img_cb(path, blob, form_name=name):
                # Picture.Data / Bitmap blobs: TPicture data starts with a class name sstring.
                if b"GIF8" in blob[:8]:
                    blob = blob[blob.index(b"GIF8"):]
                fn = "".join(c if c.isalnum() or c == "_" else "_" for c in path.replace(".", "_"))
                if blob.startswith(b"GIF8"):
                    payload, ext, cls = blob, "gif", "GIF"
                elif blob[:1] and 0 < blob[0] < 32 and blob[1:1 + blob[0]].isascii():
                    cls = blob[1:1 + blob[0]].decode()
                    payload = blob[1 + blob[0]:]
                    if cls == "TJPEGImage":
                        payload = payload[4:]  # size prefix
                        ext = "jpg"
                    elif cls == "TBitmap":
                        payload = payload[4:]
                        ext = "bmp"
                    else:
                        ext = cls.lower()
                else:
                    payload, ext, cls = blob, "bin", "raw"
                    if blob[4:6] == b"BM":
                        payload, ext = blob[4:], "bmp"
                ext = "".join(c for c in ext if c.isalnum()) or "bin"
                fname = f"{form_name}_{fn}.{ext}"
                with open(os.path.join(OUT, "img", fname), "wb") as f:
                    f.write(payload)
                return f"<{cls} {len(blob)} bytes -> img/{fname}>"

            lines = []
            dump(form, img_cb, 0, lines)
            with open(os.path.join(OUT, f"{name}.txt"), "w") as f:
                f.write("\n".join(lines) + "\n")
            print(f"{name}: {len(lines)} lines")


if __name__ == "__main__":
    main()
