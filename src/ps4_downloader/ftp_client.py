from __future__ import annotations

import io
import re
import socket
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from ftplib import FTP, error_perm
from typing import Iterator


@dataclass(frozen=True)
class InstalledTitle:
    title_id: str
    name: str | None = None


class Ps4FtpError(RuntimeError):
    pass


_TITLE_ID_RE = re.compile(r"^[A-Z]{4}\d{5}$")


def _sfo_get_title(data: bytes) -> str | None:
    """Read TITLE (or TITLE_ID fallback) from a PS4 param.sfo blob."""
    if len(data) < 20 or data[0:4] != b"\x00PSF":
        return None
    key_table_start = struct.unpack_from("<I", data, 8)[0]
    data_table_start = struct.unpack_from("<I", data, 12)[0]
    entry_count = struct.unpack_from("<I", data, 16)[0]

    title: str | None = None
    title_id: str | None = None
    for i in range(entry_count):
        off = 20 + i * 16
        if off + 16 > len(data):
            break
        key_offset, data_fmt, value_len, _value_max, data_offset = struct.unpack_from(
            "<HHIII", data, off
        )
        key_start = key_table_start + key_offset
        key_end = data.find(b"\x00", key_start)
        if key_end < 0:
            continue
        key = data[key_start:key_end].decode("ascii", errors="replace")
        raw_start = data_table_start + data_offset
        raw = data[raw_start : raw_start + value_len]
        if data_fmt not in (0x0204, 0x0004):
            continue
        value = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
        if key == "TITLE" and value:
            title = value
        elif key == "TITLE_ID" and value:
            title_id = value
    return title or title_id


class Ps4FtpClient:
    """Read console state through GoldHEN's built-in FTP server (default port 2121)."""

    def __init__(
        self,
        host: str,
        port: int = 2121,
        user: str = "",
        password: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout

    @contextmanager
    def _connect(self) -> Iterator[FTP]:
        ftp = FTP()
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
            ftp.login(self.user or "anonymous", self.password or "")
            ftp.set_pasv(True)
        except (OSError, error_perm, socket.timeout) as exc:
            raise Ps4FtpError(
                f"Cannot connect to GoldHEN FTP at {self.host}:{self.port}. "
                "Enable FTP Server in GoldHEN → Server Settings (default port 2121)."
            ) from exc
        try:
            yield ftp
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def ping(self) -> bool:
        try:
            with self._connect() as ftp:
                ftp.voidcmd("NOOP")
            return True
        except Ps4FtpError:
            return False

    def list_installed(self, *, with_names: bool = True) -> list[InstalledTitle]:
        with self._connect() as ftp:
            try:
                names = ftp.nlst("/user/app")
            except error_perm as exc:
                raise Ps4FtpError(f"Cannot list /user/app: {exc}") from exc

            title_ids: list[str] = []
            for name in names:
                base = name.rstrip("/").rsplit("/", 1)[-1]
                if _TITLE_ID_RE.match(base):
                    title_ids.append(base)
            title_ids.sort()

            results: list[InstalledTitle] = []
            for title_id in title_ids:
                title_name = None
                if with_names:
                    title_name = self._read_title_name(ftp, title_id)
                results.append(InstalledTitle(title_id=title_id, name=title_name))
            return results

    def _read_title_name(self, ftp: FTP, title_id: str) -> str | None:
        candidates = [
            f"/system_data/priv/appmeta/{title_id}/param.sfo",
            f"/user/appmeta/{title_id}/param.sfo",
            f"/user/app/{title_id}/sce_sys/param.sfo",
        ]
        for remote in candidates:
            try:
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {remote}", buf.write)
                name = _sfo_get_title(buf.getvalue())
                if name:
                    return name
            except error_perm:
                continue
        return None
