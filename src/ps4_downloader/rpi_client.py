from __future__ import annotations

import json
import secrets
import socket
from urllib.parse import quote

import httpx


class RpiError(RuntimeError):
    pass


def tcp_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_rpi_online(ps4_host: str, *, timeout: float = 3.0) -> bool:
    """DPI IsRPIOnline: GET /api contains Unsupported method + fail."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://{ps4_host}:12800/api")
            body = resp.text
            return "Unsupported method" in body and "fail" in body
    except httpx.HTTPError:
        return False


def is_etahen_online(ps4_host: str, *, timeout: float = 3.0) -> bool:
    """DPI IsEtaHenOnline: GET / contains etaHEN."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://{ps4_host}:12800/")
            return "etaHEN" in resp.text
    except httpx.HTTPError:
        return False


def port_12800_open(ps4_host: str, *, timeout: float = 2.0) -> bool:
    """TCP accept on :12800 (may be a zombie — not proof of RPI/etaHEN)."""
    return tcp_open(ps4_host, 12800, timeout=timeout)


def classify_12800(ps4_host: str, *, timeout: float = 3.0) -> str:
    """
    What is on :12800?
      closed | rpi | etahen | http_other | tcp_zombie
    tcp_zombie = SYN-ACK then HTTP gets reset/timeout (not usable for install).
    """
    if not tcp_open(ps4_host, 12800, timeout=2.0):
        return "closed"
    if is_rpi_online(ps4_host, timeout=timeout):
        return "rpi"
    if is_etahen_online(ps4_host, timeout=timeout):
        return "etahen"
    # Any HTTP response at all?
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get(f"http://{ps4_host}:12800/")
            return "http_other"
    except httpx.HTTPError:
        pass
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get(f"http://{ps4_host}:12800/api")
            return "http_other"
    except httpx.HTTPError:
        return "tcp_zombie"


def goldhen_http_ready(ps4_host: str, *, timeout: float = 2.0) -> bool:
    """DPI IsGoldHENOnline: GET :9090/status → status ready."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://{ps4_host}:9090/status")
            compact = resp.text.replace(" ", "")
            return '"status":"ready"' in compact
    except httpx.HTTPError:
        return False


def push_rpi(ps4_host: str, package_url: str, *, timeout: float = 60.0) -> dict:
    """Exact DPI PushRPI: POST text JSON to /api/install with UrlEncoded package URL."""
    url = package_url.replace("https://", "http://")
    escaped = quote(url, safe="")
    body = f'{{"type":"direct","packages":["{escaped}"]}}'
    endpoint = f"http://{ps4_host}:12800/api/install"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                endpoint,
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            text = resp.text
    except httpx.HTTPError as exc:
        raise RpiError(f"RPI /api/install failed: {exc}") from exc

    if '"success"' in text or "success" in text.lower():
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text}
    if "0x80990085" in text:
        raise RpiError(f"PS4 rejected install (free space?): {text}")
    raise RpiError(f"RPI rejected: {text}")


def push_etahen(ps4_host: str, package_url: str, *, timeout: float = 60.0) -> dict:
    """Exact DPI PushEtaHen: multipart POST to /upload with url field."""
    url = package_url.replace("https://", "http://")
    endpoint = f"http://{ps4_host}:12800/upload"
    boundary = "----DirectPackageInstaller_" + secrets.token_hex(16)
    parts: list[bytes] = []
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename=""\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
            f"\r\n"
        ).encode("utf-8")
    )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="url"\r\n\r\n'
            f"{url}\r\n"
        ).encode("utf-8")
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                endpoint,
                content=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            text = resp.text
    except httpx.HTTPError as exc:
        raise RpiError(f"etaHEN /upload failed: {exc}") from exc

    if "SUCCESS:" in text:
        return {"raw": text}
    if "0x80990085" in text:
        raise RpiError(f"PS4 rejected install (free space?): {text}")
    raise RpiError(f"etaHEN rejected: {text}")


def push_12800(ps4_host: str, package_url: str, *, timeout: float = 60.0) -> tuple[str, dict]:
    """
    DPI order: only Push when HTTP probe says RPI or etaHEN.
    Bare TCP on :12800 is NOT enough (zombie listeners RST HTTP — WinError 10054).
    """
    kind = classify_12800(ps4_host, timeout=3.0)
    if kind == "closed":
        raise RpiError(f"TCP {ps4_host}:12800 closed")
    if kind == "tcp_zombie":
        raise RpiError(
            f"TCP {ps4_host}:12800 accepts then resets HTTP (not RPI/etaHEN). "
            "Skip this port — enable GoldHEN BinLoader or launch Remote Package Installer."
        )
    if kind == "http_other":
        # Unknown HTTP — still try DPI endpoints (some forks answer differently on GET).
        errors: list[str] = []
        for name, fn in (("rpi", push_rpi), ("etahen", push_etahen)):
            try:
                return name, fn(ps4_host, package_url, timeout=timeout)
            except RpiError as exc:
                errors.append(f"{name}: {exc}")
        raise RpiError(" | ".join(errors))

    if kind == "rpi":
        return "rpi", push_rpi(ps4_host, package_url, timeout=timeout)
    if kind == "etahen":
        return "etahen", push_etahen(ps4_host, package_url, timeout=timeout)
    raise RpiError(f"Unexpected :12800 class {kind!r}")
