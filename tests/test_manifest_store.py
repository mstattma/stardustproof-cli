from pathlib import Path

from stardustproof_cli.manifest_store import DirectoryManifestStore


def test_directory_manifest_store_writes_manifest(tmp_path: Path):
    store = DirectoryManifestStore(tmp_path / "manifests")
    path = store.write_manifest("aabb", b"manifest-bytes")
    assert path.name == "aabb.c2pa"
    assert path.read_bytes() == b"manifest-bytes"


def test_directory_manifest_store_rejects_existing_file(tmp_path: Path):
    store = DirectoryManifestStore(tmp_path)
    store.write_manifest("aabb", b"first")
    try:
        store.write_manifest("aabb", b"second")
        assert False, "expected FileExistsError"
    except FileExistsError:
        pass
