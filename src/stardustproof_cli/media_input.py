"""BMFF/ISOBMFF media-input resolver.

Classifies a ``--input`` argument into one of three shapes so that
``sign`` and ``verify`` can dispatch appropriately:

- :class:`SingleFile` — an ordinary, non-fragmented media asset (MOV,
  non-fragmented MP4, image, etc.).
- :class:`SingleFileFragmented` — a single BMFF file containing at least
  one top-level ``moof`` box (a single-file fragmented MP4, a.k.a.
  CMAF non-segmented).
- :class:`Segmented` — a directory containing exactly one init segment
  (a BMFF file with ``moov`` but no ``moof``) and one or more media
  segments (BMFF files with ``moof`` but no ``moov``).

Detection is structural — it walks the top-level BMFF box headers and
classifies by the set of boxes present. This means the resolver does
not care about filename conventions (``init.m4s``, ``seg_*.m4s``, etc.);
any packager output that follows the ISO BMFF structure works.

Only top-level box headers are read; box bodies are never loaded into
memory. For SingleFileFragmented inputs, :func:`parse_fragment_schedule`
can be called separately to extract the per-``moof`` frame schedule
needed for byte-faithful re-fragmentation during sign.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


# Top-level BMFF boxes we care about. "ftyp" and "styp" distinguish
# file-type-headers; "moov" is the movie header (only in non-fragmented
# or init segments); "moof" marks a fragment; "mdat" is media payload.
_BOX_TYPES_OF_INTEREST = frozenset({b"ftyp", b"styp", b"moov", b"moof", b"mdat", b"sidx"})


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FragmentInfo:
    """Per-``moof`` schedule entry for a single-file fragmented MP4.

    Attributes:
        sample_count: Number of samples (frames, for video) declared by
            this fragment's first ``traf/trun`` box.
        decode_time: Decode time at the start of this fragment, in the
            track's timescale units (from ``traf/tfdt``). May be ``None``
            if ``tfdt`` was absent.
    """

    sample_count: int
    decode_time: Optional[int] = None


@dataclass(frozen=True)
class SingleFile:
    """Non-fragmented input: ordinary MP4/MOV/image."""

    path: Path


@dataclass(frozen=True)
class SingleFileFragmented:
    """Single-file fragmented BMFF (one file with ``moof`` boxes)."""

    path: Path
    # Populated lazily by parse_fragment_schedule(); not filled at
    # detection time to keep resolve_media_input cheap.
    schedule: tuple[FragmentInfo, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Segmented:
    """Multi-file fragmented BMFF: init segment + per-fragment files."""

    directory: Path
    init: Path
    fragments: tuple[Path, ...]


MediaInput = Union[SingleFile, SingleFileFragmented, Segmented]


class MediaInputError(ValueError):
    """Raised when media-input classification fails or is ambiguous."""


# ---------------------------------------------------------------------------
# Low-level BMFF box scanning
# ---------------------------------------------------------------------------


def _scan_top_level_box_types(path: Path, *, max_read: int = 64 * 1024 * 1024) -> list[bytes]:
    """Return the list of top-level box type fourCCs observed in ``path``.

    Reads only the 8/16-byte box headers at each top-level position;
    never materializes box bodies. ``max_read`` bounds total bytes we
    are willing to seek across to guard against malformed inputs or
    files whose boxes are larger than expected.

    Args:
        path: Path to the BMFF file.
        max_read: Abort if header walk exceeds this byte budget.

    Returns:
        Ordered list of fourCC byte strings ``[b"ftyp", b"moov", ...]``.
    """

    types: list[bytes] = []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            fh.seek(0)
            pos = 0
            while pos < file_size and pos < max_read:
                fh.seek(pos)
                header = fh.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                # Reject obviously-not-a-box tags early (all BMFF box
                # types are printable ASCII letters/digits).
                if not all(32 <= b < 127 for b in box_type):
                    break
                if size == 1:
                    # 64-bit largesize.
                    large = fh.read(8)
                    if len(large) < 8:
                        break
                    size = int.from_bytes(large, "big")
                elif size == 0:
                    # Runs to EOF; this is the last box.
                    size = file_size - pos
                if size < 8 or pos + size > file_size:
                    break
                types.append(box_type)
                pos += size
    except OSError:
        return types
    return types


def _is_bmff_file(path: Path) -> bool:
    """True iff ``path`` begins with a plausible BMFF box header."""

    try:
        with path.open("rb") as fh:
            header = fh.read(8)
    except OSError:
        return False
    if len(header) < 8:
        return False
    box_type = header[4:8]
    return all(32 <= b < 127 for b in box_type) and box_type in (
        b"ftyp", b"styp", b"moov", b"moof", b"free", b"skip", b"sidx",
    )


# ---------------------------------------------------------------------------
# Fragmentation-shape classification
# ---------------------------------------------------------------------------


def classify_bmff_file(path: Path) -> str:
    """Classify a single BMFF file by its top-level box set.

    Returns one of:

    - ``"init"``     — contains ``moov`` but no ``moof`` (init segment
      of a segmented fMP4, or an ordinary non-fragmented MP4/MOV).
    - ``"fragment"`` — contains ``moof`` but no ``moov`` (media segment
      of a segmented fMP4).
    - ``"fragmented"`` — contains BOTH ``moov`` and ``moof`` (single-file
      fragmented BMFF).
    - ``"none"``     — neither box present (probably not a BMFF file).
    """

    types = _scan_top_level_box_types(path)
    if not types:
        return "none"
    has_moov = b"moov" in types
    has_moof = b"moof" in types
    if has_moov and has_moof:
        return "fragmented"
    if has_moov:
        return "init"
    if has_moof:
        return "fragment"
    return "none"


# ---------------------------------------------------------------------------
# Directory classification (segmented fMP4)
# ---------------------------------------------------------------------------


_NATURAL_SORT_RE = re.compile(r"(\d+)")


def _natural_sort_key(p: Path) -> list:
    """Natural sort key that orders ``seg_1 < seg_2 < ... < seg_10``."""

    parts = _NATURAL_SORT_RE.split(p.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def _classify_directory(directory: Path) -> Segmented:
    """Resolve a directory as a :class:`Segmented` fMP4 input.

    The directory must contain exactly one file whose top-level box set
    makes it an init segment (``moov`` without ``moof``), plus one or
    more fragment files (``moof`` without ``moov``). Unrelated files
    (e.g. ``.m3u8`` playlists, ``.mpd`` manifests, ``.DS_Store``) are
    silently ignored.

    Raises:
        MediaInputError: When the directory is empty, has multiple init
            candidates, lacks fragments, or has no BMFF files at all.
    """

    candidates = [p for p in directory.iterdir() if p.is_file()]
    if not candidates:
        raise MediaInputError(f"Directory is empty: {directory}")

    inits: list[Path] = []
    fragments: list[Path] = []
    fragmented_singles: list[Path] = []
    for p in candidates:
        if not _is_bmff_file(p):
            continue
        kind = classify_bmff_file(p)
        if kind == "init":
            inits.append(p)
        elif kind == "fragment":
            fragments.append(p)
        elif kind == "fragmented":
            fragmented_singles.append(p)

    if fragmented_singles and not fragments and not inits:
        # Directory contains only single-file fragmented MP4(s); not
        # our "segmented" shape. The caller should have selected the
        # file directly.
        raise MediaInputError(
            f"Directory {directory} contains single-file fragmented "
            f"MP4(s); pass one of them as --input directly instead of "
            f"the directory"
        )

    if not inits:
        raise MediaInputError(
            f"No init segment (BMFF file with moov and no moof) found in {directory}"
        )
    if len(inits) > 1:
        names = ", ".join(sorted(p.name for p in inits))
        raise MediaInputError(
            f"Multiple init segment candidates found in {directory}: {names}. "
            f"Directory must contain exactly one init segment."
        )
    if not fragments:
        raise MediaInputError(
            f"No media fragments (BMFF files with moof and no moov) found in {directory}"
        )

    fragments.sort(key=_natural_sort_key)
    return Segmented(
        directory=directory,
        init=inits[0],
        fragments=tuple(fragments),
    )


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_media_input(path: Path) -> MediaInput:
    """Classify ``path`` into one of the three media-input shapes.

    Args:
        path: A regular file or a directory.

    Returns:
        :class:`SingleFile`, :class:`SingleFileFragmented`, or
        :class:`Segmented`.

    Raises:
        MediaInputError: When the input does not exist, is a directory
            without a valid segmented-fMP4 layout, or cannot be
            classified.
    """

    if not path.exists():
        raise MediaInputError(f"Input does not exist: {path}")

    if path.is_dir():
        return _classify_directory(path)

    if not path.is_file():
        raise MediaInputError(f"Input is neither a file nor a directory: {path}")

    # Non-BMFF files (images, etc.) or BMFF files without moof boxes are
    # plain "single file" inputs. We only classify as SingleFileFragmented
    # when a top-level moof was observed.
    if not _is_bmff_file(path):
        return SingleFile(path=path)

    kind = classify_bmff_file(path)
    if kind == "fragmented":
        return SingleFileFragmented(path=path)
    # Either "init" (non-fragmented MP4/MOV) or "fragment" (orphan media
    # segment passed directly). A standalone media fragment cannot be
    # verified on its own because it has no codec-configuration box;
    # require it to live in a directory with its init segment.
    if kind == "fragment":
        raise MediaInputError(
            f"Input {path} is a standalone media fragment (moof without moov). "
            f"Pass the containing directory (init + fragments) as --input instead."
        )
    return SingleFile(path=path)


# ---------------------------------------------------------------------------
# Per-fragment schedule parsing (single-file fMP4 only)
# ---------------------------------------------------------------------------


def _walk_boxes(buf: memoryview) -> list[tuple[bytes, memoryview]]:
    """Walk immediate children of a BMFF container and return
    ``(type, body)`` tuples."""

    out: list[tuple[bytes, memoryview]] = []
    pos = 0
    while pos + 8 <= len(buf):
        size = int.from_bytes(bytes(buf[pos:pos + 4]), "big")
        box_type = bytes(buf[pos + 4:pos + 8])
        header_len = 8
        if size == 1:
            if pos + 16 > len(buf):
                break
            size = int.from_bytes(bytes(buf[pos + 8:pos + 16]), "big")
            header_len = 16
        elif size == 0:
            size = len(buf) - pos
        if size < header_len or pos + size > len(buf):
            break
        out.append((box_type, buf[pos + header_len:pos + size]))
        pos += size
    return out


def parse_fragment_schedule(path: Path) -> list[FragmentInfo]:
    """Extract the per-``moof`` schedule (sample count + decode time)
    for a single-file fragmented BMFF.

    Reads only the ``moof`` boxes (not ``mdat``), so the cost is
    proportional to the number of fragments, not file size.

    Returns:
        Ordered list, one entry per ``moof``. For inputs with multiple
        tracks we take the first ``traf`` in each ``moof`` (typical for
        CMAF video tracks).
    """

    schedule: list[FragmentInfo] = []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        fh.seek(0)
        pos = 0
        while pos < file_size:
            fh.seek(pos)
            header = fh.read(8)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            header_len = 8
            if size == 1:
                extra = fh.read(8)
                if len(extra) < 8:
                    break
                size = int.from_bytes(extra, "big")
                header_len = 16
            elif size == 0:
                size = file_size - pos
            if size < header_len or pos + size > file_size:
                break
            if box_type == b"moof":
                # Read the full moof body and descend.
                fh.seek(pos + header_len)
                body = fh.read(size - header_len)
                info = _parse_moof_body(memoryview(body))
                if info is not None:
                    schedule.append(info)
            pos += size
    return schedule


def _parse_moof_body(body: memoryview) -> Optional[FragmentInfo]:
    """Parse a single ``moof`` body and return the first track's
    fragment info."""

    for box_type, box_body in _walk_boxes(body):
        if box_type == b"traf":
            sample_count = 0
            decode_time: Optional[int] = None
            for traf_type, traf_body in _walk_boxes(box_body):
                if traf_type == b"tfdt":
                    decode_time = _parse_tfdt(traf_body)
                elif traf_type == b"trun":
                    sample_count += _parse_trun_sample_count(traf_body)
            return FragmentInfo(sample_count=sample_count, decode_time=decode_time)
    return None


def _parse_trun_sample_count(body: memoryview) -> int:
    """Extract ``sample_count`` from a ``trun`` box body. ``trun`` is a
    full-box: version(1) + flags(3) + sample_count(4) + ..."""

    if len(body) < 8:
        return 0
    return int.from_bytes(bytes(body[4:8]), "big")


def _parse_tfdt(body: memoryview) -> Optional[int]:
    """Extract ``baseMediaDecodeTime`` from a ``tfdt`` box body.

    Full-box header: version(1) + flags(3), then either 4-byte
    (version=0) or 8-byte (version=1) decode time.
    """

    if len(body) < 5:
        return None
    version = body[0]
    if version == 1:
        if len(body) < 12:
            return None
        return int.from_bytes(bytes(body[4:12]), "big")
    if len(body) < 8:
        return None
    return int.from_bytes(bytes(body[4:8]), "big")
