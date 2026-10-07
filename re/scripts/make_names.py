"""Build Ghidra name/label map and the list of custom-code unit ranges.

Output: re/extracted/names.txt  (addr KIND name), re/extracted/ranges.txt (start end unit)
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EX = os.path.join(ROOT, "re", "extracted")

# Unit code ranges (from the InitTable boundaries + class RTTI), custom code only.
RANGES = [
    (0x41548C, 0x415E50, "DAudioEffect"),
    (0x415E50, 0x417388, "DAudioEffectX"),
    (0x44F2C8, 0x44F6AC, "uMessageBox"),
    (0x44F6AC, 0x44FF14, "midiParser"),
    (0x44FF14, 0x450D74, "unit_44ff14"),
    (0x450DB8, 0x451344, "unit_450db8"),
    (0x452148, 0x45269C, "globalData"),
    (0x45269C, 0x452DB4, "plugConfig"),
    (0x452DB4, 0x453734, "simplePack"),
    (0x4537A4, 0x453A6C, "unit_4537a4"),
    (0x453ADC, 0x454F90, "unit_453adc"),
    (0x454F90, 0x455EB0, "soundLibrary"),
    (0x455F34, 0x456034, "unit_455f34"),
    (0x456EBC, 0x45733C, "AniGIF"),
    (0x45733C, 0x4587D8, "LCD3"),
    (0x4587D8, 0x45B86C, "lcdControl"),
    (0x45B8DC, 0x45B97C, "unit_45b8dc"),
    (0x45B9B4, 0x45CF38, "mod_doc8_5"),
    (0x45CF70, 0x45DBA0, "mod_lfo4_22"),
    (0x45DBA0, 0x45E32C, "mod_env_2"),
    (0x45E364, 0x45E46C, "unit_45e364"),
    (0x45E46C, 0x45EAF0, "mod_amp4_13"),
    (0x45EAF0, 0x45ED38, "mod_foll"),
    (0x45ED38, 0x45EEE8, "mod_filtBase"),
    (0x45EEE8, 0x45F8E8, "mod_filter4_31"),
    (0x45F8E8, 0x4600E0, "paramEditor"),
    (0x4600E0, 0x4602C4, "unit_4600e0"),
    (0x4602C4, 0x461C7C, "editBuffer"),
    (0x461C7C, 0x4648A4, "plugCore"),
    (0x4648A4, 0x4649BC, "unit_4648a4"),
    (0x4782A4, 0x47851C, "unit_4782a4"),
    (0x47851C, 0x478CF0, "midiio2"),
    (0x478CF0, 0x4791E4, "keyCapture"),
    (0x4791E4, 0x479420, "mouseJump"),
    (0x479420, 0x4796EC, "simpleTimer"),
    (0x4796EC, 0x47B3E4, "graphKnobB"),
    (0x47B3E4, 0x47BA3C, "AniDisplay"),
    (0x47BA3C, 0x47C80C, "GraphButton"),
    (0x47C80C, 0x47D100, "uModInfoForm"),
    (0x47D100, 0x47D40C, "uAboutForm"),
    (0x47D40C, 0x47DABC, "uMidiSelForm"),
    (0x47DABC, 0x47E330, "uSelSingleForm"),
    (0x47E330, 0x4826E4, "unit_47e330"),
    (0x4826E4, 0x487374, "plugEdit"),
    (0x487374, 0x488700, "uPlug_SQ8L"),
]


def main():
    classes = json.load(open(os.path.join(EX, "classes.json")))
    by_name = {c["name"]: c for c in classes}
    names = {}

    def put(addr, kind, name):
        if addr and addr not in names:
            names[addr] = (kind, name)

    for c in classes:
        put(c["vmt"], "L", f"vmt_{c['name']}")
        for m in c["published_methods"]:
            put(m["addr"], "F", f"{c['name']}_{m['name']}")
    # virtual methods: name by the class that introduces/overrides them
    for c in sorted(classes, key=lambda c: c["vmt"]):
        parent = by_name.get(c["parent"]) if c["parent"] else None
        pv = parent["virtuals"] if parent else []
        for i, a in enumerate(c["virtuals"]):
            if i < len(pv) and pv[i] == a:
                continue
            put(a, "F", f"{c['name']}_v{i:03d}")
        for n, a in c["std_virtuals"].items():
            pa = parent["std_virtuals"][n] if parent else None
            if a != pa:
                put(a, "F", f"{c['name']}_{n}")
    with open(os.path.join(EX, "names.txt"), "w") as f:
        for a in sorted(names):
            k, n = names[a]
            f.write(f"{a:#x} {k} {n}\n")
    with open(os.path.join(EX, "ranges.txt"), "w") as f:
        for s, e, n in RANGES:
            f.write(f"{s:#x} {e:#x} {n}\n")
    print(len(names), "names,", len(RANGES), "ranges")


if __name__ == "__main__":
    main()
