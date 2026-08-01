from __future__ import annotations

import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from ps4_downloader.pkg_info import PkgInfo, read_pkg_info
from ps4_downloader.pkg_server import PkgHttpServer
from ps4_downloader.paths import payload_path as default_payload_path

# Marker inside DPI's installer payload — replaced with PC IP + listen port.
_MARKER = bytes([0xB4] * 6)


class GoldHenError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    """Outcome of pushing a PKG to the console over LAN."""

    pkg_name: str
    package_size: int
    bytes_sent: int
    download_complete: bool

    @property
    def pct(self) -> float:
        if self.package_size <= 0:
            return 0.0
        return min(100.0, 100.0 * self.bytes_sent / self.package_size)


def detect_lan_ip(remote_host: str) -> str:
    """Pick the local IP used to reach the PS4."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, 9))
        ip = sock.getsockname()[0]
    finally:
        sock.close()
    if ip.startswith("127."):
        raise GoldHenError("Could not detect LAN IP. Set ps4.pc_host in config.toml")
    return ip


class GoldHenClient:
    """Send a local PKG the DirectPackageInstaller way: HTTP serve + BinLoader payload."""

    def __init__(
        self,
        ps4_host: str,
        *,
        pc_host: str | None = None,
        binloader_ports: list[int] | None = None,
        http_port: int = 9898,
        payload_path: Path | None = None,
    ) -> None:
        self.ps4_host = ps4_host
        self.pc_host = pc_host or detect_lan_ip(ps4_host)
        self.binloader_ports = binloader_ports or [9090, 9191, 9021, 9020]
        self.http_port = http_port
        self.payload_path = payload_path if payload_path is not None else default_payload_path()

    def ping(self) -> tuple[bool, int | None]:
        for port in self.binloader_ports:
            try:
                with socket.create_connection((self.ps4_host, port), timeout=2.0):
                    return True, port
            except OSError:
                continue
        return False, None

    def send_pkg(
        self,
        pkg_path: Path,
        *,
        wait_transfer: bool = True,
        on_status: Callable[[str], None] | None = None,
    ) -> SendResult:
        pkg_path = pkg_path.resolve()
        info = read_pkg_info(pkg_path)

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        server = PkgHttpServer(
            self.pc_host,
            self.http_port,
            pkg_path,
            {
                "package_size": info.package_size,
                "digest": info.digest,
            },
        )
        server.start()

        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind on all interfaces; payload is patched with pc_host + this port.
        listen.bind(("0.0.0.0", 0))
        listen.listen(1)
        listen.settimeout(15.0)
        info_port = listen.getsockname()[1]

        result = SendResult(
            pkg_name=pkg_path.name,
            package_size=info.package_size,
            bytes_sent=0,
            download_complete=False,
        )

        try:
            status("Injecting installer payload via GoldHEN BinLoader…")
            bin_port = self._inject_payload(info_port)
            status(f"Payload sent on :{bin_port}, waiting for PS4 callback…")
            client, _addr = listen.accept()
            with client:
                client.settimeout(30.0)
                client.sendall(self._pkg_info_buffer(info, server.manifest_url))
            status("PS4 accepted the package — console is downloading over LAN…")

            if wait_transfer:
                result = self._wait_for_download(server, info)
            else:
                result = SendResult(
                    pkg_name=pkg_path.name,
                    package_size=info.package_size,
                    bytes_sent=server.bytes_sent,
                    download_complete=False,
                )
        except socket.timeout as exc:
            raise GoldHenError(
                f"PS4 did not connect back after BinLoader inject on port {bin_port}. "
                "Enable BinLoader in GoldHEN → Server Settings."
            ) from exc
        finally:
            listen.close()
            # Give the console a moment to finish last reads
            time.sleep(1.0)
            server.stop()

        return result

    def _inject_payload(self, info_port: int) -> int:
        if not self.payload_path.is_file():
            raise GoldHenError(f"Missing payload: {self.payload_path}")

        payload = bytearray(self.payload_path.read_bytes())
        offset = bytes(payload).find(_MARKER)
        if offset < 0:
            raise GoldHenError("Payload marker not found — vendor/dpi_payload.bin is corrupt")

        ip_bytes = socket.inet_aton(self.pc_host)
        port_bytes = struct.pack(">H", info_port)
        payload[offset : offset + 4] = ip_bytes
        payload[offset + 4 : offset + 6] = port_bytes

        last_err: OSError | None = None
        for port in self.binloader_ports:
            try:
                with socket.create_connection((self.ps4_host, port), timeout=3.0) as sock:
                    sock.sendall(payload)
                return port
            except OSError as exc:
                last_err = exc
                continue
        raise GoldHenError(
            f"Cannot reach GoldHEN BinLoader on {self.ps4_host} "
            f"ports {self.binloader_ports}: {last_err}"
        )

    def _pkg_info_buffer(self, info: PkgInfo, manifest_url: str) -> bytes:
        def _field(data: bytes) -> bytes:
            return struct.pack("<I", len(data)) + data

        url = manifest_url.encode("utf-8")
        name = info.friendly_name.encode("utf-8")
        content_id = info.content_id.encode("utf-8")
        pkg_type = info.bgft_content_type.encode("utf-8")
        size = struct.pack("<q", info.package_size)
        icon = b""

        buf = bytearray()
        buf += struct.pack("<I", 1)  # new package
        buf += _field(url)
        buf += _field(name)
        buf += _field(content_id)
        buf += _field(pkg_type)
        buf += size
        buf += _field(icon)
        return bytes(buf)

    def _wait_for_download(
        self,
        server: PkgHttpServer,
        info: PkgInfo,
        idle_limit: float = 20.0,
    ) -> SendResult:
        """Stay up until the console pulled the file; show progress and return status."""
        total = info.package_size
        last_sent = -1
        idle_since = time.time()
        complete = False

        with Progress(
            TextColumn("[bold blue]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task_id = progress.add_task("ps4-dl", total=total, completed=0, name=info.path.name)
            while True:
                sent = min(server.bytes_sent, total)
                progress.update(task_id, completed=sent)

                if sent != last_sent:
                    last_sent = sent
                    idle_since = time.time()

                if sent >= total:
                    time.sleep(2.0)
                    complete = True
                    break
                if sent >= total * 0.99 and (time.time() - idle_since) > 5.0:
                    complete = True
                    break
                if sent > 0 and (time.time() - idle_since) > idle_limit:
                    # Transfer stalled after partial progress — report what we know
                    complete = sent >= total * 0.95
                    break
                if sent == 0 and (time.time() - idle_since) > 60.0:
                    raise GoldHenError(
                        "Payload was sent but the PS4 never downloaded the PKG over HTTP. "
                        f"Check firewall for TCP {self.http_port} and that pc_host={self.pc_host} is correct."
                    )
                time.sleep(0.25)

        return SendResult(
            pkg_name=info.path.name,
            package_size=total,
            bytes_sent=min(server.bytes_sent, total),
            download_complete=complete,
        )
