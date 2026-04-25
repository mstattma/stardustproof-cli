"""Verification pipeline for StardustProof-watermarked assets.

The :func:`verify_asset` function blind-extracts the Stardust watermark
from the input asset, looks up the corresponding detached C2PA manifest
in a local manifest-store directory, shells out to the bundled
``c2patool`` to validate the manifest against the asset, and asserts
that the ``c2pa.soft-binding`` assertion inside the manifest matches
the extracted watermark id. This is the productized counterpart to the
verification path in the integration smoke.

For user-flow manifests (where an ICA-issued
``cawg.identity_claims_aggregation`` assertion is present alongside
the publisher's ``cawg.x509.cose`` assertion), an additional
**ICA Human Identity Binding check** runs after the c2patool step
(see :mod:`stardustproof_c2pa_signer.ica_binding`). It cryptographically
proves that the publisher's CAWG private key signs over the ICA VC's
content (CAWG nested-identity-assertion pattern, §1.4 Example 3 /
§5.1.1) AND that the publisher leaf cert SPKI matches both the VC's
``stardustproof:activeDidAssertionMethodKey`` shortcut field and the
user DID document's ``assertionMethod`` keys (RFC 7638 JWK
thumbprints).

Exit-code contract (returned via :class:`VerifyResult.exit_code`):

====  =============================================================
Code  Meaning
====  =============================================================
0     All checks passed (including ICA binding when present), or
      the manifest is org-flow / Simple Sign with no ICA binding
      to enforce.
1     Argument / IO / binary / trust-material error.
2     Blind extraction failed (no Stardust watermark detected).
3     Manifest not found in store for the extracted watermark id.
4     ``c2patool`` invocation failed (non-zero exit or unparseable
      JSON).
5     Soft-binding value in the manifest does not match the
      extracted watermark id.
6     ``c2patool`` reported one or more validation failures
      (e.g. hash mismatch, untrusted cert, BMFF hash failure).
7     ICA Human Identity Binding check failed -- a manifest with an
      ICA assertion did not satisfy one of the 7 binding sub-rows.
      The manifest is otherwise valid (claim signature + publisher
      cert chain + soft-binding all pass), but the publisher's
      private-key control of the ICA-attested human identity could
      not be cryptographically verified. Pass
      ``--tolerate-ica-binding`` (or ``tolerate_ica_binding=True``)
      to downgrade this to exit 0 with ``trust_tier="publisher_only"``.
====  =============================================================

Trust-tier output (``VerifyResult.trust_tier``):

* ``"publisher_and_human"`` -- all checks pass AND the ICA binding
  check's 7 rows all pass.
* ``"publisher_only"`` -- claim signature + soft-binding + cert
  chain all pass, but either no ICA assertion is present (org /
  Simple Sign flow) or the binding check failed.
* ``"untrusted"`` -- one or more c2patool failures, or an earlier
  pipeline error.
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
from stardustproof_c2pa_signer import (
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

_PACKAGED_TRUST_PEM_NAMES = (
    "castlabs_c2pa_ca.cert.pem",
    "trusted_publisher_ca.cert.pem",
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

    # ICA Human Identity Binding result (user-flow only).
    # ``None`` means the binding check was not run (e.g. earlier
    # pipeline failure, or the manifest has no ICA assertion to
    # check). When populated, the dict mirrors
    # ``IcaBindingCheckResult.to_dict()`` from the signer library.
    ica_binding: Optional[dict] = None

    # Trust tier surfaced to the operator. Always populated on a
    # successful c2patool run, regardless of the exit code policy
    # for ICA binding failures, so machine consumers can do their
    # own gating.
    #
    # * ``"publisher_and_human"`` -- all checks pass AND the ICA
    #   binding check's 7 rows all pass.
    # * ``"publisher_only"`` -- claim signature + soft-binding +
    #   cert chain pass, but either no ICA assertion is present
    #   (org / Simple Sign flow) or the binding check failed.
    # * ``"untrusted"`` -- one or more c2patool failures, or an
    #   earlier pipeline error (the default for new instances).
    trust_tier: str = "untrusted"

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
            "ica_binding": self.ica_binding,
            "trust_tier": self.trust_tier,
        }


def resolve_default_trust_anchor_paths() -> Optional[list[Path]]:
    """Locate the StardustProof keystore CA PEMs shipped by the signer
    package's ``keystore/`` submodule, or from this CLI package.

    Returns the list of PEM paths on success, or ``None`` if no set of
    candidates contains all required files.

    Discovery order:

    1. This CLI package's bundled CA PEMs under ``stardustproof_cli/certs``.
    2. Editable install: ``<signer-repo>/keystore/certs/…`` (two levels
        above the installed ``stardustproof_c2pa_signer`` package).
    3. site-packages install with ``keystore/`` copied next to the
        package.
    4. Sibling checkout: ``<cli-repo>/../stardustproof-c2pa-signer-vibe``.
    """

    packaged = [
        Path(__file__).resolve().parent / "certs" / name
        for name in _PACKAGED_TRUST_PEM_NAMES
    ]
    if all(p.is_file() for p in packaged):
        return packaged

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
    tolerate_ica_binding: bool = False,
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
        tolerate_ica_binding: When ``True``, an ICA Human Identity
            Binding failure on a user-flow manifest downgrades to
            ``trust_tier="publisher_only"`` and exit 0 instead of the
            default exit 7. Org-flow / Simple Sign manifests (no ICA
            assertion present) always exit 0 regardless of this flag.

    Returns:
        A :class:`VerifyResult` with ``ok``, ``exit_code``,
        ``trust_tier``, ``ica_binding``, and diagnostic fields
        populated.
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
    if vr["failure"]:
        timings["total_s"] = time.perf_counter() - t0
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
            trust_tier="untrusted",
        )

    # Step 6: ICA Human Identity Binding (user-flow manifests only).
    #
    # When a manifest carries an ICA `cawg.identity_claims_aggregation`
    # assertion alongside the publisher's `cawg.x509.cose` assertion,
    # the publisher's COSE_Sign1 cryptographically endorses the ICA
    # VC's content via referenced_assertions[] (CAWG nested-identity
    # pattern, §1.4 Example 3 / §5.1.1). Plus, the publisher leaf cert
    # SPKI is cross-checked against the VC's
    # `stardustproof:activeDidAssertionMethodKey` shortcut field and
    # the user DID document's assertionMethod[*] keys. See
    # stardustproof_c2pa_signer.ica_binding for the canonical 7-row
    # implementation.
    #
    # When no ICA assertion is present (org-publisher / Simple Sign),
    # the check is skipped: trust_tier is "publisher_only" and exit 0
    # (no human-identity claim was made; nothing to verify).
    binding_step_start = time.perf_counter()
    binding_result, binding_error = _run_ica_binding_check(
        manifest_path=manifest_path,
        c2patool_success=vr["success"],
    )
    timings["ica_binding_s"] = time.perf_counter() - binding_step_start
    timings["total_s"] = time.perf_counter() - t0

    # Decide trust_tier + exit_code.
    #
    # Three possibilities for the ICA binding outcome:
    #   - binding_result is None and binding_error is None
    #     -> the manifest has no ICA assertion (org / Simple Sign).
    #        trust_tier="publisher_only", exit 0.
    #   - binding_result is not None and binding_result["ok"] is True
    #     -> the user-flow manifest passed the 7-row check.
    #        trust_tier="publisher_and_human", exit 0.
    #   - binding_result is not None and binding_result["ok"] is False,
    #     OR binding_error is set
    #     -> a user-flow manifest has an ICA assertion but the
    #        binding does not hold. trust_tier="publisher_only";
    #        exit 7 by default, exit 0 when tolerate_ica_binding=True.
    if binding_result is None and binding_error is None:
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
            ica_binding=None,
            trust_tier="publisher_only",
        )

    if binding_result is not None and binding_result.get("ok"):
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
            ica_binding=binding_result,
            trust_tier="publisher_and_human",
        )

    # ICA binding failed (or the check itself errored on a user-flow
    # manifest). Verdict tier drops to "publisher_only".
    if tolerate_ica_binding:
        downgraded_exit = 0
        downgraded_ok = True
        downgraded_error = None
    else:
        downgraded_exit = 7
        downgraded_ok = False
        if binding_error is not None:
            downgraded_error = (
                f"ICA Human Identity Binding check could not complete: "
                f"{binding_error}"
            )
        else:
            failed_rows = [
                row for row, ok in (binding_result or {}).get("rows", {}).items()
                if ok is False
            ]
            failure_code = (binding_result or {}).get("failure_code")
            downgraded_error = (
                f"ICA Human Identity Binding failed "
                f"(failure_code={failure_code!r}, "
                f"failed_rows={failed_rows!r}). Pass --tolerate-ica-binding "
                f"to downgrade to trust_tier=publisher_only with exit 0."
            )

    return VerifyResult(
        ok=downgraded_ok, exit_code=downgraded_exit,
        wm_id_hex=wm_id_hex,
        manifest_path=manifest_path,
        soft_binding=sb,
        validation_state=validation_state,
        success=vr["success"],
        failure=vr["failure"],
        informational=vr["informational"],
        timings=timings,
        report=report,
        ica_binding=binding_result,
        trust_tier="publisher_only",
        error=downgraded_error,
    )


def _run_ica_binding_check(
    *,
    manifest_path: Path,
    c2patool_success: list[dict],
) -> tuple[Optional[dict], Optional[str]]:
    """Run the ICA Human Identity Binding check against a detached
    manifest, mediating between c2patool's validation report and the
    signer library's :func:`verify_ica_human_identity_binding`.

    Args:
        manifest_path: Detached ``<wm-id>.c2pa`` manifest store entry
            that c2patool just validated successfully.
        c2patool_success: c2patool's structured ``success`` list (for
            extracting the per-assertion ``cawg.ica.credential_valid``
            signal that becomes the binding check's row 4 input).

    Returns:
        ``(binding_result, error)`` where:

        * ``binding_result`` is a dict (mirroring
          :class:`stardustproof_c2pa_signer.ica_binding.IcaBindingCheckResult.to_dict`)
          when the manifest has an ICA assertion and the check
          completed (regardless of pass/fail).
        * ``binding_result`` is ``None`` when the manifest has no ICA
          assertion (org / Simple Sign flow); the binding check is
          inapplicable and we report it as such by returning
          ``(None, None)``.
        * ``error`` is a non-empty string only when the binding check
          itself raised (e.g. JUMBF parse error, missing signer
          library). In that case ``binding_result`` is ``None`` too.
    """
    try:
        from stardustproof_c2pa_signer import (
            extract_publisher_identity_leaf_cert_der_from_manifest_bytes,
            verify_ica_human_identity_binding,
        )
    except ImportError as exc:
        # The binding check requires the signer library's [validate]
        # extra (cbor2 + cryptography). When absent we cannot
        # distinguish "no ICA assertion present" from "we just cannot
        # check it"; treat as a hard error so the operator sees the
        # missing dependency.
        return None, f"signer library binding API unavailable: {exc}"

    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        return None, f"cannot read manifest store entry: {exc}"

    # Map c2patool's per-assertion success report to the ICA signature
    # validity boolean. c2patool emits `cawg.ica.credential_valid`
    # (structure + COSE_Sign1 verify) for valid VCs. We treat presence
    # of that code as the green signal and absence as red.
    ica_signature_valid = any(
        isinstance(s, dict)
        and isinstance(s.get("code"), str)
        and s["code"].startswith("cawg.ica.credential_valid")
        for s in c2patool_success
    )

    # Attempt the binding check FIRST so its row 1 ("ICA assertion
    # present") tells us whether we even need a publisher cert.
    # Pass a placeholder cert DER that the row 5/6 logic will catch
    # cleanly via "publisher_cert_unparseable" if it is needed --
    # but row 1 returning False short-circuits before any cert work.
    leaf_result = extract_publisher_identity_leaf_cert_der_from_manifest_bytes(
        manifest_bytes
    )
    publisher_cert_der = leaf_result.cert_der or b""

    try:
        binding = verify_ica_human_identity_binding(
            manifest_bytes=manifest_bytes,
            publisher_leaf_cert_der=publisher_cert_der,
            ica_signature_valid=ica_signature_valid,
        )
    except Exception as exc:
        return None, f"binding check raised: {exc}"

    binding_dict = binding.to_dict()
    rows = binding_dict.get("rows", {}) or {}
    failure_code = binding_dict.get("failure_code")

    # Distinguish "no ICA assertion present" from a real failure of
    # row 1. The check itself sets row1 to False when no assertion
    # is found AND populates failure_code="ica_assertion_missing".
    # In our pipeline that means org-publisher / Simple Sign and we
    # want to surface it as "skipped".
    if rows.get("row1_ica_assertion_present") is False and failure_code == "ica_assertion_missing":
        return None, None

    # Row 1 says an ICA assertion IS present but we could not extract
    # the publisher leaf cert. The binding cannot proceed; surface as
    # an explicit error rather than an opaque row 5/6 failure.
    if leaf_result.cert_der is None:
        return None, (
            f"publisher identity assertion not found or malformed in "
            f"manifest while ICA assertion is present: "
            f"{leaf_result.reason or leaf_result.reason_code}"
        )

    return binding_dict, None


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

    # ICA Human Identity Binding -- surface the trust tier and the
    # per-row breakdown so operators can see WHY the tier dropped.
    binding_elapsed = result.timings.get("ica_binding_s")
    if result.ica_binding is None:
        if result.trust_tier == "publisher_only":
            lines.append(
                "[verify] ICA Human Identity Binding: SKIPPED "
                "(no cawg.identity_claims_aggregation assertion -- "
                "org publisher / Simple Sign flow)"
            )
    else:
        elapsed_str = (
            f" ({binding_elapsed:.2f}s)" if binding_elapsed is not None else ""
        )
        if result.trust_tier == "publisher_and_human":
            tier_label = "PUBLISHER_AND_HUMAN"
        else:
            tier_label = "PUBLISHER_ONLY (verdict tier dropped)"
        lines.append(
            f"[verify] ICA Human Identity Binding: {tier_label}{elapsed_str}"
        )
        rows = result.ica_binding.get("rows", {}) or {}
        row_codes = result.ica_binding.get("row_codes", {}) or {}
        row_labels = [
            ("row1_ica_assertion_present", "Row 1 ICA assertion present"),
            ("row2_publisher_references_ica", "Row 2 publisher references ICA"),
            ("row3_referenced_hash_matches", "Row 3 referenced hash matches"),
            ("row4_ica_signature_valid", "Row 4 ICA signature valid"),
            ("row5_shortcut_consistent", "Row 5 shortcut field consistent"),
            ("row6_did_assertion_method_match", "Row 6 DID assertionMethod match"),
            ("row7_verified_identities_present", "Row 7 verified identities present"),
        ]
        for key, label in row_labels:
            ok_val = rows.get(key)
            row_short = "row" + key.split("_", 1)[0][3:] if key.startswith("row") else key
            code = row_codes.get(row_short)
            if ok_val is True:
                glyph = "+"
                detail = ""
            elif ok_val is False:
                glyph = "-"
                detail = f" (failure code: {code})" if code else ""
            else:
                glyph = "?"
                detail = " (not evaluated)"
            lines.append(f"  [{glyph}] {label}{detail}")
    if result.trust_tier and result.trust_tier != "untrusted":
        lines.append(f"[verify] trust_tier: {result.trust_tier}")

    total = result.timings.get("total_s")
    if total is not None:
        lines.append(f"[verify] total: {total:.2f}s")

    lines.append("")
    if result.ok:
        if result.trust_tier == "publisher_and_human":
            lines.append(
                "OK: asset + manifest verify cleanly against watermark and "
                "trust anchors, and the ICA Human Identity Binding holds."
            )
        elif result.trust_tier == "publisher_only" and result.ica_binding is None:
            lines.append(
                "OK: asset + manifest verify cleanly against watermark and "
                "trust anchors. No ICA human-identity assertion present "
                "(org publisher / Simple Sign flow)."
            )
        elif result.trust_tier == "publisher_only":
            # Reachable only via --tolerate-ica-binding.
            lines.append(
                "OK (downgraded): asset + manifest verify cleanly, but the "
                "ICA Human Identity Binding check did not pass. "
                "trust_tier dropped to 'publisher_only'. Re-run without "
                "--tolerate-ica-binding to fail loudly (exit 7) instead."
            )
        else:
            lines.append(
                "OK: asset + manifest verify cleanly against watermark and "
                "trust anchors."
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
