#!/bin/sh
# Package built plug-ins into a release archive.
#   tools/package.sh <build dir> <platform: macos|windows|linux> <output .zip or .tar.gz>
set -e
BUILD=$1; PLATFORM=$2; OUT=$3
case $OUT in /*) ;; *) OUT="$PWD/$OUT" ;; esac
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=$(mktemp -d)/SQ8L
mkdir -p "$STAGE"
if [ "$PLATFORM" = macos ]; then
    cp -R "$BUILD/bin/sq8l.vst" "$STAGE/SQ8L.vst"
    cp -R "$BUILD/bin/sq8l.vst3" "$STAGE/SQ8L.vst3"
    cp -R "$BUILD/bin/sq8l.component" "$STAGE/SQ8L.component"
    cp -R "$BUILD/bin/sq8l.clap" "$STAGE/SQ8L.clap"
    # Ad-hoc signature over the whole bundle (the linker only signs the binary). Not a
    # Developer ID: downloaded copies still need the quarantine flag removed.
    for b in SQ8L.vst SQ8L.vst3 SQ8L.component SQ8L.clap; do
        codesign --force --sign - --timestamp=none "$STAGE/$b"
        codesign --verify --strict "$STAGE/$b"
    done
elif [ "$PLATFORM" = linux ]; then
    cp "$BUILD/bin/sq8l-vst2.so" "$STAGE/SQ8L.so"
    cp -R "$BUILD/bin/sq8l.vst3" "$STAGE/SQ8L.vst3"
    for so in "$STAGE"/SQ8L.vst3/Contents/*-linux/sq8l.so; do mv "$so" "$(dirname "$so")/SQ8L.so"; done
    cp "$BUILD/bin/sq8l.clap" "$STAGE/SQ8L.clap"
    cp -R "$BUILD/bin/sq8l.lv2" "$STAGE/SQ8L.lv2"
    ${STRIP:-strip} "$STAGE/SQ8L.so" "$STAGE"/SQ8L.vst3/Contents/*-linux/SQ8L.so "$STAGE/SQ8L.clap" \
        "$STAGE/SQ8L.lv2/sq8l_dsp.so" "$STAGE/SQ8L.lv2/sq8l_ui.so"
else
    cp "$BUILD/bin/sq8l-vst2.dll" "$STAGE/SQ8L.dll"
    cp -R "$BUILD/bin/sq8l.vst3" "$STAGE/SQ8L.vst3"
    mv "$STAGE/SQ8L.vst3/Contents/x86_64-win/sq8l.vst3" "$STAGE/SQ8L.vst3/Contents/x86_64-win/SQ8L.vst3"
    cp "$BUILD/bin/sq8l.clap" "$STAGE/SQ8L.clap"
    ${STRIP:-x86_64-w64-mingw32-strip} "$STAGE/SQ8L.dll" "$STAGE/SQ8L.vst3/Contents/x86_64-win/SQ8L.vst3" "$STAGE/SQ8L.clap"
fi
cp "$ROOT/README.md" "$ROOT/THIRD_PARTY_NOTICES.md" "$ROOT/LICENSE.md" "$STAGE/"
case $OUT in
    *.tar.gz) (cd "$(dirname "$STAGE")" && tar czf "$OUT" SQ8L) ;;
    *) (cd "$(dirname "$STAGE")" && zip -qr -X "$OUT" SQ8L) ;;
esac
echo "wrote $OUT"
