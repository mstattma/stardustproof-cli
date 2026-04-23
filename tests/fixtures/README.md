# Test Fixtures

## sample-photo.jpg

Copied from the local STARDUSTproof demo sample set for smoke testing.

## big-buck-bunny-trailer-1080p.mov

Source: Blender Foundation `Big Buck Bunny` trailer.

- URL: `https://download.blender.org/peach/trailer/trailer_1080p.mov`
- License: Creative Commons Attribution 3.0
- License page: `https://peach.blender.org/about/`

Attribution for reuse:

`(c) copyright 2008, Blender Foundation / www.bigbuckbunny.org`

## bbb-fragmented-single.mp4

Same source trailer re-muxed as a single-file fragmented MP4
(video-only, `moov` + 9 `moof`+`mdat` pairs, 4-second fragment
duration). Used to exercise the `SingleFileFragmented` media-input
shape in sign + verify.

Regenerate (from the local bundled ffmpeg):

```bash
bin/ffmpeg/bin/ffmpeg -y -i tests/fixtures/big-buck-bunny-trailer-1080p.mov \
  -an -c:v libx264 -preset veryfast -crf 23 -g 96 -keyint_min 96 \
  -sc_threshold 0 \
  -movflags +frag_keyframe+empty_moov+default_base_moof \
  -frag_duration 4000000 \
  tests/fixtures/bbb-fragmented-single.mp4
```

## bbb-segmented/

Same source re-muxed via the DASH muxer into an init segment
(`init.m4s`) plus 8 media segments (`seg-NNNN.m4s`). Video-only,
4-second segments. Used to exercise the `Segmented` media-input
shape.

Regenerate:

```bash
bin/ffmpeg/bin/ffmpeg -y -i tests/fixtures/big-buck-bunny-trailer-1080p.mov \
  -an -c:v libx264 -preset veryfast -crf 23 -g 96 -keyint_min 96 \
  -sc_threshold 0 \
  -f dash -seg_duration 4 -use_template 1 -use_timeline 0 \
  -init_seg_name 'init.m4s' -media_seg_name 'seg-$Number%04d$.m4s' \
  -adaptation_sets 'id=0,streams=v' \
  tests/fixtures/bbb-segmented/manifest.mpd
```
