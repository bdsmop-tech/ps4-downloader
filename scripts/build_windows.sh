#!/usr/bin/env bash
# Build a portable Windows 11 x64 folder + zip from Linux (no Wine needed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist/ps4-downloader-win64"
PY_VER="3.12.10"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
WORKDIR="$ROOT/dist/_win_build"
WHEELS="$WORKDIR/wheels"

rm -rf "$DIST" "$WORKDIR"
mkdir -p "$DIST/app/src" "$DIST/vendor" "$WHEELS"

echo "==> Downloading Windows embeddable Python ${PY_VER}"
curl -fsSL "$PY_EMBED_URL" -o "$WORKDIR/python-embed.zip"
unzip -q "$WORKDIR/python-embed.zip" -d "$DIST/python"

# Enable site-packages in embeddable distribution
PTH="$(echo "$DIST/python"/python*._pth)"
{
  echo "python312.zip"
  echo "."
  echo "Lib\\site-packages"
  echo "import site"
} > "$PTH"

echo "==> Downloading Windows wheels"
PIP="python3 -m pip"
if [[ -x "$ROOT/.venv/bin/pip" ]]; then
  PIP="$ROOT/.venv/bin/pip"
elif ! python3 -m pip --version >/dev/null 2>&1; then
  python3 -m ensurepip --upgrade 2>/dev/null || true
fi
$PIP download \
  --dest "$WHEELS" \
  --platform win_amd64 \
  --python-version 312 \
  --only-binary=:all: \
  "httpx>=0.28.0" "rich>=13.9.0" "anyio" "certifi" "httpcore" "idna" "h11" "sniffio" "markdown-it-py" "mdurl" "pygments" "typing_extensions"

echo "==> Installing wheels into portable site-packages"
SITE="$DIST/python/Lib/site-packages"
mkdir -p "$SITE"
for whl in "$WHEELS"/*.whl; do
  unzip -q -o "$whl" -d "$SITE"
done

echo "==> Copying application"
cp -a "$ROOT/src/ps4_downloader" "$DIST/app/src/"
rm -rf "$DIST/app/src/ps4_downloader/__pycache__"
cp -a "$ROOT/vendor/dpi_payload.bin" "$DIST/vendor/"
cp "$ROOT/config.example.toml" "$DIST/config.example.toml"
cp "$ROOT/config.example.toml" "$DIST/config.toml"
cp "$ROOT/queue.txt" "$DIST/queue.txt"
mkdir -p "$DIST/downloads"
if [[ -f /home/terminator/Projects/dist-packs/SETUP.txt ]]; then
  cp /home/terminator/Projects/dist-packs/SETUP.txt "$DIST/SETUP.txt"
fi

cat > "$DIST/ps4-downloader.bat" <<'EOF'
@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%~dp0app\src
"%~dp0python\python.exe" -m ps4_downloader %*
EOF

cat > "$DIST/ping.bat" <<'EOF'
@echo off
cd /d "%~dp0"
call "%~dp0ps4-downloader.bat" ping
pause
EOF

cat > "$DIST/installed.bat" <<'EOF'
@echo off
cd /d "%~dp0"
call "%~dp0ps4-downloader.bat" installed
pause
EOF

cat > "$DIST/README.txt" <<'EOF'
PS4 Downloader — portable for Windows 11 (x64)
==============================================

1) Edit config.toml — set your PS4 IP (host = "192.168.x.x")
2) On the PS4 (GoldHEN):
   - Server Settings → enable BinLoader Server (install/send)
   - Server Settings → enable FTP Server (list installed apps), port 2121
3) Allow Windows Firewall for python.exe / port 9898 when sending PKGs

Commands (PowerShell or cmd in this folder):

  ps4-downloader.bat ping
  ps4-downloader.bat installed
  ps4-downloader.bat download
  ps4-downloader.bat send C:\path\to\game.pkg
  ps4-downloader.bat run

Or double-click ping.bat / installed.bat

Put PKG URLs (one per line) into queue.txt for download/run.
EOF

echo "==> Creating zip"
(
  cd "$ROOT/dist"
  rm -f ps4-downloader-win64.zip
  zip -qr ps4-downloader-win64.zip ps4-downloader-win64
)

echo "Done:"
echo "  $DIST"
echo "  $ROOT/dist/ps4-downloader-win64.zip"
du -sh "$DIST" "$ROOT/dist/ps4-downloader-win64.zip"
