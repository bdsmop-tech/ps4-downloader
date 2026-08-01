from __future__ import annotations

import base64
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
from ps4_downloader.rpi_client import RpiError, is_etahen_online, is_rpi_online, push_rpi

# Marker inside DPI's installer payload — replaced with PC IP + listen port.
_MARKER = bytes([0xB4] * 6)


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    """Outcome of pushing a PKG to the console over LAN."""

    pkg_name: str
    package_size: int
    bytes_sent: int
    download_complete: bool
    method: str  # "rpi" | "binloader"

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
        raise InstallError("Could not detect LAN IP. Set ps4.pc_host in config.toml")
    return ip


class Ps4Installer:
    """Same path DirectPackageInstaller uses with only GoldHEN: BinLoader + local HTTP."""

    def __init__(
        self,
        ps4_host: str,
        *,
        pc_host: str | None = None,
        binloader_ports: list[int] | None = None,
        http_port: int = 9898,
        payload_port: int = 9191,
        method: str = "binloader",
        payload_path: Path | None = None,
    ) -> None:
        self.ps4_host = ps4_host
        self.pc_host = pc_host or detect_lan_ip(ps4_host)
        # GoldHEN BinLoader ports (DPI order)
        self.binloader_ports = binloader_ports or [9090, 9021, 9020]
        self.http_port = http_port
        # Fixed PC callback port — DPI settings.ini PayloadPort (often 9191).
        # Ephemeral ports are frequently blocked by Windows Firewall for python.exe.
        self.payload_port = int(payload_port)
        self.method = (method or "binloader").lower()
        self.payload_path = payload_path if payload_path is not None else default_payload_path()

    def ping(self) -> dict[str, bool | int | None]:
        rpi = is_rpi_online(self.ps4_host)
        etahen = is_etahen_online(self.ps4_host) if not rpi else False
        bin_port: int | None = None
        for port in self.binloader_ports:
            try:
                with socket.create_connection((self.ps4_host, port), timeout=2.0):
                    bin_port = port
                    break
            except OSError:
                continue
        return {
            "rpi": rpi,
            "etahen": etahen,
            "binloader": bin_port is not None,
            "binloader_port": bin_port,
            "pc_host": self.pc_host,
            "payload_port": self.payload_port,
            "http_port": self.http_port,
        }

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
        try:
            server.start()
        except OSError as exc:
            raise InstallError(
                f"Cannot bind HTTP server on 0.0.0.0:{self.http_port} "
                f"(needed so PS4 can pull the PKG from {self.pc_host}): {exc}"
            ) from exc

        status(
            f"HTTP server on {self.pc_host}:{self.http_port} → {server.pkg_url}"
        )

        use_rpi = self.method in {"auto", "rpi"}
        use_bin = self.method in {"auto", "binloader"}
        rpi_up = is_rpi_online(self.ps4_host) if use_rpi else False

        try:
            # With only GoldHEN (no apps open), BinLoader is the real path — prefer it
            # unless user forced rpi or RPI is clearly up in auto mode.
            prefer_bin = self.method == "binloader" or (self.method == "auto" and not rpi_up)

            if use_rpi and rpi_up and not prefer_bin:
                status("Using Remote Package Installer (:12800)…")
                try:
                    push_rpi(self.ps4_host, server.pkg_url)
                except RpiError as exc:
                    if self.method == "rpi":
                        raise InstallError(str(exc)) from exc
                    status(f"RPI failed ({exc}); falling back to BinLoader…")
                else:
                    status("RPI accepted the package — console downloading over LAN…")
                    if wait_transfer:
                        return self._wait_for_download(server, info, "rpi")
                    return SendResult(
                        pkg_name=pkg_path.name,
                        package_size=info.package_size,
                        bytes_sent=server.bytes_sent,
                        download_complete=False,
                        method="rpi",
                    )

            if self.method == "rpi" and not rpi_up:
                raise InstallError(
                    f"RPI not reachable at {self.ps4_host}:12800. "
                    "With only GoldHEN running use install_method = \"binloader\"."
                )

            if not use_bin:
                raise InstallError("No install method available")

            status("Using GoldHEN BinLoader (same as DPI with no other apps open)…")
            return self._send_via_binloader(server, info, wait_transfer=wait_transfer, status=status)
        finally:
            time.sleep(1.0)
            server.stop()

    def _send_via_binloader(
        self,
        server: PkgHttpServer,
        info: PkgInfo,
        *,
        wait_transfer: bool,
        status: Callable[[str], None],
    ) -> SendResult:
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # DPI desktop binds IPAddress.Any + PayloadPort (often 9191 in settings.ini).
        bind_port = self.payload_port if self.payload_port > 0 else 0
        try:
            listen.bind(("0.0.0.0", bind_port))
        except OSError as exc:
            raise InstallError(
                f"Cannot listen on TCP {bind_port} for PS4 callback "
                f"(DPI PayloadPort). Close DPI if it holds the port, or set "
                f"payload_port in config.toml. Details: {exc}"
            ) from exc
        listen.listen(5)
        listen.settimeout(1.0)
        info_port = listen.getsockname()[1]

        bin_port = -1
        client = None
        addr = ("", 0)
        try:
            status(
                f"Listening for PS4 callback on {self.pc_host}:{info_port} "
                f"(allow this port + {self.http_port} for python.exe in firewall)"
            )
            bin_port = self._inject_payload(info_port)
            status(f"Payload injected via BinLoader :{bin_port}, waiting for console…")

            deadline = time.time() + 15.0
            inject_at = time.time()
            reinjected = False
            while time.time() < deadline:
                try:
                    client, addr = listen.accept()
                    break
                except socket.timeout:
                    if not reinjected and (time.time() - inject_at) >= 5.0:
                        status("No callback yet — re-injecting payload…")
                        bin_port = self._inject_payload(info_port)
                        reinjected = True
                        inject_at = time.time()
                    continue

            if client is None:
                raise socket.timeout("PS4 callback timed out")

            status(f"PS4 connected back from {addr[0]}:{addr[1]}")
            with client:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.settimeout(60.0)
                client.sendall(self._pkg_info_buffer(info, server.manifest_url))
            status("Package metadata sent — console downloading over LAN…")

            if wait_transfer:
                return self._wait_for_download(server, info, "binloader")
            return SendResult(
                pkg_name=info.path.name,
                package_size=info.package_size,
                bytes_sent=server.bytes_sent,
                download_complete=False,
                method="binloader",
            )
        except socket.timeout as exc:
            raise InstallError(
                f"PS4 did not connect back to {self.pc_host}:{info_port} "
                f"after BinLoader inject (:{bin_port}).\n"
                "GoldHEN → enable BinLoader (port 9090).\n"
                f"Windows Firewall: allow python.exe inbound TCP {info_port} and {self.http_port}.\n"
                f"Set pc_host to your LAN IP if auto-detect is wrong (now {self.pc_host}).\n"
                "payload_port should match DPI PayloadPort (default 9191)."
            ) from exc
        finally:
            listen.close()

    def _inject_payload(self, info_port: int) -> int:
        if not self.payload_path.is_file():
            raise InstallError(f"Missing payload: {self.payload_path}")

        payload = bytearray(self.payload_path.read_bytes())
        offset = bytes(payload).find(_MARKER)
        if offset < 0:
            raise InstallError("Payload marker not found — vendor/dpi_payload.bin is corrupt")

        # Same patch as DPI EnsureClient: IPv4 bytes + big-endian port at 0xB4 marker
        ip_bytes = socket.inet_aton(self.pc_host)
        port_bytes = struct.pack(">H", info_port)
        payload[offset : offset + 4] = ip_bytes
        payload[offset + 4 : offset + 6] = port_bytes

        last_err: OSError | None = None
        for port in self.binloader_ports:
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection((self.ps4_host, port), timeout=3.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, len(payload))
                sent = sock.send(payload)
                if sent != len(payload):
                    # finish send if partial
                    view = memoryview(payload)[sent:]
                    while view:
                        n = sock.send(view)
                        view = view[n:]
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return port
            except OSError as exc:
                last_err = exc
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        raise InstallError(
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
        method: str,
        idle_limit: float = 45.0,
    ) -> SendResult:
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
                    complete = sent >= total * 0.95
                    break
                if sent == 0 and (time.time() - idle_since) > 90.0:
                    raise InstallError(
                        "Install was queued but the PS4 never downloaded the PKG over HTTP. "
                        f"Allow inbound TCP {self.http_port} on this PC, confirm "
                        f"pc_host={self.pc_host} is the LAN IP the console can reach."
                    )
                time.sleep(0.25)

        return SendResult(
            pkg_name=info.path.name,
            package_size=total,
            bytes_sent=min(server.bytes_sent, total),
            download_complete=complete,
            method=method,
        )


# Back-compat aliases
GoldHenError = InstallError
GoldHenClient = Ps4Installer
