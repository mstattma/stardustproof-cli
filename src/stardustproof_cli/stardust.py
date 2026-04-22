from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from stardustproof_cli.config import StardustConfig, StardustPaths


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {result.stderr}")
    return result


def _image_dimensions(image_path: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            image_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {image_path}: {result.stderr}")
    width, height = result.stdout.strip().split(",")
    return int(width), int(height)


def check_binaries(paths: StardustPaths) -> list[str]:
    missing = paths.check_binaries()
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg (not in PATH)")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe (not in PATH)")
    return missing


def embed(image_path: str, output_path: str, wm_id_hex: str, config: StardustConfig) -> str:
    paths = config.paths
    width, height = _image_dimensions(image_path)
    bit_profile = len(wm_id_hex) * 4

    with tempfile.TemporaryDirectory() as tmp:
        cover_yuv = os.path.join(tmp, "cover.yuv")
        embedded_yuv = os.path.join(tmp, "embedded.yuv")

        _run(["ffmpeg", "-y", "-i", image_path, "-pix_fmt", "yuv420p", "-f", "rawvideo", cover_yuv])
        _run(
            [
                str(paths.stardust_embed),
                "--input-file",
                cover_yuv,
                "--output-file",
                embedded_yuv,
                "--strength",
                str(config.stardust_strength),
                "--sp-width",
                str(config.stardust_sp_width),
                "--sp-height",
                str(config.stardust_sp_height),
                "--sp-density",
                str(config.stardust_sp_density),
                "--p-density",
                str(config.stardust_p_density),
                "--pm-mode",
                str(config.stardust_pm_mode),
                "--bit-profile",
                str(bit_profile),
                "--wm-id",
                wm_id_hex,
                "--width",
                str(width),
                "--height",
                str(height),
                "--pix-fmt",
                "yuv420p",
                "--seed",
                str(config.stardust_seed),
                "--fec",
                str(config.stardust_fec),
            ]
        )
        _run([
            "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}", "-i", embedded_yuv,
            "-frames:v", "1", output_path,
        ])

        base = os.path.splitext(output_path)[0]
        shutil.copy2(cover_yuv, f"{base}.reference.yuv")
        Path(f"{base}.stardust_meta").write_text(f"{width} {height}\n")
    return output_path
