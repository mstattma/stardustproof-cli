from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from stardustproof_cli.cli import cmd_sign
from stardustproof_cli import stardust, verify as verify_mod
from stardustproof_cli.config import StardustConfig, StardustPaths


FIXTURES_DIR = Path(__file__).parent / "fixtures"

# 48-bit watermark payload. The Phase 0 analysis showed that 48 bits with
# strength=4 survives both JPEG/MJPEG and libx264 veryfast/crf=18 for blind
# extraction at 1080p, whereas longer payloads (>=144 bits) do not.
SMOKE_WM_HEX = "001122334455"


def _smoke_env() -> tuple[str, str, str, str]:
    keystore_url = os.environ.get("STARDUSTPROOF_TEST_KEYSTORE_URL")
    org_uuid = os.environ.get("STARDUSTPROOF_TEST_ORG_UUID")
    access_token = os.environ.get("STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN")
    bin_dir = os.environ.get("STARDUSTPROOF_TEST_BIN_DIR")

    if not all([keystore_url, org_uuid, access_token, bin_dir]):
        pytest.skip("Set STARDUSTPROOF_TEST_KEYSTORE_URL, STARDUSTPROOF_TEST_ORG_UUID, STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN, and STARDUSTPROOF_TEST_BIN_DIR")
    return keystore_url, org_uuid, access_token, bin_dir


def _build_sign_args(
    *,
    input_path: Path,
    output_path: Path,
    manifest_store: Path,
    org_uuid: str,
    keystore_url: str,
    access_token: str,
    bin_dir: str,
    wm_payload_hex: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="sign",
        input=str(input_path),
        output=str(output_path),
        wm_payload_hex=wm_payload_hex,
        wm_bit_profile=len(bytes.fromhex(wm_payload_hex)) * 8,
        manifest_store=str(manifest_store),
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        keystore_api_key="",
        signing_access_token=access_token,
        claim_generator_name="STARDUSTproof CLI Test",
        claim_generator_version="1.0",
        overwrite_manifest=False,
        thumbnail=False,
        bin_dir=bin_dir,
        video_preset="veryfast",
        video_crf=18,
        strength=None,
        sp_width=None,
        sp_height=None,
        sp_density=None,
        p_density=None,
        pm_mode=None,
        seed=None,
        fec=None,
    )


def _verify_via_cli(
    output_path: Path,
    manifest_store: Path,
    wm_payload_hex: str,
    bin_dir: str,
) -> None:
    """Run the productized verify pipeline against a freshly-signed asset
    and assert every exit-code branch stays green.

    This funnels the smoke through the exact same code path that
    ``stardustproof verify`` exposes to end users, so any behavior drift
    between the CLI and the smoke is impossible by construction.
    """

    config = StardustConfig(paths=StardustPaths(custom_bin_dir=Path(bin_dir).resolve()).resolve())
    wm_bit_profile = len(bytes.fromhex(wm_payload_hex)) * 8

    result = verify_mod.verify_asset(
        input_path=output_path,
        manifest_store=manifest_store,
        config=config,
        wm_bit_profile=wm_bit_profile,
    )

    if not result.ok:
        # Render the same diagnostic the CLI would print to aid debugging
        # when a smoke regresses.
        print(
            verify_mod.render_human(result, input_path=output_path),
            flush=True,
        )
    assert result.ok, f"verify_asset failed: exit={result.exit_code} error={result.error!r}"
    assert result.exit_code == 0
    assert result.wm_id_hex == wm_payload_hex.lower(), (
        f"blind-extracted WM {result.wm_id_hex!r} did not match embedded {wm_payload_hex!r}"
    )
    assert result.manifest_path == manifest_store / f"{wm_payload_hex.lower()}.c2pa"
    assert result.soft_binding is not None
    assert result.soft_binding["data"]["alg"] == verify_mod.WATERMARK_ALG
    assert result.soft_binding["data"]["value"].lower() == wm_payload_hex.lower()
    assert not result.failure, f"unexpected validation failures: {result.failure}"
    assert result.success, "expected at least one success entry (signingCredential.trusted)"

    print(
        f"[smoke] verify OK: wm={result.wm_id_hex} "
        f"state={result.validation_state} "
        f"success={len(result.success)} "
        f"info={len(result.informational)} "
        f"failures={len(result.failure)} "
        f"blind={result.timings.get('blind_extract_s', 0):.2f}s "
        f"c2patool={result.timings.get('c2patool_s', 0):.2f}s "
        f"total={result.timings.get('total_s', 0):.2f}s",
        flush=True,
    )


@pytest.mark.integration
def test_sign_image_smoke_with_real_keystore(tmp_path: Path):
    start = time.perf_counter()
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "sample-photo.jpg"
    output_path = tmp_path / "signed.jpg"
    manifest_store = tmp_path / "manifest-store"
    args = _build_sign_args(
        input_path=input_path,
        output_path=output_path,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=SMOKE_WM_HEX,
    )

    print(f"[smoke] image fixture: {input_path}", flush=True)
    print(f"[smoke] image output: {output_path}", flush=True)

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    print(f"[smoke] image smoke completed in {time.perf_counter() - start:.2f}s", flush=True)


@pytest.mark.integration
def test_sign_video_smoke_with_real_keystore(tmp_path: Path):
    start = time.perf_counter()
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "big-buck-bunny-trailer-1080p.mov"
    output_path = tmp_path / "signed.mov"
    manifest_store = tmp_path / "manifest-store"
    args = _build_sign_args(
        input_path=input_path,
        output_path=output_path,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=SMOKE_WM_HEX,
    )

    print(f"[smoke] video fixture: {input_path}", flush=True)
    print(f"[smoke] video output: {output_path}", flush=True)

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    print(f"[smoke] video smoke completed in {time.perf_counter() - start:.2f}s", flush=True)


@pytest.mark.integration
def test_sign_single_file_fragmented_smoke_with_real_keystore(tmp_path: Path):
    """End-to-end sign+verify for a single-file fragmented MP4.

    The bbb-fragmented-single.mp4 fixture is a fragmented MP4 (moov +
    9 moof/mdat pairs, 4-second fragments). Signing should:
      1. Classify input as SingleFileFragmented.
      2. Watermark the elementary stream via sffwembedsafe.
      3. Re-fragment the output at keyframe boundaries derived from
         the input's fragment schedule.
      4. Emit a detached manifest that verifies cleanly via c2patool
         (single-file fMP4 is handled internally by verify_stream_hash
         with no fragments_glob).
    """
    start = time.perf_counter()
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "bbb-fragmented-single.mp4"
    output_path = tmp_path / "signed.mp4"
    manifest_store = tmp_path / "manifest-store"
    args = _build_sign_args(
        input_path=input_path,
        output_path=output_path,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=SMOKE_WM_HEX,
    )

    print(f"[smoke] single-file fragmented fixture: {input_path}", flush=True)

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    # Output must still be a fragmented MP4.
    from stardustproof_cli.media_input import (
        SingleFileFragmented,
        resolve_media_input,
    )
    assert isinstance(resolve_media_input(output_path), SingleFileFragmented)
    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    print(
        f"[smoke] single-file fragmented smoke completed in "
        f"{time.perf_counter() - start:.2f}s",
        flush=True,
    )


@pytest.mark.integration
def test_sign_segmented_rejected_with_clear_error(tmp_path: Path):
    """Signing a segmented directory must be refused cleanly."""

    import argparse

    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()
    input_path = FIXTURES_DIR / "bbb-segmented"
    args = _build_sign_args(
        input_path=input_path,
        output_path=tmp_path / "signed-seg",
        manifest_store=tmp_path / "ms",
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=SMOKE_WM_HEX,
    )

    with pytest.raises(RuntimeError, match="segmented fragmented-MP4"):
        cmd_sign(args)


@pytest.mark.integration
def test_verify_segmented_pre_watermark_exits_2(tmp_path: Path):
    """Verify against a pre-watermark segmented fixture: the blind-extract
    must return None and the CLI must exit 2 without crashing.

    This exercises the Segmented verify dispatch: the init + first
    fragment are piped into ffmpeg for blind extraction, and the
    resolver finds the init + fragment set.
    """

    # The real verify shell-out path is exercised by verify_asset().
    from stardustproof_cli.config import StardustConfig, StardustPaths
    from stardustproof_cli import verify as verify_mod

    _, _, _, bin_dir = _smoke_env()
    config = StardustConfig(paths=StardustPaths(custom_bin_dir=Path(bin_dir).resolve()).resolve())
    store = tmp_path / "empty-store"
    store.mkdir()
    result = verify_mod.verify_asset(
        input_path=FIXTURES_DIR / "bbb-segmented",
        manifest_store=store,
        config=config,
        wm_bit_profile=48,
        check_trust=False,
    )
    assert result.exit_code == 2, (
        f"expected exit 2 (no WM), got {result.exit_code}: {result.error}"
    )
    print(
        f"[smoke] segmented-verify pre-watermark exit 2 OK "
        f"(blind={result.timings.get('blind_extract_s', 0):.2f}s)",
        flush=True,
    )
