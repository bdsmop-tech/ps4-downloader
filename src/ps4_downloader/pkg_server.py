from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


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
        return f"http://{self.host}:{self.port}/pkg/{self.pkg_path.name}"

    @property
    def manifest_url(self) -> str:
        return f"http://{self.host}:{self.port}/json/0.json"

    def start(self) -> None:
        pkg_path = self.pkg_path
        info = self.info
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = unquote(parsed.path)

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
                                "hashValue": "0" * 40,
                            }
                        ],
                    }
                    body = json.dumps(manifest).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path.startswith("/pkg/"):
                    self._send_file(pkg_path)
                    return

                self.send_error(404)

            def _send_file(self, path: Path) -> None:
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
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()

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

        # Bind all interfaces; URLs still advertise self.host (LAN IP for the PS4).
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
