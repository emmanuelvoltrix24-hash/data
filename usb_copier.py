#!/usr/bin/env python3
"""
USB Auto-Copier
Monitors for new USB drives and copies their contents to a local folder.
"""

import os
import shutil
import time
import platform
import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────
DEST_BASE = os.path.expanduser("~/usb_dumps")   # where to save USB contents
POLL_INTERVAL = 2                                # seconds between checks
# ────────────────────────────────────────────────────────────────────────

def get_mount_points_linux():
    """Return a set of currently mounted device paths that look like removable USB drives."""
    mounts = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mnt = parts[0], parts[1]
                # Typical USB devices: /dev/sd*, /dev/mmcblk*
                if dev.startswith("/dev/sd") or dev.startswith("/dev/mmcblk"):
                    # Skip the root disk (usually sda)
                    if not dev.startswith("/dev/sda"):
                        mounts.add(mnt)
    except Exception:
        pass
    return mounts


def get_mount_points_mac():
    """Return mount points on macOS by parsing 'df' output."""
    mounts = set()
    try:
        out = os.popen("df -l 2>/dev/null").read()
        for line in out.strip().split("\n")[1:]:
            cols = line.split()
            if len(cols) < 6:
                continue
            dev = cols[0]
            mnt = cols[-1]
            if dev.startswith("/dev/disk") and "Volumes" in mnt:
                mounts.add(mnt)
    except Exception:
        pass
    return mounts


def get_mount_points_windows():
    """Return drive letters that are removable on Windows."""
    drives = set()
    try:
        import win32file
        import win32con
        bitmask = win32file.GetLogicalDrives()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if bitmask & 1:
                drive = f"{letter}:\\"
                drivetype = win32file.GetDriveType(drive)
                if drivetype == win32con.DRIVE_REMOVABLE:
                    drives.add(drive)
            bitmask >>= 1
    except Exception:
        # fallback using list of fixed drives
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    import ctypes
                    val = ctypes.windll.kernel32.GetDriveTypeW(drive)
                    if val == 2:  # DRIVE_REMOVABLE
                        drives.add(drive)
                except Exception:
                    drives.add(drive)  # assume removable
                    pass
    return drives


def get_current_mounts():
    """Detect platform and return current removable mount points."""
    system = platform.system()
    if system == "Linux":
        return get_mount_points_linux()
    elif system == "Darwin":
        return get_mount_points_mac()
    elif system == "Windows":
        return get_mount_points_windows()
    else:
        print(f"[!] Unsupported platform: {system}")
        return set()


def copy_usb(src_mount, dest_dir):
    """Recursively copy everything from src_mount into a timestamped folder."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    label = os.path.basename(src_mount.rstrip("/\\")) or f"usb_{timestamp}"
    target = os.path.join(dest_dir, f"{label}_{timestamp}")
    os.makedirs(target, exist_ok=True)

    total = 0
    for root, dirs, files in os.walk(src_mount):
        for fname in files:
            src_path = os.path.join(root, fname)
            # Build relative path to preserve folder structure
            rel = os.path.relpath(src_path, src_mount)
            dst_path = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            try:
                shutil.copy2(src_path, dst_path)
                total += 1
            except Exception as e:
                print(f"    [!] Skipped {src_path}: {e}")
    print(f"    [+] Copied {total} files to {target}")
    return target


def main():
    os.makedirs(DEST_BASE, exist_ok=True)
    print(f"[*] USB Auto-Copier started")
    print(f"[*] Saving to: {DEST_BASE}")
    print(f"[*] Polling every {POLL_INTERVAL}s — insert a USB drive...\n")

    known = get_current_mounts()

    while True:
        time.sleep(POLL_INTERVAL)
        current = get_current_mounts()

        new_drives = current - known
        removed_drives = known - current

        for mnt in new_drives:
            print(f"[+] New USB detected: {mnt}")
            # Short wait for filesystem to settle
            time.sleep(1)
            if os.path.ismount(mnt) or platform.system() == "Windows":
                copy_usb(mnt, DEST_BASE)
            else:
                print(f"    [!] Not mounted yet, skipping.")

        for mnt in removed_drives:
            print(f"[-] USB removed: {mnt}")

        known = current


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Stopped by user.")
