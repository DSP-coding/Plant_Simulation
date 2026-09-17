"""
Saving what you change in the app so it's there next time.

Two things persist:

  * The roster. Every change made on the Factory Floor or the Staff &
    Skills tab is written straight back to data/staff_roster.csv (the same
    file the app loads). The first change in a session first copies the
    file to data/backups/staff_roster_<timestamp>.csv, so a bad afternoon
    of dragging can always be undone from the Staff tab. The last
    ROSTER_BACKUPS_TO_KEEP backups are kept.

  * The sidebar settings (intake, targets, area capacities, product mix,
    shifts, remakes, buffers, batching, sick leave, seed). They're saved to
    data/app_settings.json whenever they change and seeded back into the
    widgets on the next open. The file also records what each setting's
    config.py default was when it was saved: a setting you never changed
    keeps following config.py if a later calibration moves the default,
    while a setting you did change stays as you left it.

Both writes are atomic (write a temp file, then swap it in).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

SETTINGS_PATH = Path("data/app_settings.json")
BACKUP_DIR = Path("data/backups")
ROSTER_BACKUPS_TO_KEEP = 20


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """{"values": {key: value}, "defaults": {key: default_at_save_time}} or empty."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "values" in data:
            return {"values": dict(data.get("values", {})), "defaults": dict(data.get("defaults", {}))}
    except (OSError, ValueError):
        pass
    return {"values": {}, "defaults": {}}


def save_settings(values: dict, defaults: dict, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"saved_at": datetime.now().isoformat(timespec="seconds"),
                   "values": values, "defaults": defaults}, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def seeded_value(key: str, default, saved: dict):
    """What a widget should start at: the saved value if the user had
    changed it, the CURRENT config default if they hadn't (so calibration
    updates in config.py still reach untouched settings)."""
    values, defaults = saved.get("values", {}), saved.get("defaults", {})
    if key not in values:
        return default
    saved_value = values[key]
    if key in defaults and defaults[key] == saved_value:
        return default                      # never changed by the user - follow config.py
    return saved_value


def clear_settings(path: Path = SETTINGS_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Roster backups
# ---------------------------------------------------------------------------

def backup_roster(roster_path: str | Path, backup_dir: Path = BACKUP_DIR) -> Path | None:
    """Copy the roster file aside with a timestamp; prune old backups."""
    src = Path(roster_path)
    if not src.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{src.stem}_{stamp}{src.suffix}"
    shutil.copy2(src, dest)
    for old in list_backups(backup_dir)[ROSTER_BACKUPS_TO_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """Newest first."""
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("staff_roster_*.csv"), reverse=True)


def backup_label(path: Path) -> str:
    stamp = path.stem.replace("staff_roster_", "")
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%a %d %b %Y, %H:%M:%S")
    except ValueError:
        return path.name
