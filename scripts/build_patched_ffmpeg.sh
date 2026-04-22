#!/bin/bash
#
# Build a patched FFmpeg with the `sffwembedsafe` filter, statically linked.
#
# Produces two artifacts, fully self-contained and statically linked against a
# narrow pragmatic feature profile:
#
#     stardustproof-cli/bin/ffmpeg/bin/ffmpeg
#     stardustproof-cli/bin/ffmpeg/bin/ffprobe
#
# Prerequisites (Debian/Ubuntu):
#     sudo apt-get install -y build-essential nasm yasm pkg-config \
#         libx264-dev patch cmake
#
# The script expects the Stardust source tree to be available.  Set
# STARDUST_SRC to the submodule path, or let the script use the default:
#
#     ${STARDUST_SRC:=../consumer-sdproof-candidate/stardust}
#
# The script builds a static `libsffwembedsafe_static.a` from the SAFE object
# sources exposed by the CMake build, then feeds that archive into the FFmpeg
# link.  The filter sources and patches are copied directly from the Stardust
# source tree.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STARDUST_SRC="${STARDUST_SRC:-$REPO_ROOT/../consumer-sdproof-candidate/stardust}"
FFMPEG_VERSION="${FFMPEG_VERSION:-7.1.1}"
BUILD_ROOT="${BUILD_ROOT:-$REPO_ROOT/build/ffmpeg}"
OUT_DIR="$REPO_ROOT/bin/ffmpeg/bin"

if [[ ! -d "$STARDUST_SRC" ]]; then
    echo "Stardust source not found at $STARDUST_SRC. Set STARDUST_SRC to the stardust submodule path." >&2
    exit 1
fi

echo "[build] Stardust source: $STARDUST_SRC"
echo "[build] FFmpeg version:  $FFMPEG_VERSION"
echo "[build] Build root:      $BUILD_ROOT"
echo "[build] Output dir:      $OUT_DIR"

mkdir -p "$BUILD_ROOT" "$OUT_DIR"

#
# Step 1: build Stardust embed libraries so we can harvest SAFE objects
#
echo "[build] Configuring Stardust build..."
STARDUST_BUILD="$BUILD_ROOT/stardust"
cmake -S "$STARDUST_SRC" -B "$STARDUST_BUILD" \
    -DSD_STATIC_BINARIES=OFF \
    -DSD_BUILD_TESTS=OFF \
    -DSD_BUILD_ALIGN=OFF \
    -DSD_BUILD_EXTRACT=OFF \
    -DSD_WITH_OPENCV=OFF \
    -DSD_BUILD_OPENCV=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON >/dev/null
cmake --build "$STARDUST_BUILD" -j"$(nproc)" --target sffw-embed-objects-safe

SAFE_OBJ_DIR="$STARDUST_BUILD/embed/CMakeFiles/sffw-embed-objects-safe.dir"
if [[ ! -d "$SAFE_OBJ_DIR" ]]; then
    echo "[build] Safe objects not found at $SAFE_OBJ_DIR" >&2
    exit 1
fi

#
# Step 2: assemble a static libsffwembedsafe_static.a from the SAFE objects
#
STATIC_DIR="$BUILD_ROOT/sffwembedsafe_static"
rm -rf "$STATIC_DIR"
mkdir -p "$STATIC_DIR"
STATIC_LIB="$STATIC_DIR/libsffwembedsafe.a"
echo "[build] Collecting SAFE object files..."
find "$SAFE_OBJ_DIR" -name '*.o' | xargs ar crs "$STATIC_LIB"
ranlib "$STATIC_LIB" || true
ls -la "$STATIC_LIB"

#
# Step 3: fetch FFmpeg source and apply filter patches
#
FFMPEG_TARBALL="$BUILD_ROOT/ffmpeg-$FFMPEG_VERSION.tar.bz2"
FFMPEG_SRC="$BUILD_ROOT/ffmpeg-$FFMPEG_VERSION"
if [[ ! -f "$FFMPEG_TARBALL" ]]; then
    echo "[build] Downloading FFmpeg $FFMPEG_VERSION..."
    curl -L "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.bz2" -o "$FFMPEG_TARBALL"
fi

rm -rf "$FFMPEG_SRC"
tar xjf "$FFMPEG_TARBALL" -C "$BUILD_ROOT"

echo "[build] Installing Stardust filter sources into FFmpeg..."
cp "$STARDUST_SRC/embed/vf_stardust.c" "$FFMPEG_SRC/libavfilter/"
cp -R "$STARDUST_SRC/embed/include/"* "$FFMPEG_SRC/libavfilter/"
cp "$STARDUST_SRC/embed/constants.h" "$FFMPEG_SRC/libavfilter/stardust_constants.h"

echo "[build] Patching FFmpeg filter registry..."
patch "$FFMPEG_SRC/libavfilter/allfilters.c" < "$STARDUST_SRC/ffmpeg_filter/allfilters.changes"
patch "$FFMPEG_SRC/libavfilter/Makefile" < "$STARDUST_SRC/ffmpeg_filter/Makefile.changes"

#
# Step 4: configure and build FFmpeg with a narrow pragmatic profile
#
echo "[build] Configuring FFmpeg..."
cd "$FFMPEG_SRC"

EXTRA_LDFLAGS="-L$STATIC_DIR -static-libgcc -static-libstdc++"
EXTRA_LIBS="-l:libsffwembedsafe.a -l:libx264.a -lpthread -lm -ldl"

./configure \
    --prefix="$BUILD_ROOT/install" \
    --pkg-config-flags="--static" \
    --extra-cflags="-static" \
    --extra-ldflags="$EXTRA_LDFLAGS" \
    --extra-ldexeflags="-static" \
    --extra-libs="$EXTRA_LIBS" \
    --ld="g++" \
    --disable-shared \
    --enable-static \
    --enable-gpl \
    --disable-doc \
    --disable-debug \
    --disable-ffplay \
    --disable-network \
    --disable-everything \
    --enable-filter=stardust \
    --enable-filter=scale \
    --enable-filter=format \
    --enable-filter=null \
    --enable-filter=aformat \
    --enable-filter=anull \
    --enable-filter=copy \
    --enable-filter=fps \
    --enable-decoder=h264 \
    --enable-decoder=hevc \
    --enable-decoder=aac \
    --enable-decoder=mjpeg \
    --enable-decoder=png \
    --enable-decoder=rawvideo \
    --enable-encoder=libx264 \
    --enable-encoder=aac \
    --enable-encoder=mjpeg \
    --enable-encoder=png \
    --enable-encoder=rawvideo \
    --enable-demuxer=mov \
    --enable-demuxer=matroska \
    --enable-demuxer=image2 \
    --enable-demuxer=mjpeg \
    --enable-demuxer=rawvideo \
    --enable-demuxer=image_jpeg_pipe \
    --enable-demuxer=image_png_pipe \
    --enable-muxer=mov \
    --enable-muxer=mp4 \
    --enable-muxer=image2 \
    --enable-muxer=mjpeg \
    --enable-muxer=rawvideo \
    --enable-parser=h264 \
    --enable-parser=aac \
    --enable-parser=mjpeg \
    --enable-parser=png \
    --enable-protocol=file \
    --enable-protocol=pipe \
    --enable-libx264 \
    --enable-pic

echo "[build] Compiling FFmpeg..."
make -j"$(nproc)"

echo "[build] Installing ffmpeg/ffprobe to $OUT_DIR..."
install -m 755 ffmpeg "$OUT_DIR/ffmpeg"
install -m 755 ffprobe "$OUT_DIR/ffprobe"
strip "$OUT_DIR/ffmpeg" "$OUT_DIR/ffprobe"

echo "[build] Done."
"$OUT_DIR/ffmpeg" -version | head -3
"$OUT_DIR/ffmpeg" -hide_banner -filters 2>/dev/null | rg sffwembedsafe || {
    echo "[build] WARNING: sffwembedsafe filter not visible in filter list" >&2
    exit 1
}
