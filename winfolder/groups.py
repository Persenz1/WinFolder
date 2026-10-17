# -*- coding: utf-8 -*-
"""Group model.

A *group* is a plain folder on the user's desktop that contains a hidden
``.wf/group.json`` marker.  The folder keeps its normal Windows behaviour
(drag shortcuts in, F2 rename, double-click to open) while WinFolder draws an
Android-style preview icon for it and opens a popup panel on single click.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import icons, util

log = logging.getLogger(__name__)

MARKER_DIR = ".wf"
MARKER_FILE = "group.json"
ORDER_FILE = "order.json"
DESKTOP_INI = "desktop.ini"
APP_ID = "WinFolder"
MARKER_VERSION = 1
ORDER_VERSION = 1


@dataclass
class Group:
    """A desktop folder that WinFolder treats as an Android-style folder."""

    path: str
    marker: dict

    @property
    def name(self) -> str:
        return os.path.basename(self.path.rstrip("\\/"))

    @property
    def group_id(self) -> str:
        return str(self.marker.get("id", ""))

    @property
    def created(self) -> str:
        return str(self.marker.get("created", ""))

    @property
    def apps(self) -> list[str]:
        return app_entries(self.path)

    @property
    def count(self) -> int:
        return len(self.apps)

    def exists(self) -> bool:
        return os.path.isdir(self.path) and read_marker(self.path) is not None

    def refresh_icon(self) -> bool:
        try:
            return icons.apply_group_icon(self.path, self.apps)
        except Exception as exc:  # noqa: BLE001
            log.warning("刷新图标失败 %s: %s", self.path, exc)
            return False

    def needs_icon_refresh(self) -> bool:
        try:
            return icons.needs_icon_refresh(self.path, self.apps)
        except Exception as exc:  # noqa: BLE001
            log.debug("检查图标失败 %s: %s", self.path, exc)
            return False


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------

def marker_path(folder: str) -> str:
    return os.path.join(folder, MARKER_DIR, MARKER_FILE)


def read_marker(folder: str) -> dict | None:
    path = marker_path(folder)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        return None
    return None


def _write_marker(folder: str, marker: dict) -> None:
    directory = os.path.join(folder, MARKER_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, MARKER_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(marker, f, ensure_ascii=False, indent=2)
    util.hide_system(path)
    util.hide_system(directory)


def is_group_folder(path: str) -> bool:
    return os.path.isdir(path) and read_marker(path) is not None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_groups() -> list[Group]:
    groups: list[Group] = []
    for base in util.desktop_dirs():
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    marker = read_marker(entry.path)
                    if marker is not None:
                        groups.append(Group(entry.path, marker))
        except OSError as exc:
            log.warning("扫描桌面失败 %s: %s", base, exc)
    groups.sort(key=lambda g: util.natural_key(g.name))
    return groups


def group_by_name(name: str) -> Group | None:
    target = (name or "").strip()
    if not target:
        return None
    groups = discover_groups()
    for group in groups:
        if group.name == target:
            return group
    for group in groups:  # tolerate case differences
        if group.name.casefold() == target.casefold():
            return group
    return None


# ---------------------------------------------------------------------------
# Folder contents
# ---------------------------------------------------------------------------

def order_path(folder: str) -> str:
    """分组自定义排序文件的路径（``<group>/.wf/order.json``）。"""
    return os.path.join(folder, MARKER_DIR, ORDER_FILE)


def read_app_order(folder: str) -> list[str]:
    """读取用户自定义的应用顺序（basename 列表）；文件缺失/损坏时返回空列表。"""
    path = order_path(folder)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw_order = data.get("order")
    if not isinstance(raw_order, list):
        return []
    order: list[str] = []
    for item in raw_order:
        if isinstance(item, str) and item and item not in order:
            order.append(item)
    return order


def save_app_order(folder: str, app_paths: "list[str] | tuple[str, ...]") -> None:
    """把应用顺序写入 ``<group>/.wf/order.json``（原子写入 + 隐藏/系统属性）。

    *app_paths* 为应用完整路径（或 basename），按期望显示顺序排列。
    """
    names: list[str] = []
    for item in app_paths:
        name = os.path.basename(str(item).rstrip("\\/"))
        if name and name not in names:
            names.append(name)
    directory = os.path.join(folder, MARKER_DIR)
    os.makedirs(directory, exist_ok=True)
    payload = {"app": APP_ID, "version": ORDER_VERSION, "order": names}
    target = order_path(folder)
    temp = target + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(temp, target)
        util.hide_system(target)
        util.hide_system(directory)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def order_app_entries(entries: list[str], folder: str) -> list[str]:
    """按 ``order.json`` 重排条目：已记录的按记录顺序，未记录的自然排序追加。"""
    order = read_app_order(folder)
    if not order:
        return entries
    recorded: dict[str, int] = {}
    for index, name in enumerate(order):
        recorded.setdefault(name, index)
    stored: list[str] = []
    rest: list[str] = []
    for path in entries:
        name = os.path.basename(path)
        if name in recorded:
            stored.append(path)
        else:
            rest.append(path)
    stored.sort(key=lambda path: recorded[os.path.basename(path)])
    rest.sort(key=lambda path: util.natural_key(os.path.basename(path)))
    return stored + rest


def app_entries(folder: str) -> list[str]:
    """Launchable entries in *folder* (files and sub-folders), order-aware."""
    entries: list[str] = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                name = entry.name
                if name.lower() == DESKTOP_INI or name == MARKER_DIR or name.startswith("."):
                    continue
                if util.is_hidden_entry(entry):
                    continue
                try:
                    if not (entry.is_file(follow_symlinks=True) or
                            entry.is_dir(follow_symlinks=True)):
                        continue
                except OSError:
                    continue
                entries.append(entry.path)
    except OSError as exc:
        log.warning("读取分组内容失败 %s: %s", folder, exc)
    entries.sort(key=lambda path: util.natural_key(os.path.basename(path)))
    return order_app_entries(entries, folder)


# ---------------------------------------------------------------------------
# Create / rename / delete
# ---------------------------------------------------------------------------

def create_group(name: str) -> Group:
    """Create a new empty group folder on the user's desktop."""
    display_name = util.sanitize_name(name)
    desktop = util.user_desktop()
    path = util.unique_path(desktop, display_name)
    os.makedirs(path)
    marker = {
        "app": APP_ID,
        "version": MARKER_VERSION,
        "id": uuid.uuid4().hex,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _write_marker(path, marker)
    except Exception:
        util.force_rmtree(path)
        raise
    try:
        icons.apply_group_icon(path, [])
    except Exception as exc:  # noqa: BLE001 - group still usable without icon
        log.warning("生成初始图标失败 %s: %s", path, exc)
    util.notify_created(path)
    util.notify_dir(desktop)
    return Group(path, marker)


def rename_group(group: Group, new_name: str) -> Group:
    display_name = util.sanitize_name(new_name)
    if display_name == group.name:
        return group
    parent = os.path.dirname(group.path)
    target = os.path.join(parent, display_name)
    if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(group.path):
        raise FileExistsError(f"已存在同名文件夹：{display_name}")
    os.rename(group.path, target)
    marker = read_marker(target) or group.marker
    util.notify_dir(parent)
    return Group(target, marker)


def delete_group(group: Group, restore_apps: bool = True) -> list[str]:
    """Move all user entries to the desktop before removing an empty group."""
    if not group.exists():
        raise FileNotFoundError(f"分组不存在或标记已失效：{group.path}")
    restored: list[str] = []
    if restore_apps:
        # 展示列表会过滤隐藏文件，不能拿它作为删除前的搬移清单。
        with os.scandir(group.path) as entries:
            paths = [entry.path for entry in entries
                     if entry.name.lower() not in (MARKER_DIR, DESKTOP_INI)]
        for path in paths:
            # 搬移失败直接交给调用方报错，保留分组及尚未搬走的文件。
            restored.append(move_to_desktop(path))
        ini = os.path.join(group.path, DESKTOP_INI)
        if os.path.exists(ini):
            util.clear_attributes(ini)
            os.remove(ini)
        util.force_rmtree(os.path.join(group.path, MARKER_DIR))
        util.clear_attributes(group.path)
        # 若搬移期间又有文件进入，rmdir 会失败，不会误删新文件。
        os.rmdir(group.path)
    else:
        util.force_rmtree(group.path)
    parent = os.path.dirname(group.path)
    util.notify_deleted(group.path)
    util.notify_dir(parent)
    return restored


def move_to_desktop(app_path: str) -> str:
    """Move one shortcut/file out of a group back onto the desktop."""
    desktop = util.user_desktop()
    target = util.unique_path(desktop, os.path.basename(app_path))
    shutil.move(app_path, target)
    util.notify_dir(desktop)
    util.notify_dir(os.path.dirname(app_path))
    return target


def remove_from_group(group: Group, app_path: str) -> str:
    return move_to_desktop(app_path)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def sync_icons(only_changed: bool = True) -> list[str]:
    """Synchronously refresh icons that are missing/outdated. Returns refreshed paths."""
    refreshed: list[str] = []
    for group in discover_groups():
        try:
            if only_changed and not icons.needs_icon_refresh(group.path, group.apps):
                continue
        except Exception:  # noqa: BLE001
            pass
        if group.refresh_icon():
            refreshed.append(group.path)
    return refreshed
