from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import tomllib


CLI_BUNDLED_WHEEL_PATHS = {
    "stardustproof_cli/_bundled/bin/stardust/sffw-embed",
    "stardustproof_cli/_bundled/bin/stardust/extract",
    "stardustproof_cli/_bundled/bin/ffmpeg/bin/ffmpeg",
    "stardustproof_cli/_bundled/bin/ffmpeg/bin/ffprobe",
    "stardustproof_cli/_bundled/stardust_defaults.json",
    "stardustproof_cli/certs/castlabs_c2pa_ca.cert.pem",
    "stardustproof_cli/certs/trusted_publisher_ca.cert.pem",
}

REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a shippable Linux x86_64 StardustProof CLI wheelhouse zip."
    )
    parser.add_argument(
        "--output-dir",
        default="dist/release",
        help="Directory that will receive the release artifact and checksums.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used for wheel builds and validation.",
    )
    parser.add_argument(
        "--artifact-version",
        default=None,
        help="Artifact version override. Defaults to the CLI package version.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip offline install validation of the built release zip.",
    )
    return parser.parse_args()


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)


def read_pyproject(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def requirement_name(requirement: str) -> str | None:
    match = REQUIREMENT_NAME_RE.match(requirement)
    if not match:
        return None
    return normalize_name(match.group(1))


def should_replace_requirement(current: str, candidate: str) -> bool:
    current_has_spec = any(op in current for op in ("<", ">", "=", "~", "!"))
    candidate_has_spec = any(op in candidate for op in ("<", ">", "=", "~", "!"))
    if candidate_has_spec and not current_has_spec:
        return True
    return len(candidate) > len(current)


def collect_named_dependencies(pyproject_paths: Iterable[Path], local_packages: set[str]) -> list[str]:
    requirements: dict[str, str] = {}
    for pyproject_path in pyproject_paths:
        project = read_pyproject(pyproject_path)["project"]
        for raw_req in project.get("dependencies", []):
            if " @ " in raw_req:
                continue
            name = requirement_name(raw_req)
            if name is None or name in local_packages:
                continue
            current = requirements.get(name)
            if current is None or should_replace_requirement(current, raw_req):
                requirements[name] = raw_req
    return [requirements[name] for name in sorted(requirements)]


def git_output(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def resolve_source_checkout(
    repo_root: Path,
    tmp_sources_dir: Path,
    package_name: str,
    source_spec: dict,
) -> Path:
    sibling_path = (repo_root / source_spec["sibling_path"]).resolve()
    expected_commit = source_spec["commit"]

    if sibling_path.is_dir():
        try:
            sibling_commit = git_output(sibling_path, "rev-parse", "HEAD")
            if sibling_commit == expected_commit:
                return sibling_path
        except subprocess.CalledProcessError:
            pass

    checkout_dir = tmp_sources_dir / package_name
    run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--recurse-submodules",
            source_spec["repo_url"],
            str(checkout_dir),
        ]
    )
    run(["git", "checkout", expected_commit], cwd=checkout_dir)
    run(["git", "submodule", "update", "--init", "--recursive"], cwd=checkout_dir)
    return checkout_dir


def build_wheel(python_bin: str, source_dir: Path, wheelhouse_dir: Path) -> Path:
    existing = {p.name for p in wheelhouse_dir.glob("*.whl")}
    run(
        [python_bin, "-m", "build", "--wheel", "--outdir", str(wheelhouse_dir), str(source_dir)],
        cwd=wheelhouse_dir.parent,
    )
    created = [p for p in wheelhouse_dir.glob("*.whl") if p.name not in existing]
    if len(created) != 1:
        raise RuntimeError(f"Expected exactly one new wheel from {source_dir}, found {len(created)}")
    return created[0]


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cli_wheel_contains_bundled_assets(cli_wheel: Path) -> None:
    with zipfile.ZipFile(cli_wheel) as zf:
        names = set(zf.namelist())
    missing = sorted(CLI_BUNDLED_WHEEL_PATHS - names)
    if missing:
        raise RuntimeError(
            "CLI wheel is missing bundled runtime assets:\n  - " + "\n  - ".join(missing)
        )


def render_checksum_lines(paths: Iterable[Path]) -> str:
    return "".join(f"{sha256_file(path)}  {path.name}\n" for path in paths)


def validate_release_zip(python_bin: str, zip_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="stardustproof-release-validate-") as tmp:
        tmp_dir = Path(tmp)
        unpack_dir = tmp_dir / "unpacked"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unpack_dir)

        roots = [p for p in unpack_dir.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(f"Expected one extracted release directory, found {len(roots)}")
        release_root = roots[0]
        install_script = release_root / "install.sh"

        run(["chmod", "+x", str(install_script)])
        run(["bash", str(install_script), "--python", python_bin], cwd=release_root)
        venv_python = release_root / ".venv" / "bin" / "python"
        venv_cli = release_root / ".venv" / "bin" / "stardustproof"

        run([str(venv_cli), "--help"], cwd=release_root)
        run([str(venv_cli), "verify", "--help"], cwd=release_root)
        run(
            [
                str(venv_python),
                "-c",
                (
                    "from stardustproof_cli.config import StardustPaths; "
                    "from stardustproof_cli.verify import resolve_default_trust_anchor_paths; "
                    "paths = StardustPaths().resolve(); "
                    "missing = paths.check_binaries(); "
                    "assert not missing, missing; "
                    "anchors = resolve_default_trust_anchor_paths(); "
                    "assert anchors and all(p.is_file() for p in anchors);"
                ),
            ],
            cwd=release_root,
        )


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cli_pyproject = repo_root / "pyproject.toml"
    cli_project = read_pyproject(cli_pyproject)["project"]
    artifact_version = args.artifact_version or cli_project["version"]
    artifact_base = f"stardustproof-wheelhouse-linux-x86_64-{artifact_version}"
    artifact_root = output_dir / artifact_base
    zip_path = output_dir / f"{artifact_base}.zip"
    checksum_path = output_dir / "SHA256SUMS"

    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    if zip_path.exists():
        zip_path.unlink()

    wheelhouse_dir = artifact_root / "wheelhouse"
    wheelhouse_dir.mkdir(parents=True)

    release_dir = repo_root / "release"
    source_lock = json.loads((release_dir / "sources.lock.json").read_text())

    with tempfile.TemporaryDirectory(prefix="stardustproof-release-build-") as tmp:
        tmp_dir = Path(tmp)
        sources_dir = tmp_dir / "sources"
        sources_dir.mkdir()

        signer_source = resolve_source_checkout(
            repo_root, sources_dir, "stardustproof-c2pa-signer", source_lock["sources"]["stardustproof-c2pa-signer"]
        )
        c2pa_source = resolve_source_checkout(
            repo_root, sources_dir, "c2pa-python", source_lock["sources"]["c2pa-python"]
        )
        keystore_source = resolve_source_checkout(
            repo_root, sources_dir, "stardustproof-keystore", source_lock["sources"]["stardustproof-keystore"]
        )

        local_sources = {
            "c2pa-python": c2pa_source,
            "stardustproof-keystore": keystore_source,
            "stardustproof-c2pa-signer": signer_source,
            "stardustproof-cli": repo_root,
        }

        local_packages = {normalize_name(name) for name in local_sources}
        dependency_requirements = collect_named_dependencies(
            [
                c2pa_source / "pyproject.toml",
                keystore_source / "pyproject.toml",
                signer_source / "pyproject.toml",
                cli_pyproject,
            ],
            local_packages,
        )
        requirements_path = wheelhouse_dir / "requirements-offline.txt"
        requirements_path.write_text("\n".join(dependency_requirements) + "\n")

        run(
            [
                args.python,
                "-m",
                "pip",
                "wheel",
                "--wheel-dir",
                str(wheelhouse_dir),
                "-r",
                str(requirements_path),
            ]
        )

        built_wheels = {
            name: build_wheel(args.python, source_dir, wheelhouse_dir)
            for name, source_dir in local_sources.items()
        }
        ensure_cli_wheel_contains_bundled_assets(built_wheels["stardustproof-cli"])

        shutil.copy2(release_dir / "install.sh", artifact_root / "install.sh")
        shutil.copy2(release_dir / "INSTALL.md", artifact_root / "INSTALL.md")

        manifest = {
            "artifact": {
                "name": artifact_base,
                "platform": "linux-x86_64",
                "version": artifact_version,
            },
            "source_lock": source_lock,
            "dependency_requirements": dependency_requirements,
            "built_wheels": {name: path.name for name, path in built_wheels.items()},
            "wheelhouse": sorted(path.name for path in wheelhouse_dir.glob("*.whl")),
        }
        write_json(artifact_root / "RELEASE-MANIFEST.json", manifest)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(artifact_root.rglob("*")):
            if path.is_dir():
                continue
            zf.write(path, arcname=str(path.relative_to(output_dir)))

    if not args.skip_validate:
        validate_release_zip(args.python, zip_path)

    checksum_path.write_text(render_checksum_lines([zip_path]))

    print(f"Built release artifact: {zip_path}")
    print(f"Wrote checksums: {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
