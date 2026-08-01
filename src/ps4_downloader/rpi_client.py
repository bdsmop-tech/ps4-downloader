from __future__ import annotations

import json
import secrets
from urllib.parse import quote

import httpx


class RpiError(RuntimeError):
    pass


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


def port_12800_open(ps4_host: str, *, timeout: float = 3.0) -> bool:
    """Any HTTP service on :12800 (RPI, etaHEN, or unknown)."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://{ps4_host}:12800/")
            return True
    except httpx.HTTPError:
        pass
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get(f"http://{ps4_host}:12800/api")
            return True
    except httpx.HTTPError:
        return False


def push_rpi(ps4_host: str, package_url: str, *, timeout: float = 30.0) -> dict:
    """Exact DPI PushRPI: POST text JSON to /api/install with UrlEncoded package URL."""
    url = package_url.replace("https://", "http://")
    escaped = quote(url, safe="")
    # DPI builds JSON by string concat — same shape
    body = f'{{"type":"direct","packages":["{escaped}"]}}'
    endpoint = f"http://{ps4_host}:12800/api/install"
    try:
        with httpx.Client(timeout=timeout) as client:
            # DPI uses StringContent → text/plain, NOT application/json
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
            return resp.json()
        except Exception:
            return {"raw": text}
    if "0x80990085" in text:
        raise RpiError(f"PS4 rejected install (free space?): {text}")
    raise RpiError(f"RPI rejected: {text}")


def push_etahen(ps4_host: str, package_url: str, *, timeout: float = 30.0) -> dict:
    """Exact DPI PushEtaHen: multipart POST to /upload with url field."""
    url = package_url.replace("https://", "http://")
    endpoint = f"http://{ps4_host}:12800/upload"
    boundary = "----DirectPackageInstaller_" + secrets.token_hex(16)
    # Mirror DPI: empty file part + url part
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


def push_12800(ps4_host: str, package_url: str, *, timeout: float = 30.0) -> tuple[str, dict]:
    """
    Same decision order as DPI Installer.PushPackage for :12800 services.
    Returns (method_name, response).
    """
    rpi = is_rpi_online(ps4_host, timeout=timeout)
    eta = is_etahen_online(ps4_host, timeout=timeout)
    open_ = port_12800_open(ps4_host, timeout=timeout)

    errors: list[str] = []

    if rpi:
        try:
            return "rpi", push_rpi(ps4_host, package_url, timeout=timeout)
        except RpiError as exc:
            errors.append(str(exc))

    if eta:
        try:
            return "etahen", push_etahen(ps4_host, package_url, timeout=timeout)
        except RpiError as exc:
            errors.append(str(exc))

    # Port open but probes unclear — try both like a confused DPI client
    if open_ and not rpi and not eta:
        try:
            return "rpi", push_rpi(ps4_host, package_url, timeout=timeout)
        except RpiError as exc:
            errors.append(str(exc))
        try:
            return "etahen", push_etahen(ps4_host, package_url, timeout=timeout)
        except RpiError as exc:
            errors.append(str(exc))

    if errors:
        raise RpiError(" | ".join(errors))
    raise RpiError(f"Nothing usable on {ps4_host}:12800")
