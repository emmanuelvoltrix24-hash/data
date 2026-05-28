#!/usr/bin/env python3
"""
USB Copier Pro — advanced USB auto-ingestion tool.

Features:
  • Cross‑platform (Linux / macOS / Windows)
  • Config file (YAML) & CLI overrides
  • File‑type whitelist / blacklist (glob patterns)
  • Max file size filter
  • Auto‑eject after copy
  • Checksum verification (SHA‑256)
  • Desktop notifications
  • Logging to file & console
  • USB whitelist (by volume label)
  • Safe mode (read‑only, no eject)
  • Dry‑run mode
  • Remote upload (SFTP)
  • System tray icon with pause/quit
  • Daemon mode – detaches from terminal

Dependencies (pip install as needed):
    pyyaml pystray pillow pysftp plyer psutil
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Set, Optional, List, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None  # type: ignore

try:
    import plyer
except ImportError:
    plyer = None  # type: ignore

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


# ────────────────────────────────────────────────────────────────────────
#  DEFAULT CONFIG (merged with YAML file if present)
# ────────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "destination": os.path.expanduser("~/usb_dumps"),
    "poll_interval": 2.0,
    "settle_delay": 1.5,
    "log_file": os.path.expanduser("~/usb_copier.log"),
    "log_level": "INFO",
    "max_file_size_mb": 0,                # 0 = unlimited
    "include_patterns": ["*"],            # globs – only these match
    "exclude_patterns": [],               # globs – skip these
    "exclude_hidden": True,
    "checksum": False,                    # verify copy with SHA‑256
    "auto_eject": False,                  # eject USB after copy
    "safe_mode": False,                   # never eject, read‑only
    "dry_run": False,                     # only print what would be done
    "notifications": True,                # desktop notifications
    "usb_whitelist_labels": [],           # only these volume labels
    "remote": {
        "enabled": False,
        "host": "",
        "port": 22,
        "username": "",
        "password": "",                   # optional, key‑based auth preferred
        "remote_path": "/tmp/usb_dumps",
    },
    "snapshot_metadata": True,            # save a .meta.json per dump
}

CONFIG_PATH = os.path.expanduser("~/.usb_copier_config.yaml")

log = logging.getLogger("usb_copier")


# ────────────────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ────────────────────────────────────────────────────────────────────────

def _readable_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} TB"


def _notify(title: str, message: str, config: dict) -> None:
    if not config.get("notifications", True) or plyer is None:
        return
    try:
        plyer.notification.notify(title=title, message=message, timeout=5)
    except Exception:
        pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _matches_any(path: str, patterns: List[str]) -> bool:
    """Return True if *path* matches any of the glob patterns."""
    from fnmatch import fnmatch
    name = os.path.basename(path)
    for pat in patterns:
        if fnmatch(name, pat) or fnmatch(path, pat):
            return True
    return False


def _should_copy(src_path: str, config: dict) -> Tuple[bool, str]:
    """Return (allow, reason) for a given source path."""
    name = os.path.basename(src_path)

    # Hidden files/folders
    if config.get("exclude_hidden", True) and name.startswith("."):
        return False, "hidden"

    # Exclude patterns
    if _matches_any(src_path, config.get("exclude_patterns", [])):
        return False, "excluded by pattern"

    # Include patterns (must match at least one)
    if not _matches_any(src_path, config.get("include_patterns", ["*"])):
        return False, "not in include patterns"

    # Max file size
    max_mb = config.get("max_file_size_mb", 0)
    if max_mb > 0 and os.path.isfile(src_path):
        sz = os.path.getsize(src_path)
        if sz > max_mb * 1024 * 1024:
            return False, f"size {_readable_size(sz)} > limit {max_mb} MB"

    return True, ""


# ────────────────────────────────────────────────────────────────────────
#  USB DETECTION (platform‑aware)
# ────────────────────────────────────────────────────────────────────────

class UsbDetector:
    """Detect removable mount points."""

    @staticmethod
    def current_mounts() -> Set[str]:
        system = platform.system()
        if system == "Linux":
            return UsbDetector._linux()
        elif system == "Darwin":
            return UsbDetector._macos()
        elif system == "Windows":
            return UsbDetector._windows()
        return set()

    @staticmethod
    def _linux() -> Set[str]:
        mounts = set()
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    dev, mnt = parts[0], parts[1]
                    if re.match(r"/dev/sd[b-z]", dev) or re.match(r"/dev/mmcblk[0-9]", dev):
                        mounts.add(mnt)
        except Exception:
            pass
        return mounts

    @staticmethod
    def _macos() -> Set[str]:
        mounts = set()
        try:
            out = subprocess.check_output(["df", "-l"], text=True, stderr=subprocess.DEVNULL)
            for line in out.strip().split("\n")[1:]:
                cols = line.split()
                if len(cols) >= 6 and cols[0].startswith("/dev/disk") and "/Volumes/" in cols[-1]:
                    mounts.add(cols[-1])
        except Exception:
            pass
        return mounts

    @staticmethod
    def _windows() -> Set[str]:
        drives = set()
        try:
            import win32file
            import win32con
            bitmask = win32file.GetLogicalDrives()
            for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
                if bitmask & (1 << i):
                    drive = f"{letter}:\\"
                    if win32file.GetDriveType(drive) == win32con.DRIVE_REMOVABLE:
                        drives.add(drive)
        except Exception:
            # fallback
            import ctypes
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    try:
                        if ctypes.windll.kernel32.GetDriveTypeW(drive) == 2:
                            drives.add(drive)
                    except Exception:
                        drives.add(drive)
        return drives

    @staticmethod
    def volume_label(mount_point: str) -> str:
        """Return the volume label of a mounted drive."""
        label = os.path.basename(mount_point.rstrip("/\\"))
        if platform.system() == "Linux":
            # Try blkid for a friendlier label
            try:
                out = subprocess.check_output(
                    ["blkid", "-o", "value", "-s", "LABEL"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
                if out:
                    return out.split("\n")[0]
            except Exception:
                pass
        return label or "UNTITLED"


# ────────────────────────────────────────────────────────────────────────
#  CORE: COPY LOGIC
# ────────────────────────────────────────────────────────────────────────

def copy_usb(
    src_mount: str,
    config: dict,
    eject_callback=None,
) -> Optional[str]:
    """
    Copy contents of *src_mount* into destination folder.
    Returns the destination path, or None on failure.
    """
    label = UsbDetector.volume_label(src_mount)
    # Whitelist check
    whitelist = config.get("usb_whitelist_labels", [])
    if whitelist and label not in whitelist:
        log.info("Skipping %s (label '%s' not in whitelist)", src_mount, label)
        _notify("USB Copier", f"Skipped {label}: not whitelisted", config)
        return None

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)[:40]
    dest_dir = os.path.join(config["destination"], f"{safe_label}_{timestamp}")
    os.makedirs(dest_dir, exist_ok=True)

    dry_run = config.get("dry_run", False)
    checksum = config.get("checksum", False)
    max_mb = config.get("max_file_size_mb", 0)
    snapshot = config.get("snapshot_metadata", False)

    total_files = 0
    total_bytes = 0
    skipped = 0
    meta_entries = []

    for root, dirs, files in os.walk(src_mount):
        # Prune hidden dirs if needed
        if config.get("exclude_hidden", True):
            dirs[:] = [d for d in dirs if not d.startswith(".")]

        for fname in files:
            src_path = os.path.join(root, fname)
            rel = os.path.relpath(src_path, src_mount)
            dst_path = os.path.join(dest_dir, rel)

            allow, reason = _should_copy(src_path, config)
            if not allow:
                log.debug("  SKIP  %s  (%s)", rel, reason)
                skipped += 1
                continue

            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            sz = os.path.getsize(src_path)
            total_bytes += sz

            if dry_run:
                log.info("  [DRY]  %s  (%s)", rel, _readable_size(sz))
                total_files += 1
                meta_entries.append({"source": rel, "size": sz, "status": "dry_run"})
                continue

            try:
                shutil.copy2(src_path, dst_path)
                if checksum:
                    src_hash = _sha256_file(src_path)
                    dst_hash = _sha256_file(dst_path)
                    if src_hash != dst_hash:
                        log.error("  CHECKSUM MISMATCH: %s", rel)
                        meta_entries.append({"source": rel, "size": sz, "status": "checksum_fail"})
                        continue
                log.info("  OK    %s  (%s)", rel, _readable_size(sz))
                total_files += 1
                meta_entries.append({"source": rel, "size": sz, "status": "copied", "sha256": src_hash if checksum else ""})
            except Exception as e:
                log.error("  FAIL  %s  %s", rel, e)
                meta_entries.append({"source": rel, "size": sz, "status": "error", "error": str(e)})

    # Save metadata snapshot
    if snapshot and not dry_run and meta_entries:
        meta_path = os.path.join(dest_dir, ".meta.json")
        try:
            with open(meta_path, "w") as f:
                json.dump({
                    "volume_label": label,
                    "mount_point": src_mount,
                    "timestamp": timestamp,
                    "total_files_copied": total_files,
                    "total_bytes": total_bytes,
                    "entries": meta_entries,
                }, f, indent=2)
            log.info("  [META] Saved %s", meta_path)
        except Exception as e:
            log.warning("  Could not write metadata: %s", e)

    summary = (
        f"USB '{label}' copied: {total_files} files "
        f"({_readable_size(total_bytes)}), {skipped} skipped"
    )
    log.info(summary)
    _notify("USB Copier — Done", summary, config)

    # Auto‑eject
    if config.get("auto_eject", False) and not config.get("safe_mode", False) and not dry_run:
        _eject_volume(src_mount, label)

    if eject_callback:
        eject_callback()

    return dest_dir


def _eject_volume(mount_point: str, label: str) -> None:
    """Try to eject the USB drive."""
    system = platform.system()
    log.info("Ejecting %s (%s) …", label, mount_point)
    try:
        if system == "Linux":
            subprocess.run(["udisksctl", "unmount", "-b", mount_point], check=False)
            subprocess.run(["udisksctl", "power-off", "-b", mount_point], check=False)
        elif system == "Darwin":
            disk = subprocess.check_output(
                ["df", mount_point], text=True
            ).split("\n")[1].split()[0]
            subprocess.run(["diskutil", "unmount", disk], check=False)
        elif system == "Windows":
            # Windows doesn't have a simple CLI; ignore
            pass
    except Exception as e:
        log.warning("Eject failed: %s", e)


# ────────────────────────────────────────────────────────────────────────
#  REMOTE UPLOAD (SFTP)
# ────────────────────────────────────────────────────────────────────────

def remote_upload(local_path: str, config: dict) -> bool:
    """SFTP‑upload a folder to remote host."""
    remote_cfg = config.get("remote", {})
    if not remote_cfg.get("enabled"):
        return False

    try:
        import pysftp
    except ImportError:
        log.error("pysftp not installed – cannot upload remotely")
        return False

    host = remote_cfg["host"]
    port = remote_cfg.get("port", 22)
    username = remote_cfg.get("username", "")
    password = remote_cfg.get("password") or None
    remote_path = remote_cfg.get("remote_path", "/tmp/usb_dumps")

    cnopts = pysftp.CnOpts()
    cnopts.hostkeys = None  # trust all (dev env)

    try:
        with pysftp.Connection(
            host, username=username, password=password,
            port=port, cnopts=cnopts,
        ) as sftp:
            sftp.put_r(local_path, remote_path, preserve_mtime=True)
        log.info("Remote upload completed to %s:%s", host, remote_path)
        _notify("USB Copier", f"Uploaded to {host}", config)
        return True
    except Exception as e:
        log.error("Remote upload failed: %s", e)
        return False


# ────────────────────────────────────────────────────────────────────────
#  SYSTEM TRAY
# ────────────────────────────────────────────────────────────────────────

def _create_tray_icon(stop_event: threading.Event, config: dict) -> Optional[pystray.Icon]:
    if pystray is None:
        return None

    # Generate a 64x64 icon
    img = Image.new("RGB", (64, 64), (30, 144, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 20, 54, 44], fill=(255, 255, 255))  # USB shape
    draw.text((18, 24), "U", fill=(30, 144, 255))

    menu = pystray.Menu(
        pystray.MenuItem("Pause / Resume", lambda: _toggle_pause(stop_event)),
        pystray.MenuItem("Quit", lambda: _quit(stop_event)),
    )
    icon = pystray.Icon("usb_copier", img, "USB Copier Pro", menu)
    return icon


def _toggle_pause(stop_event: threading.Event) -> None:
    if hasattr(stop_event, "_paused"):
        stop_event._paused = not stop_event._paused
    else:
        stop_event._paused = False
    log.info("Paused = %s", stop_event._paused)


def _quit(stop_event: threading.Event) -> None:
    stop_event.set()


def _run_tray(stop_event: threading.Event, config: dict) -> None:
    icon = _create_tray_icon(stop_event, config)
    if icon:
        icon.run()


# ────────────────────────────────────────────────────────────────────────
#  DAEMON MODE
# ────────────────────────────────────────────────────────────────────────

def daemonize() -> None:
    """Detach from terminal (Unix only)."""
    if platform.system() not in ("Linux", "Darwin"):
        return
    pid = os.fork()
    if pid > 0:
        sys.exit(0)  # parent exits
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open("/dev/null", "w") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())


# ────────────────────────────────────────────────────────────────────────
#  CONFIG LOADER
# ────────────────────────────────────────────────────────────────────────

def load_config(path: str = CONFIG_PATH) -> dict:
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(path) and yaml is not None:
        try:
            with open(path) as f:
                file_cfg = yaml.safe_load(f)
            if file_cfg:
                _deep_merge(config, file_cfg)
            log.info("Loaded config from %s", path)
        except Exception as e:
            log.warning("Failed to load config %s: %s", path, e)
    return config


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def write_default_config(path: str = CONFIG_PATH) -> None:
    if yaml is None:
        log.error("PyYAML not installed – cannot write default config")
        return
    with open(path, "w") as f:
        yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
    log.info("Default config written to %s", path)


# ────────────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ────────────────────────────────────────────────────────────────────────

def run(config: dict, stop_event: threading.Event) -> None:
    log.info("=" * 50)
    log.info("USB Copier Pro started")
    log.info("Destination: %s", config["destination"])
    log.info("Poll interval: %.1fs", config["poll_interval"])
    log.info("Dry run: %s", config.get("dry_run", False))
    log.info("=" * 50)

    os.makedirs(config["destination"], exist_ok=True)

    known = UsbDetector.current_mounts()
    log.info("Current mounts: %s", known)

    while not stop_event.is_set():
        # Check pause
        if getattr(stop_event, "_paused", False):
            time.sleep(1)
            continue

        time.sleep(config.get("poll_interval", 2.0))
        current = UsbDetector.current_mounts()

        new_drives = current - known
        removed_drives = known - current

        for mnt in sorted(new_drives):
            log.info("[+] USB inserted: %s", mnt)
            _notify("USB Copier", f"USB detected: {mnt}", config)
            time.sleep(config.get("settle_delay", 1.5))
            if os.path.ismount(mnt) or platform.system() == "Windows":
                dest = copy_usb(mnt, config)
                if dest and config.get("remote", {}).get("enabled"):
                    threading.Thread(target=remote_upload, args=(dest, config), daemon=True).start()
            else:
                log.warning("  Mount point vanished before copy.")

        for mnt in sorted(removed_drives):
            log.info("[-] USB removed: %s", mnt)

        known = current


def main() -> None:
    parser = argparse.ArgumentParser(
        description="USB Copier Pro — automatically copy USB drives",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s                          # run with defaults / config
              %(prog)s --dry-run                # preview without copying
              %(prog)s --dest /mnt/backups      # custom destination
              %(prog)s --include "*.jpg,*.png"  # only images
              %(prog)s --exclude "*.exe,*.dll"  # skip binaries
              %(prog)s --max-size 100           # skip files >100 MB
              %(prog)s --eject                  # eject after copy
              %(prog)s --daemon                 # run in background
              %(prog)s --init-config            # write default ~/.usb_copier_config.yaml
        """),
    )
    parser.add_argument("--dest", help="Destination folder")
    parser.add_argument("--poll", type=float, help="Poll interval (seconds)")
    parser.add_argument("--include", help="Comma‑separated glob patterns to include")
    parser.add_argument("--exclude", help="Comma‑separated glob patterns to exclude")
    parser.add_argument("--max-size", type=float, help="Max file size in MB (0=unlimited)")
    parser.add_argument("--eject", action="store_true", help="Auto‑eject after copy")
    parser.add_argument("--checksum", action="store_true", help="Verify copy with SHA‑256")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no copy")
    parser.add_argument("--no-notify", action="store_true", help="Disable desktop notifications")
    parser.add_argument("--daemon", action="store_true", help="Fork to background (Unix)")
    parser.add_argument("--init-config", action="store_true", help="Write default config file and exit")
    parser.add_argument("--config", help="Path to YAML config file", default=CONFIG_PATH)
    args = parser.parse_args()

    # Handle --init-config
    if args.init_config:
        write_default_config(args.config)
        sys.exit(0)

    # Load config
    config = load_config(args.config)

    # CLI overrides
    if args.dest:
        config["destination"] = os.path.expanduser(args.dest)
    if args.poll is not None:
        config["poll_interval"] = args.poll
    if args.include:
        config["include_patterns"] = [p.strip() for p in args.include.split(",")]
    if args.exclude:
        config["exclude_patterns"] = [p.strip() for p in args.exclude.split(",")]
    if args.max_size is not None:
        config["max_file_size_mb"] = args.max_size
    if args.eject:
        config["auto_eject"] = True
    if args.checksum:
        config["checksum"] = True
    if args.dry_run:
        config["dry_run"] = True
        config["auto_eject"] = False
        config["remote"]["enabled"] = False
    if args.no_notify:
        config["notifications"] = False

    # Setup logging
    log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.get("log_file", "usb_copier.log")),
        ],
    )

    # Daemon mode
    if args.daemon and platform.system() in ("Linux", "Darwin"):
        daemonize()
        log.info("Daemonized (PID %d)", os.getpid())

    # Shared stop event
    stop_event = threading.Event()

    # Tray icon in separate thread
    if pystray and not config.get("dry_run", False):
        t = threading.Thread(target=_run_tray, args=(stop_event, config), daemon=True)
        t.start()

    # Handle Ctrl+C
    def _signal_handler(sig, frame):
        log.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        run(config, stop_event)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        log.info("USB Copier Pro stopped.")


if __name__ == "__main__":
    main()
