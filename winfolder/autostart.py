# -*- coding: utf-8 -*-
"""Enable/disable starting WinFolder with Windows (HKCU Run key)."""
from __future__ import annotations

import logging
import os
import winreg

from . import util

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WinFolder"


def _command() -> str:
    root = util.project_root()
    script = os.path.join(root, "run.pyw")
    return f'"{util.pythonw_path()}" "{script}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("读取开机自启失败: %s", exc)
        return False


def enable() -> bool:
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
        return True
    except OSError as exc:
        log.warning("设置开机自启失败: %s", exc)
        return False


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("取消开机自启失败: %s", exc)
        return False


def sync(enabled: bool) -> None:
    if enabled:
        enable()
    else:
        disable()
