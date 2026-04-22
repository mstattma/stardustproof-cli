from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from stardustproof_cli.cli import cmd_sign
from stardustproof_cli import stardust
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


def _assert_blind_extract(output_path: Path, wm_payload_hex: str, bin_dir: str) -> None:
    config = StardustConfig(paths=StardustPaths(custom_bin_dir=Path(bin_dir).resolve()).resolve())
    wm_bit_profile = len(bytes.fromhex(wm_payload_hex)) * 8
    recovered = stardust.extract_blind(str(output_path), wm_bit_profile=wm_bit_profile, config=config)
    assert recovered is not None, "blind extraction failed (no WM ID decoded)"
    assert recovered.lower() == wm_payload_hex.lower(), (
        f"blind extraction returned {recovered!r}, expected {wm_payload_hex!r}"
    )
    print(f"[smoke] blind extract OK: {recovered}", flush=True)


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
    assert (manifest_store / f"{SMOKE_WM_HEX}.c2pa").exists()
    _assert_blind_extract(output_path, SMOKE_WM_HEX, bin_dir)
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
    assert (manifest_store / f"{SMOKE_WM_HEX}.c2pa").exists()
    _assert_blind_extract(output_path, SMOKE_WM_HEX, bin_dir)
    print(f"[smoke] video smoke completed in {time.perf_counter() - start:.2f}s", flush=True)
