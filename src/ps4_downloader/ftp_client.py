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

    def read_file(self, remote_path: str) -> bytes:
        with self._connect() as ftp:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {remote_path}", buf.write)
            except error_perm as exc:
                raise Ps4FtpError(f"Cannot read {remote_path}: {exc}") from exc
            return buf.getvalue()

    def read_goldhen_config(self) -> dict[str, str]:
        """Parse /data/GoldHEN/config.ini (or goldhen.cfg) into a flat key→value map."""
        candidates = (
            "/data/GoldHEN/config.ini",
            "/data/GoldHEN/goldhen.cfg",
            "/data/goldhen/config.ini",
        )
        raw = b""
        last_err: Exception | None = None
        for path in candidates:
            try:
                raw = self.read_file(path)
                break
            except Ps4FtpError as exc:
                last_err = exc
                continue
        if not raw:
            raise Ps4FtpError(
                f"GoldHEN config not found under /data/GoldHEN/ ({last_err})"
            )

        text = raw.decode("utf-8", errors="replace")
        # Strip UTF-8 BOM / weird leading chars seen in some GoldHEN dumps
        if text.startswith("\ufeff"):
            text = text[1:]
        section = ""
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            out[key] = val
            if section:
                out[f"{section}.{key}"] = val
        return out

    def binloader_settings(self) -> dict[str, object]:
        """Return enabled/port from GoldHEN config when readable."""
        try:
            cfg = self.read_goldhen_config()
        except Ps4FtpError:
            return {"enabled": None, "port": None}

        enabled_raw = cfg.get("binloader.enabled") or cfg.get("binloader_enabled")
        port_raw = cfg.get("binloader.port") or cfg.get("binloader_port")
        for k, v in cfg.items():
            if "binloader" in k and k.endswith(".enabled"):
                enabled_raw = v
            if "binloader" in k and k.endswith(".port"):
                port_raw = v

        enabled: bool | None
        if enabled_raw is None:
            enabled = None
        else:
            enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}

        port: int | None = None
        if port_raw is not None:
            try:
                port = int(str(port_raw).strip())
            except ValueError:
                port = None

        return {"enabled": enabled, "port": port}
