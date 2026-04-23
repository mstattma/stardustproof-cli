# stardustproof-cli

CLI for Stardust watermarking and simplified C2PA signing.

## Scope

Current version supports:

- single-pipeline watermarking of images and video via a patched FFmpeg
  with the castLabs `sffwembedsafe` filter
- blind extraction as the only verification model (no reference sidecar
  files are produced)
- configurable watermark payload length (default 48 bits)
- org-only signing via `org:{uuid}` through the keystore bearer flow
- simplified C2PA manifest generation via
  `generate_and_embed_manifest_simple()`
- detached manifests written into a local directory store keyed by
  watermark id

Out of scope for now:

- ICA support
- user signing flow
- ledger integration
- IPFS integration

## Repo layout

```
bin/
  stardust/            # Stardust tools (sffw-embed, extract, align)
  ffmpeg/
    bin/
      ffmpeg           # patched static FFmpeg with sffwembedsafe filter
      ffprobe          # static FFprobe from same build
    VERSION.md         # build metadata
```

All binaries are committed directly in the repo as **x86_64 Linux**
statically linked artifacts.  Other platforms are not supported out of the
box; use `scripts/build_patched_ffmpeg.sh` to rebuild for your target.

## Installation

```bash
pip install -e ".[dev]"
```

The CLI enforces a strict binary layout.  It looks for the `bin/` root in
this order:

1. `--bin-dir`
2. `STARDUSTPROOF_BIN_DIR`
3. `./bin/`

All four binaries are required:

- `bin/stardust/sffw-embed`
- `bin/stardust/extract`
- `bin/ffmpeg/bin/ffmpeg`
- `bin/ffmpeg/bin/ffprobe`

There is no system `ffmpeg`/`ffprobe` fallback — the bundled patched
builds are used exclusively.

## Rebuilding FFmpeg (fallback)

```bash
# Debian/Ubuntu prerequisites:
sudo apt-get install -y build-essential nasm yasm pkg-config \
    libx264-dev patch cmake curl

# Point STARDUST_SRC at the stardust source tree, then:
./scripts/build_patched_ffmpeg.sh
```

The script builds a static `libsffwembedsafe.a` from the Stardust SAFE
objects, fetches FFmpeg upstream sources, applies the filter patches, and
produces `bin/ffmpeg/bin/ffmpeg` and `bin/ffmpeg/bin/ffprobe`.

## Usage

### Image

```bash
stardustproof sign \
  --input input.jpg \
  --output signed.png \
  --wm-payload-hex 001122334455 \
  --wm-bit-profile 48 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token>
```

### Video

```bash
stardustproof sign \
  --input clip.mp4 \
  --output signed.mp4 \
  --wm-payload-hex 001122334455 \
  --wm-bit-profile 48 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token> \
  --video-preset veryfast \
  --video-crf 18
```

The default video encode is `libx264 -preset veryfast -crf 18`.  Presets
faster than `veryfast` (notably `ultrafast`) break blind extraction.

### Fragmented MP4

`sign` accepts a **single-file fragmented MP4** as input (BMFF file with
`moov` plus one or more `moof`/`mdat` pairs, e.g. CMAF non-segmented).
The output is a single-file fragmented MP4 whose fragment boundaries are
preserved by passing `-force_key_frames` to the underlying ffmpeg encode
derived from the input's per-`moof` schedule.

```bash
stardustproof sign \
  --input clip-fragmented.mp4 \
  --output signed-fragmented.mp4 \
  --wm-payload-hex 001122334455 \
  --wm-bit-profile 48 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token>
```

### Segmented fragmented-MP4

`sign` also accepts a **segmented fragmented-MP4 directory**: a
directory containing exactly one init segment (BMFF file with `moov`
but no `moof`) plus one or more media fragments (BMFF files with
`moof` but no `moov`). The shape is auto-detected structurally; no
filename conventions are required.

```bash
stardustproof sign \
  --input ./dash-input/ \
  --output ./dash-signed/ \
  --wm-payload-hex 001122334455 \
  --wm-bit-profile 48 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token>
```

Output is a flat directory containing the watermarked, signed
`init.m4s` + `seg-NNNN.m4s`. The detached manifest goes into the
store as usual, keyed by the watermark id.

| Flag | Purpose |
|---|---|
| `--in-place` | Atomically replace the input directory contents with the signed output. `--output` must equal `--input` (or be omitted, in which case it defaults to `--input`). Interrupted signs leave the input tree in a partially-replaced state; prefer a separate `--output` for production. |
| `--force` | For Segmented inputs with a non-empty `--output` directory, overwrite existing files. |

### Video thumbnails (animated WebP)

Video manifests carry an animated WebP `c2pa.thumbnail.claim` assertion
by default: a 5-frame, 848 px longest-edge, 500 ms-per-frame loop
assembled from the most "meaningful" candidate frames sampled across
the first 60 seconds of the asset. Alongside it the signer emits a
`castlabs.video.preview.anim` assertion with per-frame source
timestamps so consumers can cross-reference the preview against the
underlying content.

All seven generation knobs are env-var configurable:

| Var | Default |
|---|---|
| `STARDUSTPROOF_VIDEO_THUMBNAIL_FRAMES` | 5 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_LONGEST_EDGE` | 848 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_FRAME_DURATION_MS` | 500 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_MAX_BYTES` | 256000 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_CANDIDATES` | 20 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_SKIP_SECONDS` | 1 |
| `STARDUSTPROOF_VIDEO_THUMBNAIL_MAX_SPAN_SECONDS` | 60 |

See the [signer README](../stardustproof-c2pa-signer-vibe/README.md#thumbnails)
for the full schema and algorithm details. Video thumbnail
generation failures (missing ffmpeg, too-few candidate frames,
oversized WebP, etc.) abort signing with a `RuntimeError`.

### Notes

The sign pipeline runs in three phases:

1. **Watermark** — generate the Stardust `.pp` payload via
   `sffw-embed --payload-file`, then decode → `sffwembedsafe` filter →
   encode in a single ffmpeg pass.
2. **C2PA sign** — `generate_and_embed_manifest_simple` (in
   `stardustproof-c2pa-signer`) classifies the watermarked output
   and embeds a signed C2PA manifest into the asset in-place (JUMBF
   box in the image / MP4 container).
3. **Sidecar write** — the same manifest bytes are also written to
   `<manifest-store>/<wm-payload-hex>.c2pa` as a watermark-id-keyed
   on-disk copy.

The asset therefore carries the manifest in two places:

- **Embedded** in the asset itself (discoverable by any standard C2PA
  verifier without our manifest store).
- **On disk in the store** (discoverable by any party that blind-extracts
  the watermark from a stripped-or-modified asset, since the store is
  keyed by watermark id).

`verify` uses `c2patool --external-manifest <store-entry>` so it
validates against the on-disk sidecar rather than the embedded copy.
This keeps verify semantically "watermark-first": anything that survives
the watermark round-trip can be re-verified against the store.

No reference YUV or metadata sidecars are produced by the watermark
step.

## Verify

```bash
stardustproof verify \
  --input signed.jpg \
  --manifest-store ./manifest-store
```

`--input` accepts three media-input shapes, auto-detected by walking
the top-level BMFF box structure:

| Shape | Example | Handling |
|---|---|---|
| Ordinary file | `.jpg`, `.mov`, non-fragmented `.mp4` | Passed as-is to blind-extract and `c2patool --external-manifest <c2pa>`. |
| Single-file fragmented MP4 | fragmented `.mp4` / `.cmfv` (has both `moov` and `moof` boxes) | Same code path as ordinary file; c2pa-rs handles fragmented-MP4 hashing internally via `verify_stream_hash`. |
| Segmented fMP4 directory | Directory with one init segment (BMFF with `moov` and no `moof`) plus media fragments (BMFF with `moof` and no `moov`) | Blind-extract runs ffmpeg with init + fragment[0] piped to stdin; `c2patool` is invoked with `fragment --fragments_glob <derived>` and the init as the positional asset. Classification is structural — filename conventions like `init.m4s` / `seg_*.m4s` are not required. |

The verify command:

1. classifies the input into one of the three shapes above,
2. blind-extracts the Stardust watermark id from the asset,
3. looks up the matching `.c2pa` manifest in the directory store,
4. invokes the bundled `c2patool` to validate the detached manifest
   against the asset,
5. asserts that the manifest's `c2pa.soft-binding` (alg
   `castlabs.stardust`) value equals the extracted watermark id,
6. asserts that `c2patool` reports zero validation failures.

By default, trust anchors are auto-discovered from the signer package's
`keystore/certs/{castlabs_c2pa_ca,trusted_publisher_ca}.cert.pem`. Pass
`--trust-anchors <pem>` (repeatable) to override. Pass `--no-trust` to
skip certificate-trust checks entirely (structure, hashes and
signature math are still validated).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Verified OK |
| 1 | Argument / IO / binary / trust-material error |
| 2 | Blind extraction found no Stardust watermark |
| 3 | Manifest not found in store for extracted WM id |
| 4 | `c2patool` invocation failed (non-zero exit or unparseable JSON) |
| 5 | Soft-binding value does not match extracted WM id |
| 6 | `c2patool` reported one or more validation failures |

Add `--json` to emit a single-line JSON object on stdout instead of the
human-readable report (useful for CI scripts).

## Test fixtures

- `tests/fixtures/sample-photo.jpg` — 1920x1080 natural photo
- `tests/fixtures/big-buck-bunny-trailer-1080p.mov` — Blender Foundation
  _Big Buck Bunny_ trailer (CC BY 3.0).

## Integration Smoke Test

The integration smoke test exercises the real keystore + signer path,
produces a signed asset, and then runs the productized
`verify.verify_asset()` (the same code path `stardustproof verify`
exposes) over the signed asset + detached manifest. This guarantees
zero drift between the smoke and the CLI verify behavior:

```bash
export STARDUSTPROOF_TEST_KEYSTORE_URL=http://localhost:2001
export STARDUSTPROOF_TEST_ORG_UUID=<org-uuid>
export STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN=<token>
export STARDUSTPROOF_TEST_BIN_DIR=$PWD/bin
PYTHONPATH=src pytest tests/test_integration_smoke.py -m integration -s -vv
```

For a one-command local run:

```bash
# one-time setup
cp .env.smoke.example .env.smoke.local
# edit .env.smoke.local and set:
#   STARDUSTPROOF_TEST_ORG_UUID
#   STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN

./scripts/run_smoke.sh
```

Defaults in the helper script:

- `STARDUSTPROOF_TEST_KEYSTORE_URL=http://localhost:2001`
- `STARDUSTPROOF_TEST_BIN_DIR=$PWD/bin`

Env files:

- `.env.smoke.example` is tracked and documents the required variables
- `.env.smoke.local` is ignored and can hold your local org UUID and access token
