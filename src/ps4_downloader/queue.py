from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def load_queue(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    if name and name.lower().endswith(".pkg"):
        return name
    return name or "package.pkg"
