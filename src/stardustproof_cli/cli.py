from __future__ import annotations

import argparse
import json as _json
import sys
import time
from pathlib import Path

from stardustproof_cli.config import StardustConfig, StardustPaths
from stardustproof_cli.manifest_store import DirectoryManifestStore
from stardustproof_cli import stardust
from stardustproof_cli import verify as verify_mod
from stardustproof_c2pa_signer import (
    MediaInputError,
    Segmented,
    SingleFile,
    SingleFileFragmented,
    parse_fragment_schedule,
    resolve_media_input,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stardustproof",
        description="Watermark content and sign it with the simplified StardustProof manifest workflow.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sign = sub.add_parser("sign", help="Watermark an image/video and write a detached manifest")
    sign.add_argument("--input", required=True, help="Input image or video path")
    sign.add_argument("--output", required=True, help="Output watermarked image/video path")
    sign.add_argument("--wm-payload-hex", required=True, help="Watermark payload as hex")
    sign.add_argument("--wm-bit-profile", type=int, default=None, help="Watermark bit length")
    sign.add_argument("--manifest-store", required=True, help="Directory for detached manifests")
    sign.add_argument("--org-uuid", required=True, help="Organization UUID for org:{uuid} signing")
    sign.add_argument("--keystore-url", required=True, help="Keystore base URL")
    sign.add_argument("--keystore-api-key", default="", help="Optional keystore API key")
    sign.add_argument("--signing-access-token", required=True, help="Bearer token for org signing")
    sign.add_argument("--claim-generator-name", default="STARDUSTproof CLI", help="Claim generator name")
    sign.add_argument("--claim-generator-version", default="1.0", help="Claim generator version")
    sign.add_argument("--overwrite-manifest", action="store_true", help="Overwrite manifest store entry if it exists")
    sign.add_argument("--no-thumbnail", dest="thumbnail", action="store_false", default=True, help="Skip thumbnail generation")
    sign.add_argument("--bin-dir", help="Directory containing bundled Stardust + ffmpeg binaries")
    sign.add_argument(
        "--video-preset",
        default="veryfast",
        help="libx264 encode preset for video outputs (default: veryfast; 'ultrafast' is too aggressive for blind extraction)",
    )
    sign.add_argument(
        "--video-crf",
        type=int,
        default=18,
        help="libx264 CRF for video outputs (default: 18)",
    )
    sign.add_argument(
        "--in-place",
        action="store_true",
        help=(
            "For Segmented input: atomically replace the input directory "
            "contents with the signed output. --output must equal --input "
            "(or be unset, in which case it defaults to --input). "
            "Interrupted signs leave the input tree in a partially-"
            "replaced state; prefer a separate --output for production."
        ),
    )
    sign.add_argument(
        "--force",
        action="store_true",
        help=(
            "For Segmented input with a non-empty --output directory: "
            "overwrite existing files. Required (alongside --in-place, if "
            "used) to proceed with a non-empty output directory."
        ),
    )
    sign.add_argument(
        "--title",
        default=None,
        help=(
            "Human-readable title to embed in the C2PA manifest. Shown as "
            "the asset name by Adobe's verify.contentauthenticity.org and "
            "other spec-conformant verifiers. Defaults to the input "
            "file/directory basename. Pass '' (empty string) to suppress "
            "the title field entirely (verifiers will show 'Untitled "
            "asset')."
        ),
    )
    sign.add_argument("--strength", type=int, default=None)
    sign.add_argument("--sp-width", type=int, default=None)
    sign.add_argument("--sp-height", type=int, default=None)
    sign.add_argument("--sp-density", type=int, default=None)
    sign.add_argument("--p-density", type=int, default=None)
    sign.add_argument("--pm-mode", type=int, default=None)
    sign.add_argument("--seed", type=int, default=None)
    sign.add_argument("--fec", type=int, default=None)

    verify = sub.add_parser(
        "verify",
        help="Verify a watermarked asset against a detached C2PA manifest store",
    )
    verify.add_argument("--input", required=True, help="Watermarked asset to verify")
    verify.add_argument(
        "--manifest-store",
        required=True,
        help="Directory containing detached <wm-id>.c2pa manifests",
    )
    verify.add_argument(
        "--wm-bit-profile",
        type=int,
        default=48,
        help="Expected watermark payload bit length (default: 48)",
    )
    verify.add_argument(
        "--trust-anchors",
        action="append",
        default=None,
        help=(
            "Path to a PEM bundle of trust anchors (repeatable). When "
            "omitted, auto-discovers the signer package's keystore CA PEMs."
        ),
    )
    verify.add_argument(
        "--cawg-trust-anchors",
        action="append",
        default=None,
        help=(
            "Path to a PEM bundle for CAWG identity trust (repeatable). "
            "Defaults to --trust-anchors when unset."
        ),
    )
    verify.add_argument(
        "--no-trust",
        action="store_true",
        help=(
            "Skip certificate trust checks. Structure, hashes and "
            "signature math are still validated."
        ),
    )
    verify.add_argument(
        "--bin-dir",
        help="Directory containing bundled Stardust + ffmpeg binaries",
    )
    verify.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a single-line JSON summary on stdout instead of human text",
    )
    verify.add_argument("--strength", type=int, default=None)
    verify.add_argument("--sp-width", type=int, default=None)
    verify.add_argument("--sp-height", type=int, default=None)
    verify.add_argument("--sp-density", type=int, default=None)
    verify.add_argument("--p-density", type=int, default=None)
    verify.add_argument("--pm-mode", type=int, default=None)
    verify.add_argument("--seed", type=int, default=None)
    verify.add_argument("--fec", type=int, default=None)

    return parser.parse_args()


def _build_config(args: argparse.Namespace) -> StardustConfig:
    config = StardustConfig()
    if args.bin_dir:
        config.paths = StardustPaths(custom_bin_dir=Path(args.bin_dir).resolve())
    config.paths = config.paths.resolve()
    for attr, field_name in [
        ("strength", "stardust_strength"),
        ("sp_width", "stardust_sp_width"),
        ("sp_height", "stardust_sp_height"),
        ("sp_density", "stardust_sp_density"),
        ("p_density", "stardust_p_density"),
        ("pm_mode", "stardust_pm_mode"),
        ("seed", "stardust_seed"),
        ("fec", "stardust_fec"),
    ]:
        value = getattr(args, attr)
        if value is not None:
            setattr(config, field_name, value)
    return config


def _validate_payload(payload_hex: str, wm_bit_profile: int | None) -> bytes:
    if len(payload_hex) % 2 != 0:
        raise ValueError("wm-payload-hex must have an even number of hex characters")
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError as exc:
        raise ValueError("wm-payload-hex must be valid hex") from exc
    expected_bits = wm_bit_profile or (len(payload) * 8)
    if len(payload) * 8 != expected_bits:
        raise ValueError(
            f"Watermark payload length mismatch: payload has {len(payload) * 8} bits but wm-bit-profile is {expected_bits}"
        )
    return payload


def _make_org_sign_handler(keystore, access_token: str):
    def _handler(sign_key_id: str, payload: bytes, operation_type: str, x5chain: list[str] | None = None) -> bytes:
        if operation_type == "sign_cose_sign1_embedded":
            return keystore.bearer_sign_cose(
                key_id=sign_key_id,
                payload=payload,
                access_token=access_token,
                protected_headers={"alg": "ES256"},
                x5chain=x5chain,
            )
        return keystore.bearer_sign_raw(
            key_id=sign_key_id,
            data=payload,
            access_token=access_token,
        )

    return _handler


def _validate_segmented_output(
    input_dir: Path, output_dir: Path, *, in_place: bool, force: bool
) -> None:
    """Enforce CLI invariants for Segmented-shape --output.

    - With --in-place: --output must equal --input (or be omitted and
      default to --input).
    - Without --in-place: --output must differ from --input.
    - In both cases a pre-existing non-empty output directory requires
      --force.
    """
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if in_place:
        if output_dir != input_dir:
            raise RuntimeError(
                f"--in-place requires --output == --input "
                f"({input_dir} != {output_dir}); omit --output to default "
                f"to --input, or drop --in-place."
            )
    else:
        if output_dir == input_dir:
            raise RuntimeError(
                f"--output ({output_dir}) must not equal --input for "
                f"Segmented inputs; pass --in-place if you intend to "
                f"replace the input tree."
            )
    if output_dir.is_dir():
        existing = [p for p in output_dir.iterdir() if not p.name.startswith(".")]
        if existing and not force and not in_place:
            names = ", ".join(sorted(p.name for p in existing[:5]))
            raise RuntimeError(
                f"--output directory {output_dir} is non-empty "
                f"(found: {names}...); pass --force to overwrite."
            )


def cmd_sign(args: argparse.Namespace) -> int:
    from stardustproof_c2pa_signer import KeystoreClient, generate_and_embed_manifest_simple

    total_start = time.perf_counter()
    print(f"[cli] Starting sign flow for: {args.input}", flush=True)
    payload = _validate_payload(args.wm_payload_hex, args.wm_bit_profile)
    config = _build_config(args)

    step_start = time.perf_counter()
    missing = stardust.check_binaries(config.paths)
    if missing:
        raise RuntimeError(
            "Missing required binaries (ensure bin/stardust/ and bin/ffmpeg/bin/ are present):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )
    print(f"[cli] Binary checks: {time.perf_counter() - step_start:.2f}s", flush=True)

    # Resolve the input shape so we can dispatch to the right watermark
    # and sign path. The signer's generate_and_embed_manifest_simple
    # classifies the input the same way; pre-classifying here also lets
    # us apply per-shape CLI flag semantics (e.g. --in-place for
    # Segmented).
    try:
        media_input = resolve_media_input(Path(args.input))
    except MediaInputError as exc:
        raise RuntimeError(f"Unable to classify input: {exc}") from exc

    is_segmented = isinstance(media_input, Segmented)

    # Segmented inputs want an output directory. For non-segmented
    # inputs, --in-place / --force are no-ops (single-file sign is
    # always effectively in-place from the caller's perspective).
    if is_segmented:
        _validate_segmented_output(
            Path(args.input),
            Path(args.output),
            in_place=args.in_place,
            force=args.force,
        )

    step_start = time.perf_counter()
    if is_segmented:
        # Probe via init+first-fragment piped together.
        probe_bytes = stardust.read_segmented_init_plus_first_fragment(
            str(media_input.init), str(media_input.fragments[0]),
        )
        media = stardust.probe_media(str(media_input.init), config.paths, stdin_bytes=probe_bytes)
    else:
        media = stardust.probe_media(args.input, config.paths)
    shape_name = type(media_input).__name__
    print(
        f"[cli] Media detected: {media.media_kind} {media.width}x{media.height} "
        f"(shape={shape_name})",
        flush=True,
    )
    media_probe_s = time.perf_counter() - step_start

    # ---- Watermark step -------------------------------------------------
    step_start = time.perf_counter()
    import tempfile as _tempfile
    watermarked_tmp_ctx = None
    try:
        if isinstance(media_input, SingleFileFragmented):
            schedule = parse_fragment_schedule(Path(args.input))
            stardust.embed_single_file_fragmented(
                args.input,
                args.output,
                args.wm_payload_hex.lower(),
                config,
                fragment_schedule=schedule,
                video_preset=args.video_preset,
                video_crf=args.video_crf,
            )
            watermarked_input_for_signer = args.output
        elif is_segmented:
            # Watermark into a scratch directory under --output's parent
            # so downstream moves stay on the same filesystem. The
            # scratch dir is freed after the signer completes.
            parent = Path(args.output).resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
            watermarked_tmp_ctx = _tempfile.TemporaryDirectory(
                prefix="stardustproof-wm-", dir=str(parent)
            )
            watermarked_dir = Path(watermarked_tmp_ctx.name)
            stardust.embed_segmented(
                str(media_input.init),
                [str(p) for p in media_input.fragments],
                str(watermarked_dir),
                args.wm_payload_hex.lower(),
                config,
                video_preset=args.video_preset,
                video_crf=args.video_crf,
            )
            # The signer will classify this directory itself; pass the
            # watermarked dir as input_path.
            watermarked_input_for_signer = str(watermarked_dir)
        else:
            stardust.embed(
                args.input,
                args.output,
                args.wm_payload_hex.lower(),
                config,
                video_preset=args.video_preset,
                video_crf=args.video_crf,
            )
            watermarked_input_for_signer = args.output
        embed_s = time.perf_counter() - step_start
        print(f"[cli] Watermark embed: {embed_s:.2f}s", flush=True)

        # ---- C2PA sign step --------------------------------------------
        keystore = KeystoreClient(base_url=args.keystore_url, api_key=args.keystore_api_key)
        step_start = time.perf_counter()
        # Title defaults to the INPUT basename so consumer verifiers
        # (Adobe's verify.contentauthenticity.org et al.) display the
        # user-facing name of the asset the caller started with, not
        # the intermediate watermark-output name. args.title=None ->
        # derive here. args.title='' -> explicit opt-out, propagate so
        # signer omits the title field.
        if args.title is None:
            effective_title = Path(args.input).name
        else:
            effective_title = args.title

        if is_segmented:
            # Segmented signer requires an explicit output_path. For
            # --in-place we sign into a scratch dir and swap files over
            # originals after success. Otherwise sign directly into
            # --output.
            if args.in_place:
                signer_scratch_ctx = _tempfile.TemporaryDirectory(
                    prefix="stardustproof-signed-",
                    dir=str(Path(args.output).resolve().parent),
                )
                signer_output_dir = signer_scratch_ctx.name
            else:
                signer_scratch_ctx = None
                signer_output_dir = args.output
                Path(signer_output_dir).mkdir(parents=True, exist_ok=True)

            try:
                manifest_bytes = generate_and_embed_manifest_simple(
                    input_path=watermarked_input_for_signer,
                    output_path=signer_output_dir,
                    wm_id_bytes=payload,
                    thumbnail=args.thumbnail,
                    title=effective_title,
                    claim_generator_info=[{"name": args.claim_generator_name, "version": args.claim_generator_version}],
                    keystore_url=args.keystore_url,
                    keystore_api_key=args.keystore_api_key,
                    keystore_client=keystore,
                    publisher_key_id=f"org:{args.org_uuid}",
                    publisher_sign_authorization_handler=_make_org_sign_handler(keystore, args.signing_access_token),
                )
                if args.in_place:
                    import shutil as _shutil
                    import os as _os
                    # Atomically replace each file in --input with its
                    # signed counterpart. Also clean up any stale files
                    # from --input that the sign did not produce.
                    signed_names = set()
                    for src in Path(signer_output_dir).iterdir():
                        dst = Path(args.input) / src.name
                        signed_names.add(src.name)
                        _os.replace(str(src), str(dst))
                    # Remove input-only leftovers that were NOT in the
                    # signed output (e.g. old playlists).
                    for p in Path(args.input).iterdir():
                        if p.name in signed_names:
                            continue
                        if p.name.startswith("."):
                            continue
                        # Preserve non-BMFF sidecars unless --force was
                        # also set (user may have playlists etc.).
                        if args.force:
                            try:
                                p.unlink()
                            except OSError:
                                pass
            finally:
                if signer_scratch_ctx is not None:
                    signer_scratch_ctx.cleanup()
        else:
            # Single signer entry point handles both SingleFile and
            # SingleFileFragmented via internal media-shape classification.
            # No output_path override -- the signer embeds in-place at
            # args.output, and the CLI writes the sidecar separately.
            manifest_bytes = generate_and_embed_manifest_simple(
                input_path=args.output,
                wm_id_bytes=payload,
                thumbnail=args.thumbnail,
                title=effective_title,
                claim_generator_info=[{"name": args.claim_generator_name, "version": args.claim_generator_version}],
                keystore_url=args.keystore_url,
                keystore_api_key=args.keystore_api_key,
                keystore_client=keystore,
                publisher_key_id=f"org:{args.org_uuid}",
                publisher_sign_authorization_handler=_make_org_sign_handler(keystore, args.signing_access_token),
            )
        if manifest_bytes is None:
            raise RuntimeError("Manifest generation failed")
        manifest_sign_s = time.perf_counter() - step_start
        print(f"[cli] Manifest sign/embed: {manifest_sign_s:.2f}s", flush=True)
    finally:
        if watermarked_tmp_ctx is not None:
            watermarked_tmp_ctx.cleanup()

    # ---- Sidecar write -------------------------------------------------
    step_start = time.perf_counter()
    manifest_path = DirectoryManifestStore(Path(args.manifest_store)).write_manifest(
        wm_id_hex=args.wm_payload_hex.lower(),
        manifest_bytes=manifest_bytes,
        overwrite=args.overwrite_manifest,
    )
    manifest_store_s = time.perf_counter() - step_start
    print(f"[cli] Manifest store write: {manifest_store_s:.2f}s", flush=True)

    print(f"Media type: {media.media_kind}")
    print(f"Watermarked output: {args.output}")
    print(f"Manifest store entry: {manifest_path}")
    print(f"WM ID: {args.wm_payload_hex.lower()}")
    print(f"Manifest size: {len(manifest_bytes)} bytes")
    print(
        f"[cli] Timing summary: probe={media_probe_s:.2f}s embed={embed_s:.2f}s "
        f"sign={manifest_sign_s:.2f}s store={manifest_store_s:.2f}s total={time.perf_counter() - total_start:.2f}s",
        flush=True,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = _build_config(args)
    trust_anchors = (
        [Path(p).resolve() for p in args.trust_anchors] if args.trust_anchors else None
    )
    cawg_trust_anchors = (
        [Path(p).resolve() for p in args.cawg_trust_anchors]
        if args.cawg_trust_anchors
        else None
    )

    result = verify_mod.verify_asset(
        input_path=Path(args.input),
        manifest_store=Path(args.manifest_store),
        config=config,
        wm_bit_profile=args.wm_bit_profile,
        trust_anchors=trust_anchors,
        cawg_trust_anchors=cawg_trust_anchors,
        check_trust=not args.no_trust,
    )

    if args.json_output:
        print(_json.dumps(result.to_json_dict(), sort_keys=True), flush=True)
    else:
        print(verify_mod.render_human(result, input_path=Path(args.input)), flush=True)

    return result.exit_code


def main() -> int:
    args = _parse_args()
    if args.command == "sign":
        return cmd_sign(args)
    if args.command == "verify":
        return cmd_verify(args)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
