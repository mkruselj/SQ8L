<!-- Notes for this version: add what changed here before publishing the draft. -->

**Unofficial 64-bit port of SQ8L 0.91b**, Siegfried Kullmann's Ensoniq SQ-80 emulation, for
macOS (universal: Apple Silicon and Intel, 10.15 or later), Windows x64 (10/11) and Linux
(x86-64 and ARM64), as VST2, VST3, Audio Unit (macOS), CLAP and LV2 (Linux).

The sound is bit-identical to the original plug-in: this build was checked against the
original's output on Apple Silicon, on Intel, on Windows and on Linux. Projects, libraries and SysEx
files made with the original load unchanged. See the [README](https://github.com/sq8l-revival/SQ8L#readme) for details,
credits and the known differences.

### macOS

1. Unzip `SQ8L-macOS-universal.zip` and copy the formats you use:
   - `SQ8L.component` (Audio Unit) to `~/Library/Audio/Plug-Ins/Components`
   - `SQ8L.vst3` to `~/Library/Audio/Plug-Ins/VST3`
   - `SQ8L.vst` (VST2) to `~/Library/Audio/Plug-Ins/VST`
   - `SQ8L.clap` to `~/Library/Audio/Plug-Ins/CLAP`
2. The plug-ins are not notarized by Apple, so macOS blocks them when they come from the
   internet. Open Terminal and run (an error for a format you did not install is harmless):
   ```
   xattr -dr com.apple.quarantine ~/Library/Audio/Plug-Ins/Components/SQ8L.component ~/Library/Audio/Plug-Ins/VST3/SQ8L.vst3 ~/Library/Audio/Plug-Ins/VST/SQ8L.vst ~/Library/Audio/Plug-Ins/CLAP/SQ8L.clap
   ```
3. Restart your DAW and rescan the plug-ins.

### Windows

Unzip `SQ8L-Windows-x64.zip` and copy `SQ8L.dll` (VST2) to your VST plug-in folder,
`SQ8L.vst3` to `C:\Program Files\Common Files\VST3`, `SQ8L.clap` to
`C:\Program Files\Common Files\CLAP`. If the old 32-bit `SQ8L.dll` is in the same folder,
keep a copy of it first: the files have the same name.

### Linux

Unpack `SQ8L-Linux-x64.tar.gz` (or `SQ8L-Linux-arm64.tar.gz` on ARM) and copy `SQ8L.so`
(VST2) to `~/.vst`, `SQ8L.vst3` to `~/.vst3`, `SQ8L.clap` to `~/.clap` and `SQ8L.lv2` to
`~/.lv2`. The editor needs X11 (or XWayland). The Linux version is new: reports are welcome.

### Your sounds

Banks A and B and the options are stored in `~/Library/Application Support/SQ8L` (macOS),
`%APPDATA%\SQ8L` (Windows) or `~/.config/SQ8L` (Linux). To get your old banks back, copy your `SQ8L_backup.dat` there, or
load it with *FILE → Load library*.

Problems or questions: please [open an issue](https://github.com/sq8l-revival/SQ8L/issues).
