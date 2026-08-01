from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Project root (dev) or folder next to the exe / launcher (frozen/portable)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/ps4_downloader/paths.py -> parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """Where bundled read-only assets live (PyInstaller extract dir or repo)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return app_root()


def payload_path() -> Path:
    bundled = resource_root() / "vendor" / "dpi_payload.bin"
    if bundled.is_file():
        return bundled
    return app_root() / "vendor" / "dpi_payload.bin"


def default_config_path() -> Path:
    return app_root() / "config.toml"
