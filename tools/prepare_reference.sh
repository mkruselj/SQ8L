#!/bin/sh
# Prepare the reference material used by the differential tests and the porting docs.
#
# Requires the ORIGINAL plugin (not distributed with this repository):
#   original/SQ8L.dll  - SQ8L v0.91b, SHA-256 ffbb70884a6cd2af9600d287deebb2ba8dc6ec2270150ceef04c23d0fc92ab0e
#   original/SQ8L.ini  - from the SQ8L folder of the same archive
# and a Python environment with requirements.txt installed. Ghidra (optional) adds the
# per-unit decompilation in re/decomp/.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}

test -f original/SQ8L.dll || { echo "missing original/SQ8L.dll (see README)"; exit 1; }
echo "ffbb70884a6cd2af9600d287deebb2ba8dc6ec2270150ceef04c23d0fc92ab0e  original/SQ8L.dll" | shasum -a 256 -c -

$PY re/scripts/delphi_vmt.py       # classes.json
$PY re/scripts/inittable.py        # inittable.txt
$PY re/scripts/make_names.py       # names.txt, ranges.txt (for Ghidra)
$PY re/scripts/dfm.py              # decoded forms + sprites (GUI oracle driver)

if command -v analyzeHeadless >/dev/null 2>&1 || [ -n "$GHIDRA_HOME" ]; then
    AH=${GHIDRA_HOME:+$GHIDRA_HOME/support/}analyzeHeadless
    mkdir -p re/ghidra
    "$AH" re/ghidra SQ8L -import original/SQ8L.dll -overwrite -processor x86:LE:32:default -cspec borlanddelphi \
        -scriptPath re/scripts -postScript ExportUnits.java "$PWD/re/extracted/names.txt" \
        "$PWD/re/extracted/ranges.txt" "$PWD/re/decomp"
else
    echo "Ghidra not found: skipping re/decomp (set GHIDRA_HOME to enable)"
fi
echo "done"
