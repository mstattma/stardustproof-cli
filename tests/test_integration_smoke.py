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

    # Ensure the signer's video-thumbnail path uses our bundled ffmpeg
    # (which is the one we control the feature-profile of). Falls back
    # to the signer's PATH-based discovery when unset.
    if not os.environ.get("STARDUSTPROOF_FFMPEG"):
        bundled_ffmpeg = Path(bin_dir) / "ffmpeg" / "bin" / "ffmpeg"
        if bundled_ffmpeg.is_file():
            os.environ["STARDUSTPROOF_FFMPEG"] = str(bundled_ffmpeg.resolve())
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
    thumbnail: bool = True,
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
        thumbnail=thumbnail,
        bin_dir=bin_dir,
        video_preset="veryfast",
        video_crf=18,
        in_place=False,
        force=False,
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
):
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
    return result


def _assert_manifest_has_thumbnail(verify_result, *, expected_mime_prefix: str = "image/") -> None:
    """Walk the c2patool --detailed report to confirm that the signed
    manifest carries a ``c2pa.thumbnail.claim`` resource.

    c2patool places thumbnails under the active manifest's ``thumbnail``
    key (normalized legacy shape) and/or in the ``assertion_store``
    under ``c2pa.thumbnail.claim.<ext>`` (c2pa-rs 0.78+). We tolerate
    both shapes so the smoke keeps working across c2patool versions.
    """
    report = verify_result.report
    assert isinstance(report, dict), "verify result missing c2patool report"
    container = report.get("manifest_store", report)
    manifests = container.get("manifests")
    assert isinstance(manifests, dict) and manifests, (
        "c2patool report has no manifests"
    )
    active = (
        container.get("active_manifest")
        or container.get("activeManifest")
        or next(iter(manifests))
    )
    manifest = manifests[active]
    assert isinstance(manifest, dict)

    found_label = None
    found_mime = None

    # Shape 1: modern assertion_store keyed by label.
    assertion_store = manifest.get("assertion_store")
    if isinstance(assertion_store, dict):
        for key in assertion_store:
            if isinstance(key, str) and key.startswith("c2pa.thumbnail.claim"):
                found_label = key
                break

    # Shape 2: top-level thumbnail object.
    thumb = manifest.get("thumbnail")
    if thumb and isinstance(thumb, dict):
        mime = thumb.get("format") or thumb.get("mime_type")
        if isinstance(mime, str):
            found_mime = mime
        if found_label is None:
            found_label = "thumbnail"

    # Shape 3: legacy assertions list.
    assertions = manifest.get("assertions")
    if found_label is None and isinstance(assertions, list):
        for entry in assertions:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label", "")
            if isinstance(label, str) and label.startswith("c2pa.thumbnail.claim"):
                found_label = label
                break

    assert found_label is not None, (
        f"no c2pa.thumbnail.claim found in signed manifest "
        f"(assertion_store keys: {list(assertion_store.keys()) if isinstance(assertion_store, dict) else 'n/a'})"
    )
    if found_mime:
        assert found_mime.startswith(expected_mime_prefix), (
            f"thumbnail mime {found_mime!r} does not start with "
            f"{expected_mime_prefix!r}"
        )
    print(f"[smoke] thumbnail assertion OK: label={found_label} mime={found_mime}", flush=True)


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
    result = _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    _assert_manifest_has_thumbnail(result, expected_mime_prefix="image/")
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
    result = _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    _assert_manifest_has_thumbnail(result, expected_mime_prefix="image/")
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
    from stardustproof_c2pa_signer import (
        SingleFileFragmented,
        resolve_media_input,
    )
    assert isinstance(resolve_media_input(output_path), SingleFileFragmented)
    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    result = _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    _assert_manifest_has_thumbnail(result, expected_mime_prefix="image/")
    print(
        f"[smoke] single-file fragmented smoke completed in "
        f"{time.perf_counter() - start:.2f}s",
        flush=True,
    )


@pytest.mark.integration
def test_sign_segmented_smoke_with_real_keystore(tmp_path: Path):
    """End-to-end sign+verify for a segmented fragmented-MP4 directory.

    The bbb-segmented fixture is an 8-segment DASH tree (init.m4s +
    seg-NNNN.m4s). Signing should:
      1. Classify input as Segmented.
      2. Watermark each media segment via the sffwembedsafe-enabled
         DASH pipeline in stardust.embed_segmented (scratch dir).
      3. Sign via Builder.sign_fragmented, embedding the manifest into
         the new init segment and inserting merkle-placeholder boxes
         into each fragment.
      4. Emit a flat output directory (--output) with init.m4s +
         seg-NNNN.m4s.
      5. The detached manifest in the store verifies cleanly against
         the signed output directory via c2patool's fragment
         --fragments_glob path.
    """
    start = time.perf_counter()
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "bbb-segmented"
    output_path = tmp_path / "signed-seg"
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

    print(f"[smoke] segmented fixture: {input_path}", flush=True)

    rc = cmd_sign(args)

    assert rc == 0
    # Output must exist as a flat directory of init + fragments.
    assert output_path.is_dir(), f"expected {output_path} to be a directory"
    from stardustproof_c2pa_signer import Segmented, resolve_media_input
    resolved = resolve_media_input(output_path)
    assert isinstance(resolved, Segmented)
    # Input tree untouched.
    assert (input_path / "init.m4s").exists()

    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    result = _verify_via_cli(output_path, manifest_store, SMOKE_WM_HEX, bin_dir)
    _assert_manifest_has_thumbnail(result, expected_mime_prefix="image/")
    print(
        f"[smoke] segmented smoke completed in "
        f"{time.perf_counter() - start:.2f}s",
        flush=True,
    )


@pytest.mark.integration
def test_sign_segmented_in_place_smoke_with_real_keystore(tmp_path: Path):
    """Segmented sign with --in-place atomically replaces the input tree.

    Copies the bbb-segmented fixture into a scratch directory so we do
    NOT clobber the committed repo fixture, then runs sign with
    --in-place pointing --output at the same directory. Verifies that:
      1. cmd_sign returns 0.
      2. The scratch directory contents were replaced (new init + new
         fragments; file sizes differ from the original fixture since
         the signed init carries the JUMBF manifest and each fragment
         gets a merkle-placeholder uuid box).
      3. Verify against the same directory exits 0 with soft-binding
         matching and zero validation failures.
    """
    import shutil

    start = time.perf_counter()
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    src_fixture = FIXTURES_DIR / "bbb-segmented"
    work_dir = tmp_path / "in-place-seg"
    shutil.copytree(src_fixture, work_dir)
    # Drop the non-BMFF DASH manifest sidecar; it's not part of the
    # Segmented contract and our resolver ignores it anyway.
    (work_dir / "manifest.mpd").unlink(missing_ok=True)

    # Record original sizes to confirm replacement happened.
    pre_init_size = (work_dir / "init.m4s").stat().st_size
    pre_frag_names = sorted(p.name for p in work_dir.glob("seg-*.m4s"))
    pre_frag_sizes = {p.name: p.stat().st_size for p in work_dir.glob("seg-*.m4s")}

    manifest_store = tmp_path / "manifest-store"
    args = _build_sign_args(
        input_path=work_dir,
        output_path=work_dir,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=SMOKE_WM_HEX,
    )
    args.in_place = True

    print(
        f"[smoke] in-place segmented fixture copy: {work_dir}",
        flush=True,
    )

    rc = cmd_sign(args)
    assert rc == 0

    # Work dir must still classify as Segmented and have the same
    # fragment *names* (in-place replacement preserves names; we do
    # not require sample-accurate preservation since the encoder is
    # free to pick keyframe positions within the boundaries we force).
    from stardustproof_c2pa_signer import Segmented, resolve_media_input
    resolved = resolve_media_input(work_dir)
    assert isinstance(resolved, Segmented)
    post_frag_names = sorted(p.name for p in work_dir.glob("seg-*.m4s"))
    assert post_frag_names == pre_frag_names, (
        f"expected fragment filenames preserved by in-place swap: "
        f"pre={pre_frag_names} post={post_frag_names}"
    )

    # Signed init carries the JUMBF manifest, so size MUST differ.
    post_init_size = (work_dir / "init.m4s").stat().st_size
    assert post_init_size != pre_init_size, (
        f"expected init.m4s size to change after sign, but both are {pre_init_size}"
    )
    # At least one fragment size changes too (merkle-placeholder uuid
    # box insertion plus watermarked elementary stream).
    changed = [
        name for name, sz in pre_frag_sizes.items()
        if (work_dir / name).stat().st_size != sz
    ]
    assert changed, (
        "expected at least one fragment to change size after in-place sign"
    )

    manifest_path = manifest_store / f"{SMOKE_WM_HEX}.c2pa"
    assert manifest_path.exists()
    result = _verify_via_cli(work_dir, manifest_store, SMOKE_WM_HEX, bin_dir)
    _assert_manifest_has_thumbnail(result, expected_mime_prefix="image/")

    # Confirm the committed repo fixture was NOT touched.
    assert (src_fixture / "init.m4s").exists()
    assert (src_fixture / "init.m4s").stat().st_size == pre_init_size, (
        "committed bbb-segmented fixture was modified -- in-place test "
        "leaked into the repo tree!"
    )

    print(
        f"[smoke] in-place segmented smoke completed in "
        f"{time.perf_counter() - start:.2f}s",
        flush=True,
    )


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
