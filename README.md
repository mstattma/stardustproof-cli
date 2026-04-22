# stardustproof-cli

CLI for Stardust watermarking and simplified C2PA signing.

## Scope

Current first version supports:

- watermarking images with a caller-provided payload
- watermarking video containers through the same rawvideo embed path
- configurable watermark payload length
- org-only signing via `org:{uuid}` through the keystore bearer flow
- simplified C2PA manifest generation via `generate_and_embed_manifest_simple()`
- writing detached manifests into a local directory store keyed by watermark id

Out of scope for now:

- ICA support
- user signing flow
- ledger integration
- IPFS integration

## Installation

```bash
pip install -e ".[dev]"
```

The CLI looks for Stardust binaries in this order:

1. `--bin-dir`
2. `STARDUSTPROOF_BIN_DIR`
3. `./bin/`
4. `./stardust_prebuilt_x64_avx2/`

Populate one of those directories with:

- `bin/sffw-embed`
- `bin/extract`
- `bin/align`

Typical bootstrap options:

```bash
# Option 1: copy prebuilt binaries into this repo
mkdir -p bin
cp /path/to/sffw-embed /path/to/extract /path/to/align bin/

# Option 2: point at an external binary directory
export STARDUSTPROOF_BIN_DIR=/path/to/stardust_prebuilt_x64_avx2
```

System tools required:

- `ffmpeg`
- `ffprobe`

For image signing, install the signer dependencies as well:

```bash
pip install -e ".[dev]"
```

## Usage

```bash
stardustproof sign \
  --input input.jpg \
  --output signed.png \
  --wm-payload-hex 00112233445566778899aabbccddeeff001122 \
  --wm-bit-profile 160 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token>
```

This will:

1. watermark the input image with the provided payload
2. embed a simplified C2PA manifest into the watermarked output image
3. write the detached manifest to `./manifest-store/<wm-payload-hex>.c2pa`

## Video Usage

```bash
stardustproof sign \
  --input clip.mp4 \
  --output clip_signed.mp4 \
  --wm-payload-hex 00112233445566778899aabbccddeeff001122 \
  --wm-bit-profile 160 \
  --manifest-store ./manifest-store \
  --org-uuid <org-uuid> \
  --keystore-url http://localhost:2001 \
  --signing-access-token <token>
```

Current video support uses the same rawvideo Stardust embed path and then signs
the final output with the simplified manifest workflow. Thumbnail generation is
automatically skipped for non-image media.

## Integration Smoke Test

An opt-in smoke test exercises the real keystore + signer path:

```bash
export STARDUSTPROOF_TEST_KEYSTORE_URL=http://localhost:2001
export STARDUSTPROOF_TEST_ORG_UUID=<org-uuid>
export STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN=<token>
export STARDUSTPROOF_TEST_BIN_DIR=/path/to/stardust/bin
PYTHONPATH=src pytest tests/test_integration_smoke.py -m integration -q
```
