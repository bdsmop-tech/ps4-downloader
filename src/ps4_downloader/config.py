from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ps4Config:
    host: str
    pc_host: str
    binloader_ports: tuple[int, ...]
    http_port: int = 9898
    payload_port: int = 9191
    ftp_port: int = 2121
    ftp_user: str = ""
    ftp_password: str = ""
    # binloader = GoldHEN only (DPI with no other apps); auto tries RPI if present
    install_method: str = "binloader"


@dataclass(frozen=True)
class DownloadConfig:
    dir: Path
    resume: bool = True


@dataclass(frozen=True)
class AppConfig:
    ps4: Ps4Config
    download: DownloadConfig
    queue_file: Path


def load_config(path: Path | None = None) -> AppConfig:
    config_path = (path or Path("config.toml")).resolve()
    if not config_path.exists():
        example = config_path.with_name("config.example.toml")
        raise FileNotFoundError(
            f"Missing {config_path}. Copy {example.name} to config.toml and set your PS4 IP."
        )

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    ps4 = raw.get("ps4", {})
    dl = raw.get("download", {})
    queue = raw.get("queue", {})
    base = config_path.parent

    download_dir = Path(dl.get("dir", "downloads"))
    if not download_dir.is_absolute():
        download_dir = base / download_dir
    download_dir.mkdir(parents=True, exist_ok=True)

    queue_file = Path(queue.get("file", "queue.txt"))
    if not queue_file.is_absolute():
        queue_file = base / queue_file

    ports = ps4.get("binloader_ports", [9090, 9021, 9020])
    if isinstance(ports, int):
        ports = [ports]

    return AppConfig(
        ps4=Ps4Config(
            host=ps4["host"],
            pc_host=str(ps4.get("pc_host", "") or ""),
            binloader_ports=tuple(int(p) for p in ports),
            http_port=int(ps4.get("http_port", 9898)),
            payload_port=int(ps4.get("payload_port", 9191)),
            ftp_port=int(ps4.get("ftp_port", 2121)),
            ftp_user=str(ps4.get("ftp_user", "")),
            ftp_password=str(ps4.get("ftp_password", "")),
            install_method=str(ps4.get("install_method", "binloader") or "binloader").lower(),
        ),
        download=DownloadConfig(
            dir=download_dir,
            resume=bool(dl.get("resume", True)),
        ),
        queue_file=queue_file,
    )
