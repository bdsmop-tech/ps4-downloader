from __future__ import annotations

from pathlib import Path

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from ps4_downloader.queue import filename_from_url


def download_pkg(
    url: str,
    dest_dir: Path,
    *,
    resume: bool = True,
    timeout: float = 60.0,
) -> Path:
    """Download a PKG to dest_dir with optional resume and progress bar."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = filename_from_url(url)
    dest = dest_dir / filename
    part = dest.with_suffix(dest.suffix + ".part")

    headers: dict[str, str] = {}
    start = 0
    if resume and part.exists():
        start = part.stat().st_size
        headers["Range"] = f"bytes={start}-"

    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as resp:
        if resp.status_code == 416:
            # Already fully downloaded as .part
            part.rename(dest)
            return dest
        resp.raise_for_status()

        total: int | None = None
        content_range = resp.headers.get("content-range")
        if content_range and "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total = None
        elif resp.headers.get("content-length"):
            total = start + int(resp.headers["content-length"])

        mode = "ab" if start and resp.status_code == 206 else "wb"
        if mode == "wb":
            start = 0

        with Progress(
            TextColumn("[bold blue]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task_id = progress.add_task("download", total=total, completed=start, name=filename)
            with part.open(mode) as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    progress.update(task_id, advance=len(chunk))

    part.rename(dest)
    return dest
