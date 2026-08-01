from __future__ import annotations

import json
from urllib.parse import quote

import httpx


class RpiError(RuntimeError):
    pass


def is_rpi_online(ps4_host: str, *, timeout: float = 3.0) -> bool:
    """Same probe DirectPackageInstaller uses (GET /api → Unsupported method)."""
    url = f"http://{ps4_host}:12800/api"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            body = resp.text
            return "Unsupported method" in body and "fail" in body
    except httpx.HTTPError:
        return False


def is_etahen_online(ps4_host: str, *, timeout: float = 3.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://{ps4_host}:12800/")
            return "etaHEN" in resp.text
    except httpx.HTTPError:
        return False


def _looks_success(raw: str, data: object) -> bool:
    low = raw.lower()
    if "success" in low:
        return True
    if "fail" in low or "error" in low:
        return False
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status in {"success", "0"}:
            return True
    return False


def push_rpi(ps4_host: str, package_url: str, *, timeout: float = 30.0) -> dict:
    """Tell Remote Package Installer to queue a direct PKG URL (DPI-compatible)."""
    url = package_url.replace("https://", "http://")
    endpoint = f"http://{ps4_host}:12800/api/install"
    last_error = ""

    # 1) raw URL in JSON (most senders)  2) DPI UrlEncode form
    for packages in ([url], [quote(url, safe="")]):
        payload = {"type": "direct", "packages": packages}
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(endpoint, json=payload)
                text = resp.text
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    data = {"raw": text}
        except httpx.HTTPError as exc:
            last_error = str(exc)
            continue

        raw = text if isinstance(text, str) else str(data)
        if "0x80990085" in raw:
            raise RpiError(f"PS4 rejected install (likely not enough free space): {raw}")
        if _looks_success(raw, data):
            return data if isinstance(data, dict) else {"raw": raw}
        last_error = raw

    raise RpiError(f"PS4 install rejected: {last_error}")
