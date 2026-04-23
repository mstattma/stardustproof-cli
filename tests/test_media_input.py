"""Unit tests for the BMFF media-input resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from stardustproof_cli.media_input import (
    FragmentInfo,
    MediaInputError,
    Segmented,
    SingleFile,
    SingleFileFragmented,
    classify_bmff_file,
    parse_fragment_schedule,
    resolve_media_input,
)


# ---------------------------------------------------------------------------
# BMFF synthesis helpers
#
# We fabricate tiny, structurally valid BMFF-like files so the resolver
# can be exercised without requiring real MP4s.
# ---------------------------------------------------------------------------


def _box(box_type: bytes, body: bytes = b"") -> bytes:
    """Build a minimal BMFF box (size + fourCC + body)."""

    size = 8 + len(body)
    return size.to_bytes(4, "big") + box_type + body


def _ftyp(major: bytes = b"isom") -> bytes:
    # full ftyp body: major_brand + minor + compatible_brands
    body = major + b"\x00\x00\x00\x00" + b"isom" + b"iso5"
    return _box(b"ftyp", body)


def _moov_minimal() -> bytes:
    return _box(b"moov", b"\x00" * 24)


def _build_tfdt(decode_time: int, version: int = 1) -> bytes:
    flags = b"\x00\x00\x00"
    if version == 1:
        body = bytes([version]) + flags + decode_time.to_bytes(8, "big")
    else:
        body = bytes([version]) + flags + decode_time.to_bytes(4, "big")
    return _box(b"tfdt", body)


def _build_trun(sample_count: int) -> bytes:
    # version=1, flags=0, sample_count, no per-sample entries
    body = b"\x01\x00\x00\x00" + sample_count.to_bytes(4, "big")
    return _box(b"trun", body)


def _build_traf(sample_count: int, decode_time: int | None = None) -> bytes:
    body = b""
    if decode_time is not None:
        body += _build_tfdt(decode_time)
    body += _build_trun(sample_count)
    return _box(b"traf", body)


def _build_moof(sample_count: int, decode_time: int | None = None) -> bytes:
    # mfhd (sequence_number) + traf
    mfhd = _box(b"mfhd", b"\x00\x00\x00\x00" + (1).to_bytes(4, "big"))
    traf = _build_traf(sample_count, decode_time)
    return _box(b"moof", mfhd + traf)


def _mdat_stub(size: int = 16) -> bytes:
    body = b"\x00" * max(0, size - 8)
    return _box(b"mdat", body)


def _write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# classify_bmff_file
# ---------------------------------------------------------------------------


def test_classify_init_only(tmp_path):
    p = _write(tmp_path, "init.mp4", _ftyp() + _moov_minimal())
    assert classify_bmff_file(p) == "init"


def test_classify_fragment_only(tmp_path):
    p = _write(tmp_path, "seg_0001.m4s", _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(32))
    assert classify_bmff_file(p) == "fragment"


def test_classify_single_file_fragmented(tmp_path):
    data = _ftyp() + _moov_minimal() + _build_moof(2, 0) + _mdat_stub()
    p = _write(tmp_path, "frag.mp4", data)
    assert classify_bmff_file(p) == "fragmented"


def test_classify_non_bmff(tmp_path):
    p = _write(tmp_path, "garbage.bin", b"\xff" * 64)
    assert classify_bmff_file(p) == "none"


# ---------------------------------------------------------------------------
# resolve_media_input
# ---------------------------------------------------------------------------


def test_resolve_single_file_non_bmff(tmp_path):
    p = _write(tmp_path, "photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 60)
    result = resolve_media_input(p)
    assert isinstance(result, SingleFile)
    assert result.path == p


def test_resolve_single_file_ordinary_mp4(tmp_path):
    p = _write(tmp_path, "movie.mp4", _ftyp() + _moov_minimal())
    result = resolve_media_input(p)
    assert isinstance(result, SingleFile)


def test_resolve_single_file_fragmented(tmp_path):
    p = _write(
        tmp_path, "frag.mp4",
        _ftyp() + _moov_minimal() + _build_moof(5, 0) + _mdat_stub(),
    )
    result = resolve_media_input(p)
    assert isinstance(result, SingleFileFragmented)
    assert result.path == p


def test_resolve_orphan_fragment_rejected(tmp_path):
    # A raw media segment passed directly should be rejected because
    # it cannot be verified without its init.
    p = _write(
        tmp_path, "seg_0001.m4s",
        _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(16),
    )
    with pytest.raises(MediaInputError, match="standalone media fragment"):
        resolve_media_input(p)


def test_resolve_segmented_directory(tmp_path):
    _write(tmp_path, "init.mp4", _ftyp() + _moov_minimal())
    _write(tmp_path, "seg_0001.m4s", _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(16))
    _write(tmp_path, "seg_0002.m4s", _ftyp(b"msdh") + _build_moof(4, 3) + _mdat_stub(16))
    _write(tmp_path, "seg_0010.m4s", _ftyp(b"msdh") + _build_moof(2, 7) + _mdat_stub(16))

    result = resolve_media_input(tmp_path)
    assert isinstance(result, Segmented)
    assert result.init.name == "init.mp4"
    # Natural sort: seg_0001 < seg_0002 < seg_0010
    assert [p.name for p in result.fragments] == ["seg_0001.m4s", "seg_0002.m4s", "seg_0010.m4s"]


def test_resolve_segmented_ignores_unrelated_files(tmp_path):
    _write(tmp_path, "init.mp4", _ftyp() + _moov_minimal())
    _write(tmp_path, "seg_0001.m4s", _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(16))
    _write(tmp_path, "playlist.m3u8", b"#EXTM3U\n#EXT-X-VERSION:6\n")
    _write(tmp_path, ".DS_Store", b"\x00\x01\x02")

    result = resolve_media_input(tmp_path)
    assert isinstance(result, Segmented)
    assert len(result.fragments) == 1


def test_resolve_directory_missing_init(tmp_path):
    _write(tmp_path, "seg_0001.m4s", _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(16))
    with pytest.raises(MediaInputError, match="No init segment"):
        resolve_media_input(tmp_path)


def test_resolve_directory_multiple_inits_rejected(tmp_path):
    _write(tmp_path, "init_a.mp4", _ftyp() + _moov_minimal())
    _write(tmp_path, "init_b.mp4", _ftyp() + _moov_minimal())
    _write(tmp_path, "seg_0001.m4s", _ftyp(b"msdh") + _build_moof(3, 0) + _mdat_stub(16))
    with pytest.raises(MediaInputError, match="Multiple init segment candidates"):
        resolve_media_input(tmp_path)


def test_resolve_directory_no_fragments(tmp_path):
    _write(tmp_path, "init.mp4", _ftyp() + _moov_minimal())
    with pytest.raises(MediaInputError, match="No media fragments"):
        resolve_media_input(tmp_path)


def test_resolve_directory_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(MediaInputError, match="empty"):
        resolve_media_input(empty)


def test_resolve_nonexistent_path(tmp_path):
    with pytest.raises(MediaInputError, match="does not exist"):
        resolve_media_input(tmp_path / "nope.mp4")


# ---------------------------------------------------------------------------
# parse_fragment_schedule
# ---------------------------------------------------------------------------


def test_parse_fragment_schedule_counts_per_moof(tmp_path):
    moofs = [
        _build_moof(sample_count=10, decode_time=0),
        _build_moof(sample_count=12, decode_time=10),
        _build_moof(sample_count=8, decode_time=22),
    ]
    # Interleave with mdat stubs so the walker skips over them.
    payload = b"".join(m + _mdat_stub(32) for m in moofs)
    p = _write(tmp_path, "frag.mp4", _ftyp() + _moov_minimal() + payload)

    schedule = parse_fragment_schedule(p)
    assert schedule == [
        FragmentInfo(sample_count=10, decode_time=0),
        FragmentInfo(sample_count=12, decode_time=10),
        FragmentInfo(sample_count=8, decode_time=22),
    ]


def test_parse_fragment_schedule_handles_no_tfdt(tmp_path):
    data = _ftyp() + _moov_minimal() + _build_moof(7) + _mdat_stub()
    p = _write(tmp_path, "frag.mp4", data)
    schedule = parse_fragment_schedule(p)
    assert len(schedule) == 1
    assert schedule[0].sample_count == 7
    assert schedule[0].decode_time is None


def test_parse_fragment_schedule_empty_for_non_fragmented(tmp_path):
    p = _write(tmp_path, "movie.mp4", _ftyp() + _moov_minimal())
    assert parse_fragment_schedule(p) == []
