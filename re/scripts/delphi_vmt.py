"""Recover Delphi 5 class metadata (VMTs, RTTI, published methods/fields) from SQ8L.dll.

Writes re/extracted/classes.json and prints a summary of non-VCL classes.
"""
import json
import os
import struct
import sys

import pefile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL = os.path.join(ROOT, "original", "SQ8L.dll")

pe = pefile.PE(DLL)
IB = pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()
code = pe.sections[0]
CODE_LO, CODE_HI = IB + code.VirtualAddress, IB + code.VirtualAddress + code.Misc_VirtualSize
IMG_HI = IB + pe.OPTIONAL_HEADER.SizeOfImage


def u8(va): return img[va - IB]
def u16(va): return struct.unpack_from("<H", img, va - IB)[0]
def u32(va): return struct.unpack_from("<I", img, va - IB)[0]
def sstr(va): return img[va - IB + 1: va - IB + 1 + u8(va)].decode("latin1")
def in_img(va): return IB <= va < IMG_HI
def in_code(va): return CODE_LO <= va < CODE_HI


VMT_OFS = dict(SelfPtr=-76, IntfTable=-72, AutoTable=-68, InitTable=-64, TypeInfo=-60, FieldTable=-56,
               MethodTable=-52, DynamicTable=-48, ClassName=-44, InstanceSize=-40, Parent=-36)
STD_VIRTUALS = ["SafeCallException", "AfterConstruction", "BeforeDestruction", "Dispatch",
                "DefaultHandler", "NewInstance", "FreeInstance", "Destroy"]


def find_vmts():
    vmts = []
    for off in range(0, len(img) - 4, 4):
        va = IB + off
        v = struct.unpack_from("<I", img, off)[0]
        if v == va + 76 and in_img(v):
            vmt = v
            try:
                cn = u32(vmt - 44)
                if not in_img(cn) or not (1 <= u8(cn) <= 64):
                    continue
                name = sstr(cn)
                if not name.isidentifier():
                    continue
                vmts.append(vmt)
            except Exception:
                continue
    return vmts


def parse(vmt, all_vmts):
    c = {"vmt": vmt, "name": sstr(u32(vmt - 44)), "instance_size": u32(vmt - 40)}
    p = u32(vmt - 36)
    parent = None
    if p and in_img(p):
        parent = u32(p) if u32(p) in all_vmts else (p if p in all_vmts else None)
    c["parent_vmt"] = parent
    # standard virtuals at negative offsets
    c["std_virtuals"] = {n: u32(vmt - 32 + 4 * i) for i, n in enumerate(STD_VIRTUALS)}
    # user virtuals: scan forward while entries point into code, stop at first table/name start
    stops = {u32(vmt + o) for o in VMT_OFS.values() if o != -76 and o != -40 and o != -36}
    stops |= {x for x in all_vmts if x > vmt}
    stops = {s - 76 if s in all_vmts else s for s in stops}
    virt = []
    a = vmt
    while a not in stops and in_code(u32(a)) and len(virt) < 400:
        virt.append(u32(a))
        a += 4
    c["virtuals"] = virt
    # published methods
    mt = u32(vmt - 52)
    meths = []
    if mt and in_img(mt):
        n = u16(mt)
        e = mt + 2
        for _ in range(n):
            size = u16(e)
            meths.append({"addr": u32(e + 2), "name": sstr(e + 6)})
            e += size
    c["published_methods"] = meths
    # published fields
    ft = u32(vmt - 56)
    fields = []
    if ft and in_img(ft):
        n = u16(ft)
        ctab = u32(ft + 2)
        classes = []
        if ctab and in_img(ctab):
            for i in range(u16(ctab)):
                pp = u32(ctab + 2 + 4 * i)
                classes.append(u32(pp) if in_img(pp) else 0)
        e = ft + 6
        for _ in range(n):
            off, ci = u32(e), u16(e + 4)
            nm = sstr(e + 6)
            cv = classes[ci] if ci < len(classes) else 0
            fields.append({"offset": off, "name": nm, "class_vmt": cv})
            e += 7 + u8(e + 6)
    c["published_fields"] = fields
    # RTTI: unit name
    ti = u32(vmt - 60)
    c["unit"] = None
    if ti and in_img(ti) and u8(ti) == 7:  # tkClass
        td = ti + 2 + u8(ti + 1)
        c["unit"] = sstr(td + 4 + 4 + 2)
        c["typeinfo"] = ti
    # dynamic methods (message handlers / dynamic virtuals)
    dt = u32(vmt - 48)
    dyn = []
    if dt and in_img(dt):
        n = u16(dt)
        idx = [u16(dt + 2 + 2 * i) for i in range(n)]
        addrs = [u32(dt + 2 + 2 * n + 4 * i) for i in range(n)]
        dyn = [{"index": i, "addr": a} for i, a in zip(idx, addrs)]
    c["dynamic"] = dyn
    return c


def main():
    vmts = find_vmts()
    s = set(vmts)
    classes = [parse(v, s) for v in vmts]
    by_vmt = {c["vmt"]: c for c in classes}
    for c in classes:
        c["parent"] = by_vmt[c["parent_vmt"]]["name"] if c["parent_vmt"] in by_vmt else None
    out = os.path.join(ROOT, "re", "extracted", "classes.json")
    with open(out, "w") as f:
        json.dump(classes, f, indent=1)
    print(f"{len(classes)} classes -> {out}")
    vcl_units = {"System", "SysUtils", "Classes", "Graphics", "Controls", "Forms", "StdCtrls", "ExtCtrls",
                 "Menus", "Dialogs", "ComCtrls", "Buttons", "ImgList", "ActnList", "StdActns", "Clipbrd",
                 "IniFiles", "jpeg", "Printers", "FileCtrl", "Contnrs", "SyncObjs", "Mask", "ToolWin",
                 "RichEdit", "TypInfo", "ActiveX", "Consts", "MultiMon", "FlatSB", "CommCtrl"}
    for c in sorted(classes, key=lambda c: c["vmt"]):
        if c["unit"] in vcl_units:
            continue
        print(f"{c['vmt']:#x} {c['name']:28} <- {str(c['parent']):22} unit={c['unit']} size={c['instance_size']} "
              f"virt={len(c['virtuals'])} pub={len(c['published_methods'])} fields={len(c['published_fields'])}")


if __name__ == "__main__":
    main()
