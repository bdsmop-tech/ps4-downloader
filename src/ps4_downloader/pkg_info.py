from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


_CONTENT_TYPE_MAP = {
    0x1A: "GD",  # game / patch / remaster
    0x1B: "AC",  # additional content
    0x1C: "AL",  # AC no data
    0x1E: "DP",  # delta patch
}


@dataclass(frozen=True)
class PkgInfo:
    path: Path
    content_id: str
    title_id: str
    friendly_name: str
    content_type: str
    package_size: int
    digest: str  # 64 hex chars (SHA-256)

    @property
    def bgft_content_type(self) -> str:
        return f"PS4{self.content_type.upper()}"


def read_pkg_info(path: Path) -> PkgInfo:
    data = path.read_bytes()[:0x5C0]
    if len(data) < 0x5C0 or data[0:4] != b"\x7fCNT":
        raise ValueError(f"Not a PS4 PKG: {path}")

    content_id = data[0x40:0x40 + 0x24].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    content_type_id = struct.unpack(">I", data[0x74:0x78])[0]
    content_type = _CONTENT_TYPE_MAP.get(content_type_id, "GD")
    digest = data[0x5A0:0x5C0].hex().upper()
    title_id = content_id[7:16] if len(content_id) >= 16 else content_id
    friendly_name = path.stem

    return PkgInfo(
        path=path,
        content_id=content_id,
        title_id=title_id,
        friendly_name=friendly_name,
        content_type=content_type,
        package_size=path.stat().st_size,
        digest=digest,
    )
