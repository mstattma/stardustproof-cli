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

# Soft-binding algorithm identifier embedded in every StardustProof manifest
# for the Stardust steganographic watermark. Must match
# stardustproof_c2pa_signer.WATERMARK_ALG_NAME.
WATERMARK_ALG = "castlabs.stardust"

# Trust-anchor PEMs shipped by the signer repo's keystore submodule. We need
# BOTH the Castlabs claim-generator CA (used for the c2pa.signature claim
# signature) and the Trusted Publisher CA (used for the cawg.identity
# cawg.x509.cose chain) so c2patool does not emit
# ``signingCredential.untrusted`` failures. Resolved lazily at verification
# time so test collection does not require the signer package to be
# importable.
_TRUST_PEM_RELATIVE_PATHS = [
    Path("keystore") / "certs" / "castlabs_c2pa_ca.cert.pem",
    Path("keystore") / "certs" / "trusted_publisher_ca.cert.pem",
]


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


def _resolve_signer_repo_root() -> Path:
    """Find the signer repo root that contains the keystore submodule certs."""

    try:
        import stardustproof_c2pa_signer  # noqa: F401
    except Exception as exc:  # pragma: no cover - exercised via skip
        pytest.skip(f"stardustproof_c2pa_signer not importable: {exc}")

    import stardustproof_c2pa_signer as signer_pkg

    signer_pkg_path = Path(signer_pkg.__file__).resolve().parent
    candidates = [
        # Editable install from a checkout: .../<repo>/src/stardustproof_c2pa_signer
        signer_pkg_path.parents[1],
        # site-packages install with keystore/ submodule copied alongside
        signer_pkg_path.parent,
        # Sibling checkout next to stardustproof-cli
        Path(__file__).resolve().parents[2] / "stardustproof-c2pa-signer-vibe",
    ]
    for candidate in candidates:
        if all((candidate / rel).is_file() for rel in _TRUST_PEM_RELATIVE_PATHS):
            return candidate
    pytest.skip(
        "Keystore trust anchor PEMs not found in any of: "
        + ", ".join(str(c) for c in candidates)
    )


def _resolve_trust_anchors(tmp_path: Path) -> Path:
    """Concatenate the keystore CA PEMs into a single bundle that c2patool
    ``--trust_anchors`` can consume.

    We need both the Castlabs claim-generator CA (for the claim signature)
    and the Trusted Publisher CA (for the CAWG publisher identity cert).
    """

    root = _resolve_signer_repo_root()
    bundle = tmp_path / "c2patool_trust_anchors.pem"
    with bundle.open("wb") as out:
        for rel in _TRUST_PEM_RELATIVE_PATHS:
            pem_path = root / rel
            out.write(pem_path.read_bytes())
            out.write(b"\n")
    return bundle


def _build_c2patool_settings(tmp_path: Path, trust_anchors_path: Path) -> Path:
    """Materialize a c2patool settings TOML that populates both the generic
    ``[trust]`` store and the ``[cawg_trust]`` store so that both the claim
    signature and the CAWG identity assertion X.509 cert chain up cleanly.

    ``c2patool`` exposes only a ``trust`` subcommand for the generic store;
    CAWG trust has no CLI flag and must be configured via settings.
    """

    from stardustproof_c2pa_signer.c2patool import write_cawg_trust_settings

    pem_text = trust_anchors_path.read_text()
    settings_path = tmp_path / "c2patool_settings.toml"
    return write_cawg_trust_settings(settings_path, trust_anchors_pem=pem_text)


def _resolve_c2patool() -> Path:
    """Locate the bundled c2patool. Skips the test cleanly if unavailable."""

    try:
        from stardustproof_c2pa_signer.c2patool import (
            C2patoolNotFoundError,
            c2patool_path,
        )
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"stardustproof_c2pa_signer.c2patool import failed: {exc}")

    try:
        return c2patool_path()
    except C2patoolNotFoundError as exc:
        pytest.skip(str(exc))


def _verify_with_c2patool(
    watermarked_path: Path,
    manifest_path: Path,
    wm_payload_hex: str,
    trust_anchors: Path,
    settings_path: Path,
) -> None:
    """Run c2patool against the watermarked asset + detached manifest and
    assert:

    1. c2patool exits 0.
    2. The active manifest contains a ``c2pa.soft-binding`` assertion with
       ``alg == castlabs.stardust`` whose hex value matches ``wm_payload_hex``.
    3. ``validation_results.activeManifest.failure`` is empty AND ``success``
       is non-empty (which requires passing ``--trust_anchors`` so at least
       the trust-chain ``signingCredential.trusted`` success entry is
       produced).

    Verification is performed against the *detached* manifest from the
    directory manifest store, NOT any embedded manifest in the asset.
    """

    from stardustproof_c2pa_signer.c2patool import (
        extract_validation_results,
        find_soft_binding,
        verify_detached_manifest,
    )

    start = time.perf_counter()
    result = verify_detached_manifest(
        asset_path=watermarked_path,
        manifest_path=manifest_path,
        trust_anchors=trust_anchors,
        settings_path=settings_path,
        detailed=True,
    )
    elapsed = time.perf_counter() - start

    print(f"[smoke] c2patool verify elapsed: {elapsed:.2f}s", flush=True)
    if result.returncode != 0:
        print(f"[smoke] c2patool stderr:\n{result.stderr}", flush=True)
        print(f"[smoke] c2patool stdout (first 2k):\n{result.stdout[:2000]}", flush=True)
    assert result.returncode == 0, f"c2patool exited {result.returncode}"
    assert result.report is not None, "c2patool did not emit parseable JSON"

    sb = find_soft_binding(result.report, alg=WATERMARK_ALG)
    assert sb is not None, (
        f"c2patool report has no c2pa.soft-binding with alg={WATERMARK_ALG!r}. "
        f"assertions in report: {result.report}"
    )
    # Soft-binding data.value is the hex-encoded watermark payload (see
    # stardustproof_c2pa_signer.manifest._create_watermark_soft_binding).
    sb_value = sb.get("data", {}).get("value")
    assert isinstance(sb_value, str), f"soft-binding missing .data.value: {sb!r}"
    assert sb_value.lower() == wm_payload_hex.lower(), (
        f"soft-binding value {sb_value!r} does not match embedded watermark "
        f"{wm_payload_hex!r}"
    )
    print(f"[smoke] c2patool soft-binding OK: alg={WATERMARK_ALG} value={sb_value}", flush=True)

    vr = extract_validation_results(result.report)
    assert not vr["failure"], (
        f"c2patool reported {len(vr['failure'])} validation failures: "
        f"{vr['failure']}"
    )
    assert vr["success"], (
        "c2patool validation_results.activeManifest.success is empty -- "
        "with trust_anchors set we expect at least signingCredential.trusted"
    )
    print(
        f"[smoke] c2patool validation OK: {len(vr['success'])} success, "
        f"{len(vr['informational'])} informational, 0 failures",
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
    _assert_blind_extract(output_path, SMOKE_WM_HEX, bin_dir)
    _resolve_c2patool()  # skip cleanly if not bundled/available
    trust_anchors = _resolve_trust_anchors(tmp_path)
    settings_path = _build_c2patool_settings(tmp_path, trust_anchors)
    _verify_with_c2patool(
        output_path, manifest_path, SMOKE_WM_HEX, trust_anchors, settings_path
    )
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
    _assert_blind_extract(output_path, SMOKE_WM_HEX, bin_dir)
    _resolve_c2patool()  # skip cleanly if not bundled/available
    trust_anchors = _resolve_trust_anchors(tmp_path)
    settings_path = _build_c2patool_settings(tmp_path, trust_anchors)
    _verify_with_c2patool(
        output_path, manifest_path, SMOKE_WM_HEX, trust_anchors, settings_path
    )
    print(f"[smoke] video smoke completed in {time.perf_counter() - start:.2f}s", flush=True)
