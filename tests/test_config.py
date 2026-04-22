from pathlib import Path

from stardustproof_cli.config import StardustPaths


def test_stardust_paths_resolve_prefers_existing_bin_dir(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sffw-embed").write_bytes(b"")
    (bin_dir / "extract").write_bytes(b"")

    resolved = StardustPaths(repo_root=tmp_path).resolve()

    assert resolved.bin_dir == bin_dir
