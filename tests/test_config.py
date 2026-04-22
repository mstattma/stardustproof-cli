from pathlib import Path

from stardustproof_cli.config import StardustPaths


def test_stardust_paths_resolve_prefers_existing_layout(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    (bin_dir / "stardust").mkdir(parents=True)
    (bin_dir / "ffmpeg" / "bin").mkdir(parents=True)
    for tool in ("sffw-embed", "extract"):
        (bin_dir / "stardust" / tool).write_bytes(b"")
    for tool in ("ffmpeg", "ffprobe"):
        (bin_dir / "ffmpeg" / "bin" / tool).write_bytes(b"")

    resolved = StardustPaths(repo_root=tmp_path).resolve()

    assert resolved.bin_dir == bin_dir
    assert resolved.stardust_embed == bin_dir / "stardust" / "sffw-embed"
    assert resolved.stardust_extract == bin_dir / "stardust" / "extract"
    assert resolved.ffmpeg == bin_dir / "ffmpeg" / "bin" / "ffmpeg"
    assert resolved.ffprobe == bin_dir / "ffmpeg" / "bin" / "ffprobe"


def test_stardust_paths_check_binaries_reports_missing(tmp_path: Path):
    paths = StardustPaths(custom_bin_dir=tmp_path)
    missing = paths.check_binaries()
    names = " ".join(missing)
    assert "sffw-embed" in names
    assert "extract" in names
    assert "ffmpeg" in names
    assert "ffprobe" in names
