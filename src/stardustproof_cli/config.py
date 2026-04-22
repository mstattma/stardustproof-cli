from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_stardust_defaults() -> dict:
    defaults_path = _repo_root() / "stardust_defaults.json"
    if defaults_path.exists():
        return json.loads(defaults_path.read_text())
    return {}


_SD = _load_stardust_defaults()


@dataclass
class StardustPaths:
    """Filesystem layout for bundled binaries.

    The repo ships x86_64 Linux binaries under ``bin/``:

    - ``bin/stardust/``  : ``sffw-embed``, ``extract``, ``align``
    - ``bin/ffmpeg/bin/`` : patched static ``ffmpeg`` and ``ffprobe`` with
                             the ``sffwembedsafe`` filter
    """

    repo_root: Path = field(default_factory=_repo_root)
    custom_bin_dir: Path | None = None

    @property
    def bin_dir(self) -> Path:
        if self.custom_bin_dir is not None:
            return self.custom_bin_dir
        env_bin_dir = os.environ.get("STARDUSTPROOF_BIN_DIR")
        if env_bin_dir:
            return Path(env_bin_dir).resolve()
        return self.repo_root / "bin"

    @property
    def stardust_dir(self) -> Path:
        return self.bin_dir / "stardust"

    @property
    def ffmpeg_dir(self) -> Path:
        return self.bin_dir / "ffmpeg" / "bin"

    @property
    def stardust_embed(self) -> Path:
        return self.stardust_dir / "sffw-embed"

    @property
    def stardust_extract(self) -> Path:
        return self.stardust_dir / "extract"

    @property
    def stardust_align(self) -> Path:
        return self.stardust_dir / "align"

    @property
    def ffmpeg(self) -> Path:
        return self.ffmpeg_dir / "ffmpeg"

    @property
    def ffprobe(self) -> Path:
        return self.ffmpeg_dir / "ffprobe"

    @property
    def candidate_bin_dirs(self) -> list[Path]:
        candidates: list[Path] = []
        if self.custom_bin_dir is not None:
            candidates.append(self.custom_bin_dir)
        env_bin_dir = os.environ.get("STARDUSTPROOF_BIN_DIR")
        if env_bin_dir:
            candidates.append(Path(env_bin_dir).resolve())
        candidates.append(self.repo_root / "bin")
        seen: set[Path] = set()
        unique: list[Path] = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)
        return unique

    def check_binaries(self) -> list[str]:
        missing: list[str] = []
        for name, path in [
            ("sffw-embed", self.stardust_embed),
            ("extract", self.stardust_extract),
            ("ffmpeg", self.ffmpeg),
            ("ffprobe", self.ffprobe),
        ]:
            if not path.exists():
                missing.append(f"{name} ({path})")
        return missing

    def resolve(self) -> "StardustPaths":
        for candidate in self.candidate_bin_dirs:
            resolved = StardustPaths(repo_root=self.repo_root, custom_bin_dir=candidate)
            if not resolved.check_binaries():
                return resolved
        return self


@dataclass
class StardustConfig:
    stardust_strength: int = _SD.get("strength", 4)
    stardust_sp_width: int = _SD.get("sp_width", 7)
    stardust_sp_height: int = _SD.get("sp_height", 7)
    stardust_sp_density: int = _SD.get("sp_density", 100)
    stardust_p_density: int = _SD.get("p_density", 100)
    stardust_pm_mode: int = _SD.get("pm_mode", 3)
    stardust_seed: int = _SD.get("seed", 1)
    stardust_fec: int = _SD.get("fec", 2)
    stardust_bit_profile: int = _SD.get("bit_profile", 48)
    paths: StardustPaths = field(default_factory=StardustPaths)
