# Patched FFmpeg with `sffwembedsafe` filter

Static x86_64 Linux build of FFmpeg with the castLabs Stardust safe
watermark embedder filter compiled in. Produced by
`scripts/build_patched_ffmpeg.sh`.

## Build metadata

- FFmpeg upstream version: 7.1.1
- Stardust source commit:   `f5b8a74bbb8bb4e165d12b1cb418a122697bc18a`
  (`ffmpeg_1.2.4-14-gf5b8a74`)
- Toolchain:                gcc 13 (Ubuntu)
- libx264:                  2:0.164.3108+git31e19f9-1
- Linkage:                  fully static (`libsffwembedsafe.a`, `libx264.a`,
                            `-static-libgcc -static-libstdc++`)
- Target:                   x86_64-linux-gnu

## Enabled features

Narrow pragmatic profile:

- decoders:   h264, hevc, aac, mjpeg, png, rawvideo
- encoders:   libx264, aac, mjpeg, png, rawvideo
- demuxers:   mov, matroska, image2, mjpeg, rawvideo, image_jpeg_pipe,
              image_png_pipe
- muxers:     mov, mp4, image2, mjpeg, rawvideo
- filters:    sffwembedsafe, scale, format, null, copy, fps, aformat, anull
- parsers:    h264, aac, mjpeg, png
- protocols:  file, pipe

Rebuild via:

```bash
./scripts/build_patched_ffmpeg.sh
```
