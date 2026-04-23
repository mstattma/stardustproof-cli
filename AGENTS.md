# AGENTS.md — Coding Agent Instructions

## Project Overview

`stardustproof` CLI: watermarks media with a Stardust steganographic
watermark, signs it with a simplified C2PA manifest, and verifies
signed assets against a local directory manifest store.

Two commands: `sign` and `verify`. Backed by:

- `stardustproof-c2pa-signer-vibe` — the Python library that builds
  + embeds the C2PA manifest, exposes
  `generate_and_embed_manifest_simple(...)`, the media-input
  classifier (`resolve_media_input`), the bundled `c2patool`, and
  the video thumbnail generator.
- A patched `ffmpeg` + `ffprobe` + Stardust binaries committed under
  `bin/`. ffmpeg is built from `consumer-sdproof-candidate/stardust`
  with the `sffwembedsafe` filter plus a narrow feature profile
  (see `scripts/build_patched_ffmpeg.sh` and `bin/ffmpeg/VERSION.md`).

## Scope

| Path | What it is |
|---|---|
| `src/stardustproof_cli/cli.py` | `stardustproof sign` / `stardustproof verify` entrypoints, arg parsing, pipeline wiring. |
| `src/stardustproof_cli/stardust.py` | Watermark embed/extract helpers; wraps the Stardust binaries + patched ffmpeg. Three embed variants: `embed` (SingleFile), `embed_single_file_fragmented`, `embed_segmented`. |
| `src/stardustproof_cli/verify.py` | Verification pipeline: classify input shape, blind-extract, look up manifest in the store, shell out to bundled `c2patool`, assert soft-binding + zero validation failures. Trust anchors resolved from the signer repo's keystore submodule. |
| `src/stardustproof_cli/config.py` | `StardustConfig` + `StardustPaths`: discovers `bin/stardust/` + `bin/ffmpeg/bin/`. |
| `src/stardustproof_cli/manifest_store.py` | `DirectoryManifestStore`: writes `<manifest-store>/<wm-id>.c2pa` sidecar. |
| `bin/` | Prebuilt Linux x86_64 binaries: `stardust/{sffw-embed,extract,align}` + `ffmpeg/bin/{ffmpeg,ffprobe}`. Tracked directly in the repo. |
| `tests/fixtures/` | Committed media fixtures: `sample-photo.jpg`, `big-buck-bunny-trailer-1080p.mov`, `bbb-fragmented-single.mp4`, `bbb-segmented/`. |
| `scripts/build_patched_ffmpeg.sh` | Documented fallback build path for the bundled ffmpeg. |
| `scripts/run_smoke.sh` | One-command smoke runner; sources `.env.smoke.local` + invokes pytest on `tests/test_integration_smoke.py`. |

## Media-input shapes

The classifier lives in the signer repo
(`stardustproof_c2pa_signer.media_input.resolve_media_input`) but
both sign and verify dispatch on its three return types:

- **SingleFile** — images, non-fragmented MP4/MOV. `--output` is a
  single file.
- **SingleFileFragmented** — one file with both `moov` and `moof`
  boxes. `--output` is a single file; output is also a single-file
  fragmented MP4 with `-force_key_frames` derived from the input's
  per-moof schedule.
- **Segmented** — directory containing exactly one init segment
  (BMFF file with `moov` and no `moof`) plus one or more fragments
  (BMFF files with `moof` and no `moov`). `--output` is a
  directory; `--in-place` and `--force` are available.

## Sign pipeline (three phases)

1. **Watermark** — generate Stardust `.pp` payload, then decode →
   `sffwembedsafe` filter → encode in a single ffmpeg pass.
2. **C2PA sign** — signer's
   `generate_and_embed_manifest_simple(input_path, output_path,
   wm_id_bytes, ...)` classifies the input, embeds a signed
   manifest in-place (or redirected via `output_path`), and returns
   the manifest bytes.
3. **Sidecar write** — same manifest bytes go to
   `<manifest-store>/<wm-id>.c2pa` as a watermark-id-keyed on-disk
   copy. verify uses `c2patool --external-manifest` against this
   sidecar.

## Verify pipeline

1. Classify input shape.
2. Blind-extract the Stardust watermark id (ffmpeg decodes the
   first decodable keyframe; for Segmented, init + fragment[0] are
   piped into ffmpeg).
3. Look up `<wm-id>.c2pa` in the manifest store (exit 3 on miss).
4. Invoke `c2patool` with `--settings <toml>` containing
   `[trust]` + `[cawg_trust]` so CAWG publisher-identity cert
   chains are validated. For Segmented inputs, `fragment
   --fragments_glob <derived>` is attached (no `trust` subcommand
   since c2patool only accepts one subcommand at a time).
5. Assert soft-binding `alg == castlabs.stardust` value matches
   the extracted watermark id; assert validation failure list is
   empty; report validation_state.

Exit codes: 0 ok, 1 arg/IO, 2 no watermark, 3 manifest missing,
4 c2patool error, 5 soft-binding mismatch, 6 validation failures.

## Integration smoke test

`tests/test_integration_smoke.py` exercises sign+verify end-to-end
against a real dev keystore for all six paths (image, video,
single-file fragmented, segmented, segmented in-place, segmented
pre-watermark verify).

### Environment

Copy `.env.smoke.example` to `.env.smoke.local` and fill in:

```
STARDUSTPROOF_TEST_KEYSTORE_URL=http://localhost:2001
STARDUSTPROOF_TEST_ORG_UUID=<dev-org-uuid>
STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN=dev-access-token-smoke
STARDUSTPROOF_TEST_BIN_DIR=./bin
```

`.env.smoke.local` is gitignored; `scripts/run_smoke.sh` sources it.

### Dev keystore must be running with the signing token

The smoke signs via the keystore's `authorize_sign/begin` →
`authorize_sign/finish` flow. For `org:{uuid}` key ids the
keystore **requires** `KEYSTORE_DEV_SIGNING_ACCESS_TOKEN` to be
set in its environment at startup. Without it,
`authorize_sign/begin` returns **501 Not Implemented** and the
signer's DynamicAssertion callback raises; c2pa-rs's FFI boundary
swallows the actual HTTP error and surfaces it only as
`bad parameter: DynamicAssertion callback returned an error`.

To start the keystore correctly (recommended — uses the canonical
start script from the keystore repo, handles daemonization, port
conflict detection, health-probing, and token plumbing):

```bash
# From the CLI repo root. The shim forwards into
# $STARDUSTPROOF_KEYSTORE_REPO/scripts/start-dev-keystore.sh
# (default: ../stardustproof-keystore).
scripts/start-dev-keystore.sh            # --start
scripts/start-dev-keystore.sh --status
scripts/start-dev-keystore.sh --stop
scripts/start-dev-keystore.sh --restart
```

The token value defaults to `dev-access-token-smoke` inside the
keystore script; it must match `STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN`
in `.env.smoke.local`.

Manual invocation (still supported when the keystore repo is not
checked out side-by-side):

```bash
cd stardustproof-keystore
KEYSTORE_DEV_SIGNING_ACCESS_TOKEN=dev-access-token-smoke \
  python -m uvicorn stardustproof_keystore.app:app \
  --host 127.0.0.1 --port 2001
```

On WSL, backgrounding uvicorn by hand requires careful nohup/disown
or a wrapped systemd unit — the start script takes care of this with
`nohup setsid`. If you restart the keystore manually, verify the
process is live after ~5 seconds via
`curl -sf http://localhost:2001/health`.

### Run the smoke

```bash
bash scripts/run_smoke.sh
```

Expected duration: ~50-60 s for six tests.

## Rules

- **Binaries under `bin/` are committed.** Rebuild via
  `scripts/build_patched_ffmpeg.sh` when the patched ffmpeg
  feature profile changes; update `bin/ffmpeg/VERSION.md`.
- **Never modify `consumer-sdproof-candidate/` or its `stardust`
  submodule.** The CLI repo references Stardust sources via the
  `STARDUST_SRC` env var used by `build_patched_ffmpeg.sh`; source
  changes belong in the consumer repo.
- Stardust filter invocations go through `stardust.py`; do not
  shell out to `sffw-embed` / `extract` directly from `cli.py` or
  `verify.py`.
- The ffmpeg feature profile is intentionally narrow (no lavfi, no
  image2pipe muxer, no libwebp). Test fixtures that need those
  features must use **system** ffmpeg (see
  `stardustproof-c2pa-signer-vibe/tests/test_video_thumbnail.py`
  and `stardustproof-c2pa-signer-vibe/tests/fixtures/tiny-segmented/README.md`
  for the regeneration commands).
- All trust anchor resolution in verify flows through the signer
  repo's keystore submodule PEMs
  (`keystore/certs/{castlabs_c2pa_ca,trusted_publisher_ca}.cert.pem`).
  The CLI never hard-codes cert paths.

## Code Style

- `snake_case` functions/variables, `SCREAMING_SNAKE_CASE` constants.
- Type hints throughout. Dataclasses for structured data
  (`VerifyResult`, `StardustConfig`, `StardustPaths`).
- Imports: stdlib first, then third-party, then local.
- Subprocess invocations use `capture_output=True` + explicit
  `timeout=`; output is decoded only on error paths.

## When You Change These, Update Docs Too

| What changed | Update |
|---|---|
| `sign` / `verify` CLI flags | `cli.py` argparse, `README.md` |
| Media-input classification rules | Signer's `media_input.py`, CLI `README.md`, smoke tests |
| Bundled ffmpeg feature profile | `scripts/build_patched_ffmpeg.sh`, `bin/ffmpeg/VERSION.md`, this file |
| Manifest store layout | `manifest_store.py`, `README.md` |
| Exit-code contract | `verify.py`, `README.md` |
