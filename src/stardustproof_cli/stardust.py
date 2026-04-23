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


def probe_media(
    path: str,
    paths: StardustPaths,
    *,
    stdin_bytes: bytes | None = None,
) -> MediaInfo:
    """Probe media dimensions and audio presence using the bundled ffprobe.

    When ``stdin_bytes`` is provided, ffprobe reads from stdin (``pipe:0``)
    instead of the filesystem path. This is used for segmented fMP4
    inputs where the init segment and a media fragment must be
    concatenated on the fly to form a decodable stream.
    """
    input_arg = "pipe:0" if stdin_bytes is not None else path
    run_kwargs = {"capture_output": True, "text": stdin_bytes is None}
    if stdin_bytes is not None:
        run_kwargs["input"] = stdin_bytes
    result = subprocess.run(
        [
            str(paths.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate",
            "-of",
            "json",
            input_arg,
        ],
        **run_kwargs,
    )
    if result.returncode != 0:
        err = result.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise RuntimeError(f"ffprobe failed on {path}: {err}")

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


# ---------------------------------------------------------------------------
# Fragmented-input sign paths
# ---------------------------------------------------------------------------


def _sffwembedsafe_filter_str(payload_file: str, config: StardustConfig) -> str:
    """Build the ``sffwembedsafe`` filter descriptor string."""
    return (
        f"sffwembedsafe="
        f"strength={config.stardust_strength}:"
        f"pixel_density={config.stardust_p_density}:"
        f"superpixel_density={config.stardust_sp_density}:"
        f"pm_mode={config.stardust_pm_mode}:"
        f"seed={config.stardust_seed}:"
        f"fec={config.stardust_fec}:"
        f"payload_file={payload_file}"
    )


def _fragment_boundary_timestamps(schedule, frame_rate: float) -> list[float]:
    """Convert a parsed fragment schedule into a list of seconds-offsets
    at which each NEW fragment should start (excluding the leading 0).

    Given ``schedule = [F0, F1, F2, ...]`` with per-fragment frame
    counts ``F[i].sample_count``, returns
    ``[F0.count/fps, (F0+F1).count/fps, ...]`` so that each entry
    corresponds to the start timestamp of fragments 1, 2, 3, ...
    """
    timestamps: list[float] = []
    running = 0
    for i, info in enumerate(schedule):
        if i == 0:
            running = info.sample_count
            continue
        timestamps.append(running / frame_rate)
        running += info.sample_count
    return timestamps


def embed_single_file_fragmented(
    input_path: str,
    output_path: str,
    wm_id_hex: str,
    config: StardustConfig,
    fragment_schedule: list,
    *,
    video_preset: str = "veryfast",
    video_crf: int = 18,
) -> MediaInfo:
    """Watermark a single-file fragmented MP4, preserving per-fragment
    frame counts in the output.

    The output is a single-file fragmented MP4 whose moof/mdat pairs
    cover the same frame ranges as the input. We achieve this by
    passing ``-force_key_frames <timestamps>`` derived from the input
    schedule + ``-movflags +frag_custom`` so ffmpeg emits one fragment
    per keyframe boundary.
    """
    paths = config.paths
    embed_start = time.perf_counter()
    media = probe_media(input_path, paths)
    _log(
        f"Embedding watermark into single-file fragmented "
        f"{media.width}x{media.height} ({len(fragment_schedule)} fragments): {input_path}"
    )

    # Use the input's frame rate. avg_frame_rate may be "25/1"; parse.
    frame_rate = 24.0
    if media.frame_rate:
        try:
            num, den = media.frame_rate.split("/")
            frame_rate = float(num) / float(den) if float(den) else float(num)
        except (ValueError, ZeroDivisionError):
            pass

    boundary_ts = _fragment_boundary_timestamps(fragment_schedule, frame_rate)
    force_kf = ",".join(f"{t:.6f}" for t in boundary_ts) if boundary_ts else ""

    with tempfile.TemporaryDirectory() as tmp:
        pp_path = os.path.join(tmp, "wm.pp")
        _generate_payload(pp_path, wm_id_hex, media, config)

        filter_str = _sffwembedsafe_filter_str(pp_path, config)
        cmd = [
            str(paths.ffmpeg),
            "-y", "-hide_banner",
            "-loglevel", "info" if _is_verbose() else "warning",
            "-i", input_path,
            "-vf", filter_str,
            "-map", "0:v:0",
        ]
        if media.has_audio:
            cmd += ["-map", "0:a:0?", "-c:a", "copy"]
        cmd += [
            "-c:v", "libx264",
            "-preset", video_preset,
            "-crf", str(video_crf),
            "-pix_fmt", "yuv420p",
        ]
        if force_kf:
            cmd += ["-force_key_frames", force_kf]
        # Fragmented-MP4 output: one moof per keyframe boundary. The
        # frag_keyframe flag means: start a new fragment at every
        # keyframe; combined with -force_key_frames this yields the
        # same number of fragments as the input.
        cmd += [
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            output_path,
        ]

        step_start = time.perf_counter()
        _run(cmd)
        _log(f"ffmpeg_pipeline: {time.perf_counter() - step_start:.2f}s")

    _log(f"embed_total: {time.perf_counter() - embed_start:.2f}s")
    return media


def embed_segmented(
    init_path: str,
    fragment_paths: list,
    output_dir: str,
    wm_id_hex: str,
    config: StardustConfig,
    *,
    video_preset: str = "veryfast",
    video_crf: int = 18,
) -> MediaInfo:
    """Watermark a segmented fragmented-MP4 (init + media fragments) into
    an output directory containing a regenerated init + watermarked
    fragment files.

    Input fragments are concatenated with the init on the fly (piped
    into ffmpeg's stdin) so no intermediate on-disk concatenation is
    required. Output uses the DASH muxer which emits a separate init
    segment plus per-fragment files.

    Output naming:
        <output_dir>/init.m4s
        <output_dir>/seg-NNNN.m4s  (one per input fragment, natural order)
    """
    paths = config.paths
    embed_start = time.perf_counter()

    # Probe using init + first fragment piped together so ffprobe can
    # see the codec config.
    probe_bytes = read_segmented_init_plus_first_fragment(init_path, fragment_paths[0])
    media = probe_media(init_path, paths, stdin_bytes=probe_bytes)
    _log(
        f"Embedding watermark into segmented fMP4 "
        f"{media.width}x{media.height} ({len(fragment_paths)} fragments)"
    )

    # Probe per-fragment frame counts via the standalone parser so we
    # can ask ffmpeg's DASH muxer to emit the exact same number of
    # fragments, at the same frame boundaries.
    from stardustproof_c2pa_signer import parse_fragment_schedule  # local
    # The schedule is embedded in each fragment's moof; accumulate
    # across the fragment file set.
    per_frag_counts: list[int] = []
    for frag_path in fragment_paths:
        sched = parse_fragment_schedule(Path(frag_path))
        per_frag_counts.append(sched[0].sample_count if sched else 0)

    frame_rate = 24.0
    if media.frame_rate:
        try:
            num, den = media.frame_rate.split("/")
            frame_rate = float(num) / float(den) if float(den) else float(num)
        except (ValueError, ZeroDivisionError):
            pass

    # Build force_key_frames timestamps from the cumulative per-fragment
    # sample counts (in frames) divided by frame rate.
    boundary_ts: list[float] = []
    running = 0
    for i, count in enumerate(per_frag_counts):
        if i == 0:
            running = count
            continue
        boundary_ts.append(running / frame_rate)
        running += count
    force_kf = ",".join(f"{t:.6f}" for t in boundary_ts) if boundary_ts else ""

    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        pp_path = os.path.join(tmp, "wm.pp")
        _generate_payload(pp_path, wm_id_hex, media, config)
        filter_str = _sffwembedsafe_filter_str(pp_path, config)

        # Output goes to a DASH manifest in the output directory; the
        # DASH muxer emits init.m4s + seg-NNNN.m4s with the template we
        # provide, matching our resolver's expected layout.
        manifest_path = os.path.join(output_dir, "manifest.mpd")

        cmd = [
            str(paths.ffmpeg),
            "-y", "-hide_banner",
            "-loglevel", "info" if _is_verbose() else "warning",
            "-f", "mp4",
            "-i", "pipe:0",
            "-vf", filter_str,
            "-map", "0:v:0",
            "-c:v", "libx264",
            "-preset", video_preset,
            "-crf", str(video_crf),
            "-pix_fmt", "yuv420p",
        ]
        if force_kf:
            cmd += ["-force_key_frames", force_kf]
        cmd += [
            "-f", "dash",
            "-seg_duration", "4",
            "-use_template", "1",
            "-use_timeline", "0",
            "-init_seg_name", "init.m4s",
            "-media_seg_name", "seg-$Number%04d$.m4s",
            "-adaptation_sets", "id=0,streams=v",
            manifest_path,
        ]

        # Stream init+fragments bytes to ffmpeg stdin.
        step_start = time.perf_counter()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with open(init_path, "rb") as fh:
                proc.stdin.write(fh.read())
            for frag_path in fragment_paths:
                with open(frag_path, "rb") as fh:
                    # stream in chunks to avoid huge-allocation spikes
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
            proc.stdin.close()
            stdout, stderr = proc.communicate(timeout=600)
        except Exception:
            proc.kill()
            raise
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
            raise RuntimeError(f"ffmpeg segmented embed failed: {err}")
        _log(f"ffmpeg_pipeline: {time.perf_counter() - step_start:.2f}s")

        # Clean up the DASH manifest file; our output directory
        # contains only BMFF artifacts per the Segmented resolver spec.
        try:
            os.remove(manifest_path)
        except OSError:
            pass

    _log(f"embed_total: {time.perf_counter() - embed_start:.2f}s")
    return media


def read_segmented_init_plus_first_fragment(init_path: str, fragment_path: str) -> bytes:
    """Concatenate an init segment and one media segment into a single
    self-contained BMFF byte blob suitable for piping to ffmpeg/ffprobe
    via stdin.

    Used by verify (blind-extract on segmented fMP4) and by ffprobe in
    the verify path.
    """
    with open(init_path, "rb") as fh:
        init_bytes = fh.read()
    with open(fragment_path, "rb") as fh:
        frag_bytes = fh.read()
    return init_bytes + frag_bytes


def extract_blind(
    input_path: str,
    wm_bit_profile: int,
    config: StardustConfig,
    *,
    stdin_bytes: bytes | None = None,
) -> str | None:
    """Blind watermark extraction.

    Decodes the first video frame (or the full image) to luma, wraps it as
    an identity-aligned input folder for the Stardust ``extract`` tool,
    and returns the decoded WM ID hex on success or ``None`` on failure.

    When ``stdin_bytes`` is provided, ffmpeg/ffprobe read from stdin
    (``pipe:0``) instead of the filesystem path. Callers supply a
    pre-concatenated init+fragment byte blob via
    :func:`read_segmented_init_plus_first_fragment` to blind-extract
    from a segmented fMP4.
    """
    paths = config.paths
    media = probe_media(input_path, paths, stdin_bytes=stdin_bytes)
    width, height = media.width, media.height

    with tempfile.TemporaryDirectory() as tmp:
        aligned_dir = os.path.join(tmp, "aligned")
        os.makedirs(aligned_dir, exist_ok=True)
        raw_yuv = os.path.join(tmp, "frame.yuv")

        decode_cmd = [
            str(paths.ffmpeg),
            "-y", "-hide_banner", "-loglevel", "error",
        ]
        if stdin_bytes is not None:
            decode_cmd += ["-i", "pipe:0"]
        else:
            decode_cmd += ["-i", input_path]
        decode_cmd += [
            "-vframes", "1",
            "-pix_fmt", "yuv420p",
            "-f", "rawvideo",
            raw_yuv,
        ]
        # Run decode. When piping we need our own subprocess call so
        # we can pass stdin=bytes.
        if stdin_bytes is not None:
            result = subprocess.run(
                decode_cmd, input=stdin_bytes,
                capture_output=True,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
                raise RuntimeError(
                    f"ffmpeg decode failed (stdin mode): {err}"
                )
        else:
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
