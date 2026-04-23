"""Verification pipeline for StardustProof-watermarked assets.

The :func:`verify_asset` function blind-extracts the Stardust watermark
from the input asset, looks up the corresponding detached C2PA manifest
in a local manifest-store directory, shells out to the bundled
``c2patool`` to validate the manifest against the asset, and asserts
that the ``c2pa.soft-binding`` assertion inside the manifest matches
the extracted watermark id. This is the productized counterpart to the
verification path in the integration smoke.

Exit-code contract (returned via :class:`VerifyResult.exit_code`):

====  =============================================================
Code  Meaning
====  =============================================================
0     All checks passed.
1     Argument / IO / binary / trust-material error.
2     Blind extraction failed (no Stardust watermark detected).
3     Manifest not found in store for the extracted watermark id.
4     ``c2patool`` invocation failed (non-zero exit or unparseable
      JSON).
5     Soft-binding value in the manifest does not match the
      extracted watermark id.
6     ``c2patool`` reported one or more validation failures
      (e.g. hash mismatch, untrusted cert, BMFF hash failure).
====  =============================================================
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from stardustproof_cli import stardust
from stardustproof_cli.config import StardustConfig
from stardustproof_cli.media_input import (
    MediaInput,
    MediaInputError,
    Segmented,
    SingleFile,
    SingleFileFragmented,
    resolve_media_input,
)


WATERMARK_ALG = "castlabs.stardust"
"""Soft-binding algorithm identifier for the Stardust steganographic
watermark. Must match ``stardustproof_c2pa_signer.WATERMARK_ALG_NAME``."""


_DEFAULT_TRUST_PEM_RELATIVE_PATHS = (
    Path("keystore") / "certs" / "castlabs_c2pa_ca.cert.pem",
    Path("keystore") / "certs" / "trusted_publisher_ca.cert.pem",
)


@dataclass
class VerifyResult:
    """Structured outcome of :func:`verify_asset`."""

    ok: bool
    exit_code: int
    wm_id_hex: Optional[str] = None
    manifest_path: Optional[Path] = None
    soft_binding: Optional[dict] = None
    validation_state: Optional[str] = None
    success: list[dict] = field(default_factory=list)
    failure: list[dict] = field(default_factory=list)
    informational: list[dict] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    report: Optional[dict] = None

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary (omits the full c2patool
        report to keep output compact; use :attr:`report` in-process
        for the full structure)."""

        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "wm_id_hex": self.wm_id_hex,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "soft_binding": self.soft_binding,
            "validation_state": self.validation_state,
            "success": self.success,
            "failure": self.failure,
            "informational": self.informational,
            "timings": self.timings,
            "error": self.error,
        }


def resolve_default_trust_anchor_paths() -> Optional[list[Path]]:
    """Locate the StardustProof keystore CA PEMs shipped by the signer
    package's ``keystore/`` submodule.

    Returns the list of PEM paths on success, or ``None`` if no set of
    candidates contains all required files.

    Discovery order:

    1. Editable install: ``<signer-repo>/keystore/certs/…`` (two levels
       above the installed ``stardustproof_c2pa_signer`` package).
    2. site-packages install with ``keystore/`` copied next to the
       package.
    3. Sibling checkout: ``<cli-repo>/../stardustproof-c2pa-signer-vibe``.
    """

    try:
        import stardustproof_c2pa_signer as signer_pkg  # type: ignore
    except Exception:
        signer_pkg = None

    candidates: list[Path] = []
    if signer_pkg is not None:
        signer_pkg_path = Path(signer_pkg.__file__).resolve().parent
        candidates.append(signer_pkg_path.parents[1])
        candidates.append(signer_pkg_path.parent)
    # Sibling checkout fallback -- assumes layout where the CLI repo
    # lives next to the signer repo under a common parent directory.
    candidates.append(
        Path(__file__).resolve().parents[3] / "stardustproof-c2pa-signer-vibe"
    )

    for root in candidates:
        pem_paths = [root / rel for rel in _DEFAULT_TRUST_PEM_RELATIVE_PATHS]
        if all(p.is_file() for p in pem_paths):
            return pem_paths

    return None


def _concat_pem_bundle(pem_paths: list[Path], destination: Path) -> Path:
    """Concatenate ``pem_paths`` into a single PEM bundle at
    ``destination`` for ``c2patool --trust_anchors`` consumption."""

    with destination.open("wb") as out:
        for pem_path in pem_paths:
            out.write(pem_path.read_bytes())
            if not out.tell() or pem_path.read_bytes()[-1:] != b"\n":
                out.write(b"\n")
    return destination


def _write_trust_bundle(
    pem_paths: list[Path],
    tmp_dir: Path,
    name: str = "trust_anchors.pem",
) -> Path:
    return _concat_pem_bundle(pem_paths, tmp_dir / name)


def _derive_fragments_glob(segmented: Segmented) -> Path:
    """Derive a filename glob pattern that matches every media fragment
    in ``segmented`` but excludes the init segment.

    c2patool's ``--fragments_glob`` is a filename pattern (not a full
    path) rooted at the asset's directory. Per c2patool:

        "The fragments_glob pattern should only match fragment file
         names not the full paths"

    We pick the longest common prefix + ``*`` + longest common suffix
    of the fragment basenames. This works for every packager naming
    convention we have seen (``seg_NNNN.m4s``, ``chunk-XXXXX.m4s``,
    ``segment_N.cmfv``, …). Falls back to ``*`` if the fragments
    share no useful prefix/suffix.
    """

    names = [p.name for p in segmented.fragments]
    if not names:
        return Path("*")

    prefix = os.path.commonprefix(names)
    reversed_names = [n[::-1] for n in names]
    suffix = os.path.commonprefix(reversed_names)[::-1]

    # If prefix+suffix already cover the entire name of a fragment we
    # would emit a non-glob literal that matches only one file; in
    # that case fall through to plain ``*``.
    if any(prefix + suffix == n for n in names):
        return Path("*")

    pattern = f"{prefix}*{suffix}"
    # Sanity check: pattern must not match the init segment.
    from fnmatch import fnmatch as _fnmatch
    if _fnmatch(segmented.init.name, pattern):
        # Fall back to a stricter pattern anchored on a shared
        # fragment-suffix-only match, or "*" if even that catches init.
        if suffix and not _fnmatch(segmented.init.name, f"*{suffix}"):
            pattern = f"*{suffix}"
        else:
            pattern = "*"
    return Path(pattern)


def verify_asset(
    input_path: Path | str,
    manifest_store: Path | str,
    *,
    config: StardustConfig,
    wm_bit_profile: int,
    trust_anchors: Optional[list[Path]] = None,
    cawg_trust_anchors: Optional[list[Path]] = None,
    check_trust: bool = True,
) -> VerifyResult:
    """End-to-end verification of a Stardust-watermarked asset against
    a detached-manifest directory store.

    Args:
        input_path: Path to the (watermarked) asset to verify.
        manifest_store: Directory containing ``<wm-id>.c2pa`` detached
            manifests.
        config: Populated :class:`StardustConfig` with binary paths and
            Stardust embedding parameters (used by blind extraction).
        wm_bit_profile: Watermark payload bit length used to blind-extract.
        trust_anchors: Optional list of PEM paths to use as trust anchors
            for both the claim signature and (absent
            ``cawg_trust_anchors``) the CAWG X.509 identity-assertion
            chain. When ``None`` and ``check_trust=True``, the function
            auto-discovers the signer's keystore CA PEMs; if that fails
            it returns exit code 1.
        cawg_trust_anchors: Optional distinct trust anchors for CAWG
            identity assertions. Defaults to ``trust_anchors``.
        check_trust: When ``False``, runs ``c2patool`` without any
            trust-anchor settings (structure/hash/signature-math only).

    Returns:
        A :class:`VerifyResult` with ``ok``, ``exit_code`` and diagnostic
        fields populated.
    """

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    input_path = Path(input_path)
    manifest_store = Path(manifest_store)

    if not input_path.exists():
        return VerifyResult(
            ok=False, exit_code=1,
            error=f"Input asset not found: {input_path}",
            timings=timings,
        )
    if not manifest_store.is_dir():
        return VerifyResult(
            ok=False, exit_code=1,
            error=f"Manifest store is not a directory: {manifest_store}",
            timings=timings,
        )

    try:
        media = resolve_media_input(input_path)
    except MediaInputError as exc:
        return VerifyResult(
            ok=False, exit_code=1,
            error=f"Unable to classify input: {exc}",
            timings=timings,
        )

    missing_bins = stardust.check_binaries(config.paths)
    if missing_bins:
        return VerifyResult(
            ok=False, exit_code=1,
            error=(
                "Missing required binaries (ensure bin/stardust/ and "
                "bin/ffmpeg/bin/ are present):\n  - "
                + "\n  - ".join(missing_bins)
            ),
            timings=timings,
        )

    # Resolve trust material up front so missing defaults fail fast
    # (exit 1) instead of after the expensive blind-extract step.
    if check_trust:
        if trust_anchors is None:
            resolved = resolve_default_trust_anchor_paths()
            if resolved is None:
                return VerifyResult(
                    ok=False, exit_code=1,
                    error=(
                        "No --trust-anchors supplied and the default "
                        "StardustProof keystore PEMs could not be located. "
                        "Pass --trust-anchors, or rerun with --no-trust to "
                        "skip certificate-trust checks."
                    ),
                    timings=timings,
                )
            trust_anchors = resolved
        for pem in trust_anchors:
            if not pem.is_file():
                return VerifyResult(
                    ok=False, exit_code=1,
                    error=f"Trust anchor PEM not found: {pem}",
                    timings=timings,
                )
        if cawg_trust_anchors is None:
            cawg_trust_anchors = trust_anchors
        for pem in cawg_trust_anchors:
            if not pem.is_file():
                return VerifyResult(
                    ok=False, exit_code=1,
                    error=f"CAWG trust anchor PEM not found: {pem}",
                    timings=timings,
                )

    # Step 1: blind-extract the watermark id.
    #
    # Dispatch depends on the media-input shape:
    #   SingleFile / SingleFileFragmented -> decode from the file directly
    #   Segmented                         -> concatenate init + fragments[0]
    #                                        in memory and pipe to ffmpeg
    step_start = time.perf_counter()
    try:
        if isinstance(media, Segmented):
            stdin_bytes = stardust.read_segmented_init_plus_first_fragment(
                str(media.init), str(media.fragments[0]),
            )
            # extract_blind's `input_path` is used only for diagnostics
            # in stdin mode.
            wm_id_hex = stardust.extract_blind(
                str(media.init),
                wm_bit_profile=wm_bit_profile,
                config=config,
                stdin_bytes=stdin_bytes,
            )
        else:
            wm_id_hex = stardust.extract_blind(
                str(input_path),
                wm_bit_profile=wm_bit_profile,
                config=config,
            )
    except Exception as exc:
        timings["blind_extract_s"] = time.perf_counter() - step_start
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=2,
            error=f"Blind extraction raised: {exc}",
            timings=timings,
        )
    timings["blind_extract_s"] = time.perf_counter() - step_start

    if wm_id_hex is None:
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=2,
            error=(
                f"Blind extraction returned no watermark id from {input_path}. "
                "The asset may not carry a Stardust watermark or the "
                "configured Stardust parameters do not match the embed."
            ),
            timings=timings,
        )
    wm_id_hex = wm_id_hex.lower()

    # Step 2: locate the detached manifest in the store.
    manifest_path = manifest_store / f"{wm_id_hex}.c2pa"
    if not manifest_path.is_file():
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=3,
            wm_id_hex=wm_id_hex,
            error=(
                f"No manifest found in store for wm_id={wm_id_hex!r}: "
                f"{manifest_path} does not exist."
            ),
            timings=timings,
        )

    # Step 3: c2patool verification against the detached manifest.
    try:
        from stardustproof_c2pa_signer.c2patool import (
            extract_validation_results,
            find_soft_binding,
            verify_detached_manifest,
            write_cawg_trust_settings,
        )
    except Exception as exc:
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=1,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            error=(
                "stardustproof_c2pa_signer.c2patool is unavailable. "
                f"Install the signer package to enable verify: {exc}"
            ),
            timings=timings,
        )

    step_start = time.perf_counter()
    # Compute c2patool args that depend on media shape. For segmented
    # inputs we pass the init segment as the positional asset and the
    # fragment file set via --fragments_glob. For SingleFile and
    # SingleFileFragmented we pass the file itself; c2patool's
    # verify_stream_hash handles single-file fragmented MP4 internally.
    if isinstance(media, Segmented):
        c2patool_asset = media.init
        # c2patool expects the glob pattern relative to the init's
        # directory (see its --fragments_glob docs). Derive a pattern
        # that matches every fragment in the set while excluding the
        # init itself. We use a conservative glob based on the
        # resolver's sorted filename list.
        c2patool_fragments_glob: Optional[Path] = _derive_fragments_glob(media)
    else:
        c2patool_asset = input_path
        c2patool_fragments_glob = None
    with tempfile.TemporaryDirectory(prefix="stardustproof-verify-") as tmp:
        tmp_dir = Path(tmp)

        if check_trust:
            # trust_anchors and cawg_trust_anchors are resolved by this
            # point (non-None), asserted by the early check_trust branch.
            assert trust_anchors is not None
            assert cawg_trust_anchors is not None
            trust_bundle = _write_trust_bundle(
                trust_anchors, tmp_dir, "trust_anchors.pem"
            )
            cawg_bundle = (
                trust_bundle
                if cawg_trust_anchors == trust_anchors
                else _write_trust_bundle(
                    cawg_trust_anchors, tmp_dir, "cawg_trust_anchors.pem"
                )
            )
            settings_path = write_cawg_trust_settings(
                tmp_dir / "c2patool_settings.toml",
                trust_anchors_pem=cawg_bundle.read_text(),
            )
            result = verify_detached_manifest(
                asset_path=c2patool_asset,
                manifest_path=manifest_path,
                trust_anchors=trust_bundle,
                settings_path=settings_path,
                fragments_glob=c2patool_fragments_glob,
                detailed=True,
            )
        else:
            result = verify_detached_manifest(
                asset_path=c2patool_asset,
                manifest_path=manifest_path,
                fragments_glob=c2patool_fragments_glob,
                detailed=True,
            )
    timings["c2patool_s"] = time.perf_counter() - step_start

    if result.returncode != 0 or result.report is None:
        timings["total_s"] = time.perf_counter() - t0
        err = (
            f"c2patool exited {result.returncode}."
            if result.returncode != 0
            else "c2patool did not emit parseable JSON on stdout."
        )
        if result.stderr:
            err += f"\nstderr: {result.stderr.strip()}"
        return VerifyResult(
            ok=False, exit_code=4,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            error=err,
            timings=timings,
            report=result.report,
        )

    report = result.report
    validation_state = report.get("validation_state") if isinstance(report, dict) else None

    # Step 4: locate the Stardust soft-binding and cross-check value.
    sb = find_soft_binding(report, alg=WATERMARK_ALG)
    if sb is None:
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=5,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            validation_state=validation_state,
            error=(
                f"Manifest contains no c2pa.soft-binding with alg={WATERMARK_ALG!r}."
            ),
            timings=timings,
            report=report,
        )
    sb_value_raw = sb.get("data", {}).get("value") if isinstance(sb, dict) else None
    if not isinstance(sb_value_raw, str):
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=5,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            soft_binding=sb,
            validation_state=validation_state,
            error=f"Soft-binding assertion missing .data.value: {sb!r}",
            timings=timings,
            report=report,
        )
    if sb_value_raw.lower() != wm_id_hex:
        timings["total_s"] = time.perf_counter() - t0
        return VerifyResult(
            ok=False, exit_code=5,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            soft_binding=sb,
            validation_state=validation_state,
            error=(
                f"Soft-binding value {sb_value_raw!r} does not match the "
                f"blind-extracted watermark id {wm_id_hex!r}."
            ),
            timings=timings,
            report=report,
        )

    # Step 5: check c2patool validation results.
    vr = extract_validation_results(report)
    timings["total_s"] = time.perf_counter() - t0
    if vr["failure"]:
        return VerifyResult(
            ok=False, exit_code=6,
            wm_id_hex=wm_id_hex,
            manifest_path=manifest_path,
            soft_binding=sb,
            validation_state=validation_state,
            success=vr["success"],
            failure=vr["failure"],
            informational=vr["informational"],
            error=(
                f"c2patool reported {len(vr['failure'])} validation "
                f"failure(s); see .failure for details."
            ),
            timings=timings,
            report=report,
        )

    return VerifyResult(
        ok=True, exit_code=0,
        wm_id_hex=wm_id_hex,
        manifest_path=manifest_path,
        soft_binding=sb,
        validation_state=validation_state,
        success=vr["success"],
        failure=vr["failure"],
        informational=vr["informational"],
        timings=timings,
        report=report,
    )


def render_human(result: VerifyResult, *, input_path: Path) -> str:
    """Format a :class:`VerifyResult` as the default human-readable
    multi-line string rendered by ``stardustproof verify``."""

    lines: list[str] = []
    lines.append(f"[verify] Input: {input_path}")

    be = result.timings.get("blind_extract_s")
    if result.wm_id_hex is not None:
        if be is not None:
            lines.append(
                f"[verify] Blind extraction: {result.wm_id_hex} ({be:.2f}s)"
            )
        else:
            lines.append(f"[verify] Blind extraction: {result.wm_id_hex}")
    elif be is not None:
        lines.append(f"[verify] Blind extraction: FAILED ({be:.2f}s)")

    if result.manifest_path is not None:
        lines.append(f"[verify] Manifest: {result.manifest_path}")

    cv = result.timings.get("c2patool_s")
    if result.report is not None:
        lines.append(
            f"[verify] c2patool verification: "
            f"{len(result.success)} success, "
            f"{len(result.informational)} informational, "
            f"{len(result.failure)} failures"
            + (f" ({cv:.2f}s)" if cv is not None else "")
        )
    if result.soft_binding is not None:
        sb_value = result.soft_binding.get("data", {}).get("value")
        lines.append(
            f"[verify] Soft-binding: alg={WATERMARK_ALG} value={sb_value}"
        )
    if result.validation_state is not None:
        lines.append(f"[verify] validation_state: {result.validation_state}")

    total = result.timings.get("total_s")
    if total is not None:
        lines.append(f"[verify] total: {total:.2f}s")

    lines.append("")
    if result.ok:
        lines.append(
            "OK: asset + manifest verify cleanly against watermark and trust anchors."
        )
    else:
        lines.append(
            f"FAIL (exit {result.exit_code}): {result.error or 'verification failed'}"
        )
        for f in result.failure:
            code = f.get("code", "?")
            url = f.get("url", "")
            explanation = f.get("explanation", "")
            lines.append(f"  - {code}: {explanation} ({url})")
    return "\n".join(lines)
