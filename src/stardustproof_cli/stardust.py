from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from stardustproof_cli.config import StardustConfig, StardustPaths


@dataclass
class MediaInfo:
    width: int
    height: int
    frame_rate: str | None
    has_audio: bool
    media_kind: str


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def _is_verbose() -> bool:
    return os.environ.get("STARDUSTPROOF_VERBOSE", "").lower() in {"1", "true", "yes", "on"}


def _log(message: str) -> None:
    print(f"[stardust] {message}", flush=True)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    verbose = kwargs.pop("verbose", _is_verbose())
    if verbose:
        _log(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, text=True, **kwargs)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        stderr = getattr(result, "stderr", "") or ""
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {stderr}")
    return result


def probe_media(path: str, paths: StardustPaths) -> MediaInfo:
    """Probe media dimensions and audio presence using the bundled ffprobe."""
    result = subprocess.run(
        [
            str(paths.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError(f"No video stream found in {path}")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    ext = Path(path).suffix.lower()
    if ext in _VIDEO_EXTS:
        media_kind = "video"
    elif ext in _IMAGE_EXTS:
        media_kind = "image"
    else:
        media_kind = "video" if has_audio else "image"
    return MediaInfo(
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        frame_rate=video_stream.get("avg_frame_rate") or None,
        has_audio=has_audio,
        media_kind=media_kind,
    )


def check_binaries(paths: StardustPaths) -> list[str]:
    return paths.check_binaries()


def _generate_payload(
    pp_path: str,
    wm_id_hex: str,
    media: MediaInfo,
    config: StardustConfig,
) -> None:
    """Generate a Stardust .pp payload file.

    The payload encodes width, height, sp_width, sp_height, sp_density,
    p_density, pm_mode, strength, fec, bit_profile and wm-id. The FFmpeg
    filter then consumes this payload as its ground truth.
    """
    bit_profile = len(wm_id_hex) * 4
    cmd = [
        str(config.paths.stardust_embed),
        "--payload-file", pp_path,
        "--strength", str(config.stardust_strength),
        "--sp-width", str(config.stardust_sp_width),
        "--sp-height", str(config.stardust_sp_height),
        "--sp-density", str(config.stardust_sp_density),
        "--p-density", str(config.stardust_p_density),
        "--pm-mode", str(config.stardust_pm_mode),
        "--bit-profile", str(bit_profile),
        "--wm-id", wm_id_hex,
        "--width", str(media.width),
        "--height", str(media.height),
        "--pix-fmt", "yuv420p",
        "--seed", str(config.stardust_seed),
        "--fec", str(config.stardust_fec),
    ]
    _run(cmd)


def _ffmpeg_filter_cmd(
    *,
    paths: StardustPaths,
    input_path: str,
    output_path: str,
    payload_file: str,
    media: MediaInfo,
    config: StardustConfig,
    video_preset: str,
    video_crf: int,
) -> list[str]:
    filter_str = (
        f"sffwembedsafe="
        f"strength={config.stardust_strength}:"
        f"pixel_density={config.stardust_p_density}:"
        f"superpixel_density={config.stardust_sp_density}:"
        f"pm_mode={config.stardust_pm_mode}:"
        f"seed={config.stardust_seed}:"
        f"fec={config.stardust_fec}:"
        f"payload_file={payload_file}"
    )

    cmd: list[str] = [
        str(paths.ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel", "info" if _is_verbose() else "warning",
        "-i", input_path,
        "-vf", filter_str,
    ]

    if media.media_kind == "video":
        cmd += ["-map", "0:v:0"]
        if media.has_audio:
            cmd += ["-map", "0:a:0?", "-c:a", "copy"]
        cmd += [
            "-c:v", "libx264",
            "-preset", video_preset,
            "-crf", str(video_crf),
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        cmd += [
            "-frames:v", "1",
            "-update", "1",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    return cmd


def embed(
    input_path: str,
    output_path: str,
    wm_id_hex: str,
    config: StardustConfig,
    *,
    video_preset: str = "veryfast",
    video_crf: int = 18,
) -> MediaInfo:
    """Watermark the input asset into the output asset.

    Uses the patched FFmpeg with the ``sffwembedsafe`` filter to run
    decode + watermark + encode in a single pipeline.  Produces no sidecar
    files; designed for blind extraction.
    """
    paths = config.paths
    embed_start = time.perf_counter()
    media = probe_media(input_path, paths)
    _log(f"Embedding watermark into {media.media_kind} {media.width}x{media.height}: {input_path}")

    with tempfile.TemporaryDirectory() as tmp:
        pp_path = os.path.join(tmp, "wm.pp")

        step_start = time.perf_counter()
        _generate_payload(pp_path, wm_id_hex, media, config)
        _log(f"payload_gen: {time.perf_counter() - step_start:.2f}s")

        step_start = time.perf_counter()
        cmd = _ffmpeg_filter_cmd(
            paths=paths,
            input_path=input_path,
            output_path=output_path,
            payload_file=pp_path,
            media=media,
            config=config,
            video_preset=video_preset,
            video_crf=video_crf,
        )
        _run(cmd)
        _log(f"ffmpeg_pipeline: {time.perf_counter() - step_start:.2f}s")

    _log(f"embed_total: {time.perf_counter() - embed_start:.2f}s")
    return media


def extract_blind(
    input_path: str,
    wm_bit_profile: int,
    config: StardustConfig,
) -> str | None:
    """Blind watermark extraction.

    Decodes the first video frame (or the full image) to luma, wraps it as
    an identity-aligned input folder for the Stardust ``extract`` tool,
    and returns the decoded WM ID hex on success or ``None`` on failure.
    """
    paths = config.paths
    media = probe_media(input_path, paths)
    width, height = media.width, media.height

    with tempfile.TemporaryDirectory() as tmp:
        aligned_dir = os.path.join(tmp, "aligned")
        os.makedirs(aligned_dir, exist_ok=True)
        raw_yuv = os.path.join(tmp, "frame.yuv")

        decode_cmd = [
            str(paths.ffmpeg),
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-vframes", "1",
            "-pix_fmt", "yuv420p",
            "-f", "rawvideo",
            raw_yuv,
        ]
        _run(decode_cmd)

        luma_size = width * height
        aligned_file = os.path.join(aligned_dir, f"aligned_0__{width}xx{height}.yuv")
        with open(raw_yuv, "rb") as src, open(aligned_file, "wb") as dst:
            dst.write(src.read(luma_size))

        pts = f"0 0 {width} 0 {width} {height} 0 {height}"
        Path(os.path.join(aligned_dir, "pts_0.txt")).write_text(pts + "\n")
        content_pts = pts + " " + " ".join(["-1 -1"] * 6)
        Path(os.path.join(aligned_dir, "content_pts_0.txt")).write_text(content_pts + "\n")

        extract_cmd = [
            str(paths.stardust_extract),
            "--input-folder", aligned_dir,
            "--strength", str(config.stardust_strength),
            "--sp-width", str(config.stardust_sp_width),
            "--sp-height", str(config.stardust_sp_height),
            "--sp-density", str(config.stardust_sp_density),
            "--bit-profile", str(wm_bit_profile),
            "--width", str(width),
            "--height", str(height),
            "--seed", str(config.stardust_seed),
            "--fec", str(config.stardust_fec),
        ]
        result = subprocess.run(extract_cmd, capture_output=True, text=True)
        for stream in (result.stdout, result.stderr):
            for line in stream.splitlines():
                if line.startswith("WM ID Hex:"):
                    return line.split(":", 1)[1].strip().lower()
    return None
