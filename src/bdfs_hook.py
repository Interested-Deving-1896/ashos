"""
bdfs_hook.py — btrfs-dwarfs-framework integration for AshOS

Provides demote-on-delete: when a snapshot is about to be removed by
delete_node(), this hook compresses the rootfs subvolume to a DwarFS
archive via the bdfs CLI before the btrfs subvolume is deleted.  Old OS
states become recoverable compressed archives rather than being discarded.

Usage
-----
Import this module for its side effects before calling delete_node():

    import src.bdfs_hook  # noqa: F401

Or enable it system-wide by adding the import to the top of ashpk_core.py.

The hook is fully opt-in and safe to apply on systems without bdfs: if the
bdfs binary is absent, the BDFS_DEMOTE_ON_DELETE env var is unset, or the
demote command fails, ash falls back to plain btrfs subvolume deletion with
a warning — no snapshot is ever lost due to a bdfs failure.

Configuration
-------------
Set in /etc/bdfs/bdfs.conf (INI format) or via environment variables:

    [ashos]
    demote_on_delete = true          # master switch (default: false)
    archive_dir = /var/lib/bdfs/archives/ashos
    bdfs_bin = bdfs
    compression = zstd               # passed to bdfs demote --compression

Environment overrides (take precedence over config file):
    BDFS_DEMOTE_ON_DELETE=1          # enable the hook
    BDFS_ARCHIVE_DIR=<path>          # override archive directory
    BDFS_BIN=<path>                  # override bdfs binary path
    BDFS_COMPRESSION=<algo>          # zstd | lz4 | zlib | none

Subvolume layout
----------------
AshOS stores each snapshot N as three subvolumes:
    /.snapshots/rootfs/snapshot-N   ← this is demoted to DwarFS
    /.snapshots/boot/boot-N         ← deleted normally (small, not worth archiving)
    /.snapshots/etc/etc-N           ← deleted normally (small config overlay)

Only the rootfs subvolume is demoted; boot and etc are deleted as usual.
"""

import configparser
import logging
import os
import shutil
import subprocess
import time
import types

log = logging.getLogger("ash.bdfs_hook")

# ── Configuration ────────────────────────────────────────────────────────────

_SNAPSHOTS_ROOT = "/.snapshots/rootfs"
_CONFIG_FILE    = "/etc/bdfs/bdfs.conf"
_SECTION        = "ashos"


def _load_config() -> dict:
    """Read /etc/bdfs/bdfs.conf [ashos] section, apply env overrides."""
    cfg = {
        "demote_on_delete": False,
        "archive_dir":      "/var/lib/bdfs/archives/ashos",
        "bdfs_bin":         "bdfs",
        "compression":      "zstd",
    }

    parser = configparser.ConfigParser()
    parser.read(_CONFIG_FILE)
    if parser.has_section(_SECTION):
        if parser.has_option(_SECTION, "demote_on_delete"):
            val = parser.get(_SECTION, "demote_on_delete").strip().lower()
            cfg["demote_on_delete"] = val in ("1", "true", "yes")
        for key in ("archive_dir", "bdfs_bin", "compression"):
            if parser.has_option(_SECTION, key):
                cfg[key] = parser.get(_SECTION, key).strip()

    # Environment overrides
    if os.environ.get("BDFS_DEMOTE_ON_DELETE"):
        cfg["demote_on_delete"] = True
    for env_key, cfg_key in (
        ("BDFS_ARCHIVE_DIR",   "archive_dir"),
        ("BDFS_BIN",           "bdfs_bin"),
        ("BDFS_COMPRESSION",   "compression"),
    ):
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    return cfg


# ── Core demote logic ────────────────────────────────────────────────────────

def _bdfs_available(bdfs_bin: str) -> bool:
    return shutil.which(bdfs_bin) is not None


def demote_snapshot(snap: int, cfg: dict) -> bool:
    """
    Compress /.snapshots/rootfs/snapshot-<snap> to a DwarFS archive.

    Returns True on success, False on any failure (caller should fall back
    to plain btrfs subvolume deletion).
    """
    subvol_path = f"{_SNAPSHOTS_ROOT}/snapshot-{snap}"

    if not os.path.exists(subvol_path):
        log.debug("bdfs_hook: snapshot-%d rootfs not found, skipping demote", snap)
        return False

    if not _bdfs_available(cfg["bdfs_bin"]):
        log.warning(
            "bdfs_hook: bdfs binary '%s' not found — skipping demote for snapshot %d",
            cfg["bdfs_bin"], snap,
        )
        return False

    archive_dir = cfg["archive_dir"]
    try:
        os.makedirs(archive_dir, mode=0o755, exist_ok=True)
    except OSError as exc:
        log.warning("bdfs_hook: cannot create archive dir %s: %s", archive_dir, exc)
        return False

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_name = f"ashos-snapshot-{snap}_{ts}.dwarfs"
    archive_path = os.path.join(archive_dir, archive_name)

    cmd = [
        cfg["bdfs_bin"], "snapshot", "demote",
        "--to-dwarfs",    subvol_path,
        "--output",       archive_path,
        "--compression",  cfg["compression"],
    ]

    log.info(
        "bdfs_hook: demoting snapshot %d → %s (compression: %s)",
        snap, archive_path, cfg["compression"],
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log.error("bdfs_hook: demote timed out for snapshot %d", snap)
        return False
    except OSError as exc:
        log.error("bdfs_hook: failed to exec bdfs: %s", exc)
        return False

    if result.returncode != 0:
        log.error(
            "bdfs_hook: bdfs demote failed for snapshot %d (exit %d):\n%s",
            snap, result.returncode, result.stderr.strip(),
        )
        return False

    log.info("bdfs_hook: snapshot %d archived to %s", snap, archive_path)
    return True


# ── Monkey-patch ─────────────────────────────────────────────────────────────

_PATCHED = False


def _apply_patch() -> None:
    """
    Wrap delete_node() in ashpk_core so that each snapshot's rootfs is
    demoted to DwarFS before the btrfs subvolumes are deleted.

    The patch is idempotent — safe to call multiple times.
    """
    global _PATCHED
    if _PATCHED:
        return

    cfg = _load_config()
    if not cfg["demote_on_delete"]:
        log.debug("bdfs_hook: demote_on_delete is false — patch not applied")
        return

    try:
        import src.ashpk_core as core
    except ImportError as exc:
        log.warning("bdfs_hook: cannot import ashpk_core (%s) — patch skipped", exc)
        return

    _orig_delete_node = core.delete_node

    def patched_delete_node(snaps, quiet=False, nuke=False):
        """
        delete_node() wrapper: demote each snapshot's rootfs to a DwarFS
        archive before delegating to the original deletion logic.

        Demote failures are non-fatal — the snapshot is still deleted.
        """
        for snap in snaps:
            if snap == 0:
                # Base snapshot — never deleted, skip.
                continue
            success = demote_snapshot(snap, cfg)
            if not success:
                print(
                    f"[bdfs] Warning: could not demote snapshot {snap} to DwarFS "
                    f"— proceeding with plain deletion."
                )
        return _orig_delete_node(snaps, quiet=quiet, nuke=nuke)

    core.delete_node = patched_delete_node
    _PATCHED = True
    log.debug("bdfs_hook: patch applied to ashpk_core.delete_node")


_apply_patch()
