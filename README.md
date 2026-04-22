# stardustproof-cli

CLI for Stardust watermarking and simplified C2PA signing.

## Scope

Current first version supports:

- watermarking images with a caller-provided payload
- configurable watermark payload length
- org-only signing via `org:{uuid}` through the keystore bearer flow
- simplified C2PA manifest generation via `generate_and_embed_manifest_simple()`
- writing detached manifests into a local directory store keyed by watermark id

Out of scope for now:

- ICA support
- user signing flow
- ledger integration
- IPFS integration
- video support

## Installation

```bash
pip install -e ".[dev]"
```

The CLI expects Stardust binaries in `./bin/` by default:

- `bin/sffw-embed`
- `bin/extract`
- `bin/align`

You can also point to another directory with `--bin-dir`.

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
