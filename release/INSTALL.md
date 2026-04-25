# StardustProof CLI Release

This release bundle is a Linux `x86_64` offline wheelhouse for local
installation.

Contents:

- `wheelhouse/` with all required Python wheels for the CLI release
- `install.sh` for offline installation
- `RELEASE-MANIFEST.json` with pinned source metadata and bundled wheels

Requirements:

- Python 3.10+
- Linux `x86_64`

Default install:

```bash
chmod +x install.sh
./install.sh
./.venv/bin/stardustproof --help
```

Install into the current Python environment instead:

```bash
./install.sh --current-env
```

Notes:

- `verify` is packaged for keystore-less local operation.
- `sign` still requires a reachable keystore service plus signing credentials.
- The bundled FFmpeg and Stardust binaries are Linux `x86_64` artifacts.
