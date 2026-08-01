from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class PkgHttpServer:
    """Serve a local PKG (+ BGFT JSON manifest) so the PS4 can download it over LAN."""

    def __init__(self, host: str, port: int, pkg_path: Path, info: dict) -> None:
        self.host = host
        self.port = port
        self.pkg_path = pkg_path.resolve()
        self.info = info
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.bytes_sent = 0
        self._lock = threading.Lock()

    @property
    def pkg_url(self) -> str:
        # DPI-compatible /file/?b64=<abs path>
        b64 = base64.b64encode(str(self.pkg_path).encode("utf-8")).decode("ascii")
        return f"http://{self.host}:{self.port}/file/?b64={b64}"

    @property
    def manifest_url(self) -> str:
        return f"http://{self.host}:{self.port}/json/0.json"

    def start(self) -> None:
        pkg_path = self.pkg_path
        info = self.info
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                return

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle(body=False)

            def do_GET(self) -> None:  # noqa: N802
                self._handle(body=True)

            def _handle(self, *, body: bool) -> None:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query)

                if path.startswith("/json/"):
                    manifest = {
                        "originalFileSize": info["package_size"],
                        "packageDigest": info["digest"],
                        "numberOfSplitFiles": 1,
                        "pieces": [
                            {
                                "url": server.pkg_url,
                                "fileOffset": 0,
                                "fileSize": info["package_size"],
                                "hashValue": "0000000000000000000000000000000000000000",
                            }
                        ],
                    }
                    raw = json.dumps(manifest, indent=2).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if body:
                        self.wfile.write(raw)
                    return

                if path.startswith("/file"):
                    target = pkg_path
                    if "b64" in query:
                        try:
                            decoded = base64.b64decode(query["b64"][0]).decode("utf-8")
                            candidate = Path(decoded)
                            if candidate.is_file():
                                target = candidate
                        except Exception:
                            pass
                    self._send_file(target, body=body)
                    return

                if path.startswith("/pkg/"):
                    self._send_file(pkg_path, body=body)
                    return

                self.send_error(404)

            def _send_file(self, path: Path, *, body: bool) -> None:
                size = path.stat().st_size
                range_header = self.headers.get("Range")
                start, end = 0, size - 1
                status = 200
                if range_header and range_header.startswith("bytes="):
                    spec = range_header[6:].split(",")[0].strip()
                    if "-" in spec:
                        a, b = spec.split("-", 1)
                        if a:
                            start = int(a)
                        if b:
                            end = int(b)
                        status = 206

                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Connection", "close")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not body:
                    return

                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 256, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                        with server._lock:
                            server.bytes_sent += len(chunk)

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
