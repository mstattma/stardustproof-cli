from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from stardustproof_cli.config import StardustConfig, StardustPaths
from stardustproof_cli.manifest_store import DirectoryManifestStore
from stardustproof_cli import stardust


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
    sign.add_argument("--strength", type=int, default=None)
    sign.add_argument("--sp-width", type=int, default=None)
    sign.add_argument("--sp-height", type=int, default=None)
    sign.add_argument("--sp-density", type=int, default=None)
    sign.add_argument("--p-density", type=int, default=None)
    sign.add_argument("--pm-mode", type=int, default=None)
    sign.add_argument("--seed", type=int, default=None)
    sign.add_argument("--fec", type=int, default=None)
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

    step_start = time.perf_counter()
    media = stardust.probe_media(args.input, config.paths)
    print(f"[cli] Media detected: {media.media_kind} {media.width}x{media.height}", flush=True)
    media_probe_s = time.perf_counter() - step_start

    step_start = time.perf_counter()
    stardust.embed(
        args.input,
        args.output,
        args.wm_payload_hex.lower(),
        config,
        video_preset=args.video_preset,
        video_crf=args.video_crf,
    )
    embed_s = time.perf_counter() - step_start
    print(f"[cli] Watermark embed: {embed_s:.2f}s", flush=True)

    keystore = KeystoreClient(base_url=args.keystore_url, api_key=args.keystore_api_key)
    step_start = time.perf_counter()
    manifest_bytes = generate_and_embed_manifest_simple(
        image_path=args.output,
        wm_id_bytes=payload,
        thumbnail=args.thumbnail,
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


def main() -> int:
    args = _parse_args()
    if args.command == "sign":
        return cmd_sign(args)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
