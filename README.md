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

### Notes

The embed pipeline runs as a single FFmpeg invocation:

1. generate the Stardust `.pp` payload via `sffw-embed --payload-file`
2. decode → `sffwembedsafe` filter → encode in one ffmpeg pass
3. simplified C2PA manifest signing via `stardustproof-c2pa-signer`
4. write detached manifest to `<manifest-store>/<wm-payload-hex>.c2pa`

No reference YUV or metadata sidecars are produced.

## Verify

```bash
stardustproof verify \
  --input signed.jpg \
  --manifest-store ./manifest-store
```

The verify command:

1. blind-extracts the Stardust watermark id from the asset,
2. looks up the matching `.c2pa` manifest in the directory store,
3. invokes the bundled `c2patool` to validate the detached manifest
   against the asset,
4. asserts that the manifest's `c2pa.soft-binding` (alg
   `castlabs.stardust`) value equals the extracted watermark id,
5. asserts that `c2patool` reports zero validation failures.

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
