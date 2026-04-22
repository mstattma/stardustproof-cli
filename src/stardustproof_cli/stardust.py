from __future__ import annotations

import os
import shutil
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
        stderr = getattr(result, "stderr", "")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {stderr}")
    return result


def probe_media(path: str) -> MediaInfo:
    result = subprocess.run(
        [
            "ffprobe",
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
    import json

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError(f"No video stream found in {path}")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    ext = Path(path).suffix.lower()
    media_kind = "video" if ext in {".mp4", ".mov", ".m4v", ".webm"} else "image"
    return MediaInfo(
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        frame_rate=video_stream.get("avg_frame_rate") or None,
        has_audio=has_audio,
        media_kind=media_kind,
    )


def _image_dimensions(image_path: str) -> tuple[int, int]:
    media = probe_media(image_path)
    return media.width, media.height


def check_binaries(paths: StardustPaths) -> list[str]:
    missing = paths.check_binaries()
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg (not in PATH)")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe (not in PATH)")
    return missing


def embed(image_path: str, output_path: str, wm_id_hex: str, config: StardustConfig) -> str:
    paths = config.paths
    embed_start = time.perf_counter()
    media = probe_media(image_path)
    width = media.width
    height = media.height
    bit_profile = len(wm_id_hex) * 4
    _log(f"Embedding watermark into {media.media_kind} {width}x{height}: {image_path}")

    with tempfile.TemporaryDirectory() as tmp:
        cover_yuv = os.path.join(tmp, "cover.yuv")
        embedded_yuv = os.path.join(tmp, "embedded.yuv")

        step_start = time.perf_counter()
        _run(["ffmpeg", "-y", "-i", image_path, "-pix_fmt", "yuv420p", "-f", "rawvideo", cover_yuv])
        ffmpeg_decode_s = time.perf_counter() - step_start
        _log(f"ffmpeg decode to rawvideo: {ffmpeg_decode_s:.2f}s")

        step_start = time.perf_counter()
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
        stardust_embed_s = time.perf_counter() - step_start
        _log(f"sffw-embed: {stardust_embed_s:.2f}s")

        if media.media_kind == "video":
            frame_rate = media.frame_rate or "30"
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "yuv420p",
                "-s",
                f"{width}x{height}",
                "-r",
                frame_rate,
                "-i",
                embedded_yuv,
                "-i",
                image_path,
                "-map",
                "0:v:0",
            ]
            if media.has_audio:
                cmd.extend(["-map", "1:a:0", "-c:a", "copy"])
            cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", output_path])
            step_start = time.perf_counter()
            _run(cmd)
            ffmpeg_encode_s = time.perf_counter() - step_start
            _log(f"ffmpeg video encode: {ffmpeg_encode_s:.2f}s")
        else:
            step_start = time.perf_counter()
            _run([
                "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}", "-i", embedded_yuv,
                "-frames:v", "1", output_path,
            ])
            ffmpeg_encode_s = time.perf_counter() - step_start
            _log(f"ffmpeg image encode: {ffmpeg_encode_s:.2f}s")

        base = os.path.splitext(output_path)[0]
        reference_path = f"{base}.reference.yuv"
        step_start = time.perf_counter()
        shutil.move(cover_yuv, reference_path)
        Path(f"{base}.stardust_meta").write_text(f"{width} {height}\n")
        _log(f"reference sidecar move + metadata: {time.perf_counter() - step_start:.2f}s")
    _log(f"Embed pipeline total: {time.perf_counter() - embed_start:.2f}s")
    return output_path
