from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DirectoryManifestStore:
    root: Path

    def write_manifest(self, wm_id_hex: str, manifest_bytes: bytes, overwrite: bool = False) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{wm_id_hex}.c2pa"
        if path.exists() and not overwrite:
            raise FileExistsError(f"Manifest already exists: {path}")
        path.write_bytes(manifest_bytes)
        return path
