from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ps4_downloader.config import load_config
from ps4_downloader.downloader import download_pkg
from ps4_downloader.ftp_client import Ps4FtpClient, Ps4FtpError
from ps4_downloader.installer import InstallError, Ps4Installer
from ps4_downloader.paths import default_config_path
from ps4_downloader.queue import load_queue

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps4-downloader",
        description=(
            "Download PKG files and install them on a PS4 the DirectPackageInstaller way: "
            "Remote Package Installer (:12800) first, GoldHEN BinLoader as fallback."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (default: config.toml next to the app)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Check RPI / BinLoader / FTP reachability")

    installed = sub.add_parser(
        "installed",
        help="List games/apps installed on the PS4 (via GoldHEN FTP → /user/app)",
    )
    installed.add_argument(
        "--ids-only",
        action="store_true",
        help="Only print title IDs (skip reading param.sfo names)",
    )

    dl = sub.add_parser("download", help="Download all URLs from the queue file")
    dl.add_argument("urls", nargs="*", help="Optional PKG URLs (otherwise use queue file)")

    send = sub.add_parser(
        "send",
        help="Send local PKG file(s) to the PS4 (RPI first, like DPI)",
    )
    send.add_argument("files", nargs="+", type=Path, help="Local .pkg path(s)")

    run = sub.add_parser(
        "run",
        help="Download queue locally, then send each PKG to the PS4",
    )
    run.add_argument(
        "--skip-download",
        action="store_true",
        help="Only send already-downloaded PKGs from the download dir",
    )

    return parser


def _client_from_config(cfg) -> Ps4Installer:
    return Ps4Installer(
        cfg.ps4.host,
        pc_host=cfg.ps4.pc_host or None,
        binloader_ports=list(cfg.ps4.binloader_ports),
        http_port=cfg.ps4.http_port,
        payload_port=cfg.ps4.payload_port,
        method=cfg.ps4.install_method,
    )


def _ftp_from_config(cfg) -> Ps4FtpClient:
    return Ps4FtpClient(
        cfg.ps4.host,
        port=cfg.ps4.ftp_port,
        user=cfg.ps4.ftp_user,
        password=cfg.ps4.ftp_password,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config or default_config_path())
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    try:
        client = _client_from_config(cfg)
    except InstallError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.command == "ping":
        info = client.ping()
        ftp_ok = _ftp_from_config(cfg).ping()
        console.print(
            f"PC LAN IP: [cyan]{info['pc_host']}[/cyan]  "
            f"callback:[cyan]{info['payload_port']}[/cyan]  "
            f"http:[cyan]{info['http_port']}[/cyan]  "
            f"method=[cyan]{cfg.ps4.install_method}[/cyan]"
        )
        if info["binloader"]:
            console.print(
                f"[green]BinLoader[/green] {cfg.ps4.host}:{info['binloader_port']}  "
                "← GoldHEN only / DPI path"
            )
        else:
            console.print(
                f"[red]BinLoader[/red] not on {list(cfg.ps4.binloader_ports)} — "
                "GoldHEN → Server Settings → enable BinLoader (9090)"
            )
        if info["rpi"]:
            console.print(f"[dim]RPI[/dim] {cfg.ps4.host}:12800 (optional)")
        if info["etahen"]:
            console.print(f"[dim]etaHEN[/dim] on :12800")
        if ftp_ok:
            console.print(f"[green]FTP[/green] {cfg.ps4.host}:{cfg.ps4.ftp_port}")
        else:
            console.print(f"[dim]FTP[/dim] {cfg.ps4.host}:{cfg.ps4.ftp_port} offline")
        return 0 if info["binloader"] or info["rpi"] or ftp_ok else 1

    if args.command == "installed":
        ftp = _ftp_from_config(cfg)
        try:
            titles = ftp.list_installed(with_names=not args.ids_only)
        except Ps4FtpError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        if not titles:
            console.print("[yellow]No titles found in /user/app[/yellow]")
            return 0
        table = Table(title=f"Installed on {cfg.ps4.host} ({len(titles)})")
        table.add_column("Title ID", style="cyan")
        table.add_column("Name")
        for t in titles:
            table.add_row(t.title_id, t.name or "—")
        console.print(table)
        return 0

    if args.command == "download":
        urls = list(args.urls) or load_queue(cfg.queue_file)
        if not urls:
            console.print("[yellow]Queue is empty. Add URLs to queue.txt or pass them as args.[/yellow]")
            return 1
        for url in urls:
            console.print(f"Downloading [cyan]{url}[/cyan]")
            path = download_pkg(url, cfg.download.dir, resume=cfg.download.resume)
            console.print(f"[green]Saved[/green] {path}")
        return 0

    if args.command == "send":
        for path in args.files:
            console.print(f"Sending [cyan]{path}[/cyan] → PS4 {cfg.ps4.host}")
            try:
                result = client.send_pkg(path, on_status=lambda m: console.print(f"[dim]{m}[/dim]"))
                _print_send_result(result)
            except (InstallError, ValueError, OSError) as exc:
                console.print(f"[red]{exc}[/red]")
                return 1
        return 0

    if args.command == "run":
        urls = load_queue(cfg.queue_file)
        local_files: list[Path] = []
        if args.skip_download:
            local_files = sorted(cfg.download.dir.glob("*.pkg"))
            if not local_files:
                console.print(f"[yellow]No .pkg files in {cfg.download.dir}[/yellow]")
                return 1
        else:
            if not urls:
                console.print(f"[yellow]No URLs in {cfg.queue_file}[/yellow]")
                return 1
            for url in urls:
                console.print(f"Downloading [cyan]{url}[/cyan]")
                path = download_pkg(url, cfg.download.dir, resume=cfg.download.resume)
                console.print(f"[green]Saved[/green] {path}")
                local_files.append(path)

        for path in local_files:
            console.print(f"Sending [cyan]{path.name}[/cyan] → PS4")
            try:
                result = client.send_pkg(path, on_status=lambda m: console.print(f"[dim]{m}[/dim]"))
                _print_send_result(result)
            except (InstallError, ValueError, OSError) as exc:
                console.print(f"[red]{exc}[/red]")
                return 1
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def _print_send_result(result) -> None:
    via = getattr(result, "method", "?")
    if result.download_complete:
        console.print(
            f"[green]Download complete[/green] {result.pkg_name} "
            f"via {via} ({result.bytes_sent}/{result.package_size} bytes). "
            "Installation continues on the PS4 — watch the console notification."
        )
    else:
        console.print(
            f"[yellow]Transfer ended[/yellow] {result.pkg_name} via {via}: "
            f"served {result.bytes_sent}/{result.package_size} bytes "
            f"({result.pct:.0f}%). Check the PS4 download/install notification."
        )


if __name__ == "__main__":
    raise SystemExit(main())
