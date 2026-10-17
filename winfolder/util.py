# -*- coding: utf-8 -*-
"""Small Win32 helpers shared by WinFolder modules.

Everything here is plain ctypes so the app has no pywin32 dependency.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import uuid
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_NORMAL = 0x80

SHCNE_RENAMEITEM = 0x00000001
SHCNE_CREATE = 0x00000002
SHCNE_DELETE = 0x00000004
SHCNE_MKDIR = 0x00000008
SHCNE_RMDIR = 0x00000010
SHCNE_UPDATEDIR = 0x00001000
SHCNE_UPDATEITEM = 0x00002000
SHCNE_UPDATEIMAGE = 0x00008000
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_PATHW = 0x0005
SHCNF_FLUSH = 0x1000

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

# ---------------------------------------------------------------------------
# Known folders
# ---------------------------------------------------------------------------

_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_PUBLIC_DESKTOP = "{C4AA340D-F20F-4863-AFEF-F87EF2E6BA25}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    u = uuid.UUID(text)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    for i in range(8):
        g.Data4[i] = u.bytes[8 + i]
    return g


shell32.SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_wchar_p),
]
shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None


def _known_folder(folder_id: str) -> str:
    path_ptr = ctypes.c_wchar_p()
    try:
        hr = shell32.SHGetKnownFolderPath(
            ctypes.byref(_guid(folder_id)), 0, None, ctypes.byref(path_ptr))
    except Exception as exc:  # noqa: BLE001
        log.warning("SHGetKnownFolderPath 失败: %s", exc)
        hr = -1
    if hr != 0 or not path_ptr.value:
        return ""
    value = path_ptr.value
    # ownership is transferred to the caller with CoTaskMemFree
    try:
        ole32.CoTaskMemFree(ctypes.cast(path_ptr, ctypes.c_void_p))
    except Exception:  # noqa: BLE001
        pass
    return value


def user_desktop() -> str:
    path = _known_folder(_FOLDERID_DESKTOP)
    if path and os.path.isdir(path):
        return path
    return os.path.join(os.path.expanduser("~"), "Desktop")


def public_desktop() -> str:
    path = _known_folder(_FOLDERID_PUBLIC_DESKTOP)
    if path and os.path.isdir(path):
        return path
    return os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")


def desktop_dirs() -> list[str]:
    result: list[str] = []
    for path in (user_desktop(), public_desktop()):
        if path and os.path.isdir(path) and path not in result:
            result.append(path)
    return result


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
kernel32.SetFileAttributesW.restype = wintypes.BOOL
kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
kernel32.GetFileAttributesW.restype = wintypes.DWORD


def get_attributes(path: str) -> int:
    return int(kernel32.GetFileAttributesW(path))


def set_attributes(path: str, attributes: int) -> bool:
    return bool(kernel32.SetFileAttributesW(path, attributes))


def clear_attributes(path: str) -> None:
    try:
        set_attributes(path, FILE_ATTRIBUTE_NORMAL)
    except Exception:  # noqa: BLE001
        pass


def hide_system(path: str) -> None:
    """Mark a file (desktop.ini) or folder (.wf) as hidden + system."""
    try:
        set_attributes(path, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        log.warning("隐藏文件属性失败 %s: %s", path, exc)


def set_readonly(path: str) -> None:
    try:
        set_attributes(path, FILE_ATTRIBUTE_READONLY)
    except Exception as exc:  # noqa: BLE001
        log.warning("设置只读属性失败 %s: %s", path, exc)


def force_rmtree(path: str) -> None:
    """Delete a tree even when files/folders carry READONLY / HIDDEN / SYSTEM."""
    if not os.path.exists(path):
        return
    clear_attributes(path)
    for root, dirs, files in os.walk(path):
        clear_attributes(root)
        for name in dirs + files:
            clear_attributes(os.path.join(root, name))
    shutil.rmtree(path)


def unique_path(parent: str, name: str) -> str:
    """Return a path in *parent* for *name*, adding ` (2)`, ` (3)` ... if needed."""
    name = name.strip()
    candidate = os.path.join(parent, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    for index in range(2, 1000):
        candidate = os.path.join(parent, f"{stem} ({index}){ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(parent, f"{stem} ({uuid.uuid4().hex[:6]}){ext}")


_INVALID_NAME_CHARS = set('<>:"/\\|?*')


def sanitize_name(name: str) -> str:
    name = (name or "").strip().strip(".")
    cleaned = "".join(ch for ch in name if ch not in _INVALID_NAME_CHARS and ord(ch) >= 32)
    cleaned = cleaned.strip().rstrip(".")
    return cleaned[:80] or "新建分组"


# ---------------------------------------------------------------------------
# Shell notifications
# ---------------------------------------------------------------------------

shell32.SHChangeNotify.argtypes = [
    ctypes.c_long, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
]
shell32.SHChangeNotify.restype = None
shell32.SHGetSetFolderCustomSettings.restype = ctypes.HRESULT


def sh_change_notify(event: int, path1: str | None = None,
                     path2: str | None = None, flags: int = SHCNF_PATHW | SHCNF_FLUSH) -> None:
    p1 = ctypes.c_wchar_p(path1) if path1 else None
    p2 = ctypes.c_wchar_p(path2) if path2 else None
    try:
        shell32.SHChangeNotify(event, flags, p1, p2)
    except Exception as exc:  # noqa: BLE001
        log.warning("SHChangeNotify 失败 %s: %s", path1, exc)


def notify_item(path: str) -> None:
    sh_change_notify(SHCNE_UPDATEITEM, path)


def notify_dir(path: str) -> None:
    sh_change_notify(SHCNE_UPDATEDIR, path)


def notify_created(path: str) -> None:
    sh_change_notify(SHCNE_CREATE | SHCNE_MKDIR, path)
    notify_dir(os.path.dirname(path))


def notify_deleted(path: str) -> None:
    sh_change_notify(SHCNE_DELETE | SHCNE_RMDIR, path)
    notify_dir(os.path.dirname(path))


# ---------------------------------------------------------------------------
# Shell operations
# ---------------------------------------------------------------------------

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int


def recycle_path(path: str) -> bool:
    """Move a file/folder to the Recycle Bin (undoable)."""
    try:
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        buffer = ctypes.create_unicode_buffer(path + "\0")
        op.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        result = shell32.SHFileOperationW(ctypes.byref(op))
        return result == 0 and not op.fAnyOperationsAborted
    except Exception as exc:  # noqa: BLE001
        log.warning("移入回收站失败 %s: %s", path, exc)
        return False


def reveal_in_explorer(path: str) -> None:
    path = os.path.abspath(path)
    if os.path.isdir(path):
        subprocess.Popen(["explorer", path])
        return
    try:
        subprocess.Popen(["explorer", "/select,", path])
    except Exception:  # noqa: BLE001
        subprocess.Popen(["explorer", os.path.dirname(path)])


def open_in_explorer(path: str) -> None:
    subprocess.Popen(["explorer", os.path.abspath(path)])


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pythonw_path() -> str:
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    if exe.lower().endswith("pythonw.exe"):
        return exe
    return exe


def is_hidden_entry(entry: os.DirEntry) -> bool:
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return False
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def set_dark_titlebar(hwnd: int) -> None:
    """Ask DWM for a dark title bar (Windows 10 20H1+ / Windows 11)."""
    try:
        value = ctypes.c_int(1)
        dwmapi = ctypes.WinDLL("dwmapi")
        size = ctypes.sizeof(value)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwmapi.DwmSetWindowAttribute(hwnd, attribute,
                                            ctypes.byref(value), size) == 0:
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("设置深色标题栏失败: %s", exc)


#: 会被 Electron 宿主/Node 工具链注入、并让“启动 Electron 应用”变成启动 node 进程的环境变量。
_CHILD_ENV_NOISE = (
    "ELECTRON_RUN_AS_NODE",
    "ELECTRON_NO_ATTACH_CONSOLE",
    "ELECTRON_ENABLE_LOGGING",
    "ELECTRON_ENABLE_STACK_DUMPING",
    "NODE_OPTIONS",
)


def sanitize_child_environment() -> list[str]:
    """清掉会让子进程跑偏的 Electron/Node 环境变量。

    场景：如果 WinFolder 是从 VS Code / 某些 Electron 宿主终端里被拉起来的，
    环境里可能带着 ``ELECTRON_RUN_AS_NODE=1``。之后从面板启动 Kimi、VS Code 等
    Electron 应用时，它们会把自己当成 node 解释器、几十毫秒内静默退出。
    这里从进程环境块里移除这些变量，保证启动的是正常 GUI 进程。
    """
    removed: list[str] = []
    for name in _CHILD_ENV_NOISE:
        try:
            if name in os.environ:
                os.environ.pop(name, None)
                removed.append(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctypes.windll.kernel32.SetEnvironmentVariableW(name, None)
        except Exception:  # noqa: BLE001
            pass
    if removed:
        log.info("已清理子进程环境变量: %s", ", ".join(removed))
    return removed


def natural_key(name: str) -> tuple:
    """Case-insensitive natural sort key: 'app 2' < 'app 10'."""
    import re
    text = (name or "").casefold()
    return tuple(int(part) if part.isdigit() else part
                 for part in re.split(r"(\d+)", text))
