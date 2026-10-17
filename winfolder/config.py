# -*- coding: utf-8 -*-
"""Application-wide paths and persisted settings for WinFolder."""
from __future__ import annotations

import json
import logging
import os

APP_DIR_NAME = "WinFolder"


def _data_dir() -> str:
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, APP_DIR_NAME)


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "winfolder.log")
ICON_CACHE_DIR = os.path.join(DATA_DIR, "icon_cache")
LOCK_PATH = os.path.join(DATA_DIR, "winfolder.lock")

# Settings are deliberately few: the rest of the state lives inside the group
# folders themselves (.wf/group.json), so the desktop is the source of truth.
DEFAULTS = {
    "single_click_enabled": True,
    "autostart": False,
    "animations": True,
    "first_run_done": False,
    "welcome_shown": False,
}

_cached: dict | None = None


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ICON_CACHE_DIR, exist_ok=True)


def load() -> dict:
    global _cached
    if _cached is not None:
        return dict(_cached)
    data = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in DEFAULTS:
                        data[key] = value
                    else:
                        data[key] = value
    except Exception as exc:  # noqa: BLE001 - config must never crash startup
        logging.getLogger(__name__).warning("读取配置失败: %s", exc)
    _cached = dict(data)
    return data


def save(settings: dict) -> None:
    global _cached
    _cached = dict(settings)
    ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("保存配置失败: %s", exc)


def set_value(key: str, value) -> dict:
    settings = load()
    settings[key] = value
    save(settings)
    return settings
