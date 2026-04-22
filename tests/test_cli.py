import pytest

from stardustproof_cli.cli import _validate_payload
from stardustproof_cli.config import StardustPaths


def test_validate_payload_accepts_matching_bit_profile():
    payload = _validate_payload("00112233", 32)
    assert payload == bytes.fromhex("00112233")


def test_validate_payload_rejects_mismatched_bit_profile():
    with pytest.raises(ValueError):
        _validate_payload("00112233", 64)


def test_stardust_paths_support_custom_bin_dir(tmp_path):
    paths = StardustPaths(custom_bin_dir=tmp_path)
    assert paths.bin_dir == tmp_path
