# -*- coding: utf-8 -*-
"""WinFolder 图标引擎。

职责：
1. 从 Windows Shell 提取应用图标（Jumbo 256px HICON -> PIL RGBA），带磁盘缓存；
2. 把最多 4 个应用图标合成为 Android 风格的白色圆角分组卡片；
3. 生成多尺寸 .ico，通过 desktop.ini + 文件夹只读属性把卡片应用为真实文件夹图标，
   并调用 SHChangeNotify / SHGetSetFolderCustomSettings 让 Explorer 桌面尽快刷新。

公开 API（其它模块按此调用，签名不可变）：
    extract_icon(path) -> PIL.Image.Image | None
    compose_group_card(images, size=256) -> PIL.Image.Image
    render_empty_card(size=256) -> PIL.Image.Image
    icon_signature(app_paths) -> str
    apply_group_icon(group_dir, app_paths) -> bool
    needs_icon_refresh(group_dir, app_paths) -> bool
    ensure_icon_dir(group_dir) -> str
    read_signature(group_dir) -> str | None

所有公开函数都吞掉异常并 logging，不允许图标问题导致进程崩溃。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from typing import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

logger = logging.getLogger(__name__)

__all__ = [
    "extract_icon",
    "compose_group_card",
    "render_empty_card",
    "icon_signature",
    "apply_group_icon",
    "needs_icon_refresh",
    "ensure_icon_dir",
    "read_signature",
    "CARD_VERSION",
    "ICO_SIZES",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 卡片视觉版本号。改动卡片样式/布局时递增，使所有签名失效并触发重新生成。
CARD_VERSION = "wf-card-v2"

#: 图标缓存格式版本号。提取逻辑变化时递增：旧版本缓存会在下次启动时被清空，
#: 避免历史上被写入缓存的坏图标（如 shell 瞬时返回的 generic 白页图标）继续命中。
_CACHE_VERSION = 5

#: 快捷方式图标"通用文件图标"在系统图标列表里的索引（shell 偶发瞬时失败时返回 0）
_GENERIC_ICON_INDEX = 0

#: 快捷方式提取失败时的重试次数与间隔（秒）
_SHORTCUT_EXTRACT_ATTEMPTS = 3
_SHORTCUT_RETRY_DELAYS = (0.15, 0.20)

#: 需要走"快捷方式目标回退"的扩展名
_SHORTCUT_EXTS = (".lnk", ".url")

#: 卡片基准尺寸（其余尺寸都按 size/256 等比缩放）
CARD_SIZE = 256

#: .ico 里包含的多尺寸（Windows 文件夹图标会按显示尺寸选用）
ICO_SIZES = (
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (96, 96),
    (128, 128),
    (256, 256),
)

_CARD_RADIUS = 56            # 256 基准圆角半径
_CARD_MARGIN = 4             # 卡片外留白（256 基准）
_CARD_SUPERSAMPLE = 4        # 圆角/渐变超采样倍数（抗锯齿）

# 半透明磨砂/液态玻璃卡片参数（256 基准）
_GLASS_TOP_ALPHA = 205               # 卡片顶部白色渐变 alpha
_GLASS_BOTTOM_ALPHA = 108            # 卡片底部白色渐变 alpha
_GLASS_TOP_COLOR = (250, 252, 255)   # 顶部冷白
_GLASS_BOTTOM_COLOR = (228, 238, 255)  # 底部略偏蓝
_GLASS_TOP_HIGHLIGHT_ALPHA = 105     # 顶部内侧柔光强度
_GLASS_TOP_HIGHLIGHT_HEIGHT = 90     # 顶部柔光衰减高度（256 基准）
_GLASS_BOTTOM_EDGE_COLOR = (8, 20, 45)  # 底部内侧暗边颜色
_GLASS_BOTTOM_EDGE_ALPHA = 55        # 底部内侧暗边强度
_GLASS_BOTTOM_EDGE_START = 165       # 暗边从 165/255 高度开始
_GLASS_RIM_ALPHA = 150               # 细白描边 alpha
_GLASS_TOP_RIM_ALPHA = 235           # 顶部高光描边 alpha

# 图标视觉归一化：按强 alpha bbox 裁掉多余透明边（含 shell 低 alpha 伪影），内容放大到画布 92%
_ICON_ALPHA_SOLID = 160              # "实体内容" alpha 阈值
_ICON_CROP_BBOX_RATIO = 0.85         # bbox 面积 < 画布 85% 时才裁剪
_ICON_CONTENT_SCALE = 0.92           # 归一化后内容占画布的 92%
_ICON_CROP_MARGIN = 6                # 裁剪时向外扩的边距（256 基准，保留抗锯齿/柔光）
_ICON_LAYOUTS = {1: 150, 2: 104, 3: 96, 4: 84}  # 各数量下的单元格边长（256 基准）
_ICON_LAYOUT_GAP = 10               # >=3 个图标时的间距（256 基准）
_ICON_LAYOUT_GAP_2 = 12             # 2 个图标时间距（256 基准）
_ICON_FULL_CELL_SCALE = 0.92        # 全幅图标在单元格内的占比

_PLUS_COLOR = (140, 152, 175, 235)  # 空卡片的淡灰 "+"
_PLUS_ARM = 60               # "+" 每根横/竖的长度
_PLUS_THICK = 12             # "+" 的粗细

_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

# Windows 常量
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_NORMAL = 0x80

_SHGFI_ICON = 0x000000100
_SHGFI_LARGEICON = 0x000000000
_SHGFI_SYSICONINDEX = 0x00004000

_SHIL_LARGE = 0x0       # 32px
_SHIL_EXTRALARGE = 0x2  # 48px
_SHIL_JUMBO = 0x4       # 256px

_ILD_TRANSPARENT = 0x1
_DI_NORMAL = 0x3

_IID_IIMAGELIST = "{46EB5926-582E-4017-9FDF-E8998DAA0950}"
_IID_ISHELL_ITEM_IMAGE_FACTORY = "{BCC18B79-BA16-442F-80C4-8A59C30C463B}"

# SIIGBF 标志：SIIGBF_ICONONLY（只要图标不要缩略图）
_SIIGBF_ICONONLY = 0x00000004
# CoInitializeEx：STA
_COINIT_APARTMENTTHREADED = 0x2

_SHCNE_UPDATEITEM = 0x00002000
_SHCNE_UPDATEDIR = 0x00001000
_SHCNF_PATHW = 0x0005
_SHCNF_FLUSH = 0x1000

_FCSM_ICONFILE = 0x10
_FCS_FORCEWRITE = 0x2

_IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# ctypes 结构体 / Windows API 封装
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    try:
        from ctypes import wintypes

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    except Exception:  # pragma: no cover - 非 Windows 或 shell 不可用
        logger.exception("加载 user32/gdi32/shell32/ole32 失败，图标引擎降级为空实现")
        _user32 = _gdi32 = _shell32 = _kernel32 = _ole32 = None
else:  # pragma: no cover - 非 Windows 平台
    _user32 = _gdi32 = _shell32 = _kernel32 = _ole32 = None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", ctypes.c_uint32),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


class _SHFOLDERCUSTOMSETTINGS(ctypes.Structure):
    """shlobj_core.h 中的 SHFOLDERCUSTOMSETTINGS。"""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwMask", ctypes.c_uint32),
        ("pvid", ctypes.c_void_p),
        ("pszWebViewTemplate", ctypes.c_wchar_p),
        ("cchWebViewTemplate", ctypes.c_uint32),
        ("pszWebViewTemplateVersion", ctypes.c_wchar_p),
        ("pszInfoTip", ctypes.c_wchar_p),
        ("cchInfoTip", ctypes.c_uint32),
        ("pclsid", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
        ("pszIconFile", ctypes.c_wchar_p),
        ("cchIconFile", ctypes.c_uint32),
        ("iIconIndex", ctypes.c_int),
        ("pszLogo", ctypes.c_wchar_p),
        ("cchLogo", ctypes.c_uint32),
    ]


_HRESULT = ctypes.c_long


def _setup_win_apis() -> bool:
    """设置 ctypes 函数原型。返回是否全部可用。"""
    if not _IS_WINDOWS or _shell32 is None:
        return False
    try:
        from ctypes import wintypes

        hwnd = wintypes.HWND
        hdc = wintypes.HDC
        hicon = wintypes.HICON
        hbitmap = wintypes.HBITMAP
        hgdiobj = wintypes.HGDIOBJ
        hbrush = wintypes.HBRUSH
        lpcwstr = wintypes.LPCWSTR
        uint = wintypes.UINT
        dword = wintypes.DWORD
        bool_ = wintypes.BOOL

        _user32.GetDC.argtypes = [hwnd]
        _user32.GetDC.restype = hdc
        _user32.ReleaseDC.argtypes = [hwnd, hdc]
        _user32.ReleaseDC.restype = ctypes.c_int
        _user32.DrawIconEx.argtypes = [
            hdc, ctypes.c_int, ctypes.c_int, hicon,
            ctypes.c_int, ctypes.c_int, uint, hbrush, uint,
        ]
        _user32.DrawIconEx.restype = bool_
        _user32.DestroyIcon.argtypes = [hicon]
        _user32.DestroyIcon.restype = bool_

        _gdi32.CreateCompatibleDC.argtypes = [hdc]
        _gdi32.CreateCompatibleDC.restype = hdc
        _gdi32.DeleteDC.argtypes = [hdc]
        _gdi32.DeleteDC.restype = bool_
        _gdi32.CreateDIBSection.argtypes = [
            hdc, ctypes.POINTER(_BITMAPINFO), uint,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, dword,
        ]
        _gdi32.CreateDIBSection.restype = hbitmap
        _gdi32.SelectObject.argtypes = [hdc, hgdiobj]
        _gdi32.SelectObject.restype = hgdiobj
        _gdi32.DeleteObject.argtypes = [hgdiobj]
        _gdi32.DeleteObject.restype = bool_
        _gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        _gdi32.GetObjectW.restype = ctypes.c_int
        _gdi32.GetDIBits.argtypes = [
            hdc, hbitmap, uint, uint, ctypes.c_void_p, ctypes.c_void_p, uint,
        ]
        _gdi32.GetDIBits.restype = ctypes.c_int

        _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, dword]
        _ole32.CoInitializeEx.restype = _HRESULT

        _kernel32.GetFileAttributesW.argtypes = [lpcwstr]
        _kernel32.GetFileAttributesW.restype = dword
        _kernel32.SetFileAttributesW.argtypes = [lpcwstr, dword]
        _kernel32.SetFileAttributesW.restype = bool_

        _shell32.SHGetFileInfoW.argtypes = [
            lpcwstr, dword, ctypes.POINTER(_SHFILEINFOW), uint, uint,
        ]
        _shell32.SHGetFileInfoW.restype = ctypes.c_size_t
        _shell32.SHGetImageList.argtypes = [
            ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        _shell32.SHGetImageList.restype = _HRESULT
        _shell32.SHChangeNotify.argtypes = [
            ctypes.c_long, uint, ctypes.c_void_p, ctypes.c_void_p,
        ]
        _shell32.SHChangeNotify.restype = None
        _shell32.SHCreateItemFromParsingName.argtypes = [
            lpcwstr, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        _shell32.SHCreateItemFromParsingName.restype = _HRESULT
        return True
    except Exception:  # pragma: no cover
        logger.exception("设置 Windows API 原型失败")
        return False


def _get_folder_custom_settings_fn():
    """取 shell32!SHGetSetFolderCustomSettings（无 W 后缀），失败返回 None。"""
    if not _IS_WINDOWS or _shell32 is None:
        return None
    try:
        fn = getattr(_shell32, "SHGetSetFolderCustomSettings", None)
        if fn is None:
            return None
        fn.argtypes = [
            ctypes.POINTER(_SHFOLDERCUSTOMSETTINGS), wintypes.LPCWSTR, wintypes.DWORD,
        ]
        fn.restype = _HRESULT
        return fn
    except Exception:
        logger.debug("SHGetSetFolderCustomSettings 不可用", exc_info=True)
        return None


_APIS_READY = _setup_win_apis()
_FOLDER_SETTINGS_FN = _get_folder_custom_settings_fn() if _APIS_READY else None


def _is_windows_shell_ok() -> bool:
    return bool(_IS_WINDOWS and _APIS_READY and _shell32 is not None)


# ---------------------------------------------------------------------------
# 目录 / 缓存
# ---------------------------------------------------------------------------

_CHUNK = 1024


def _data_dir() -> str:
    """返回 WinFolder 数据目录：优先 winfolder.config.DATA_DIR，否则 %APPDATA%\\WinFolder。"""
    try:
        from . import config as _config  # 延迟导入：config.py 可能稍后才由其它模块创建

        data_dir = getattr(_config, "DATA_DIR", None)
        if data_dir:
            return os.fspath(data_dir)
    except Exception:
        pass
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "WinFolder")


def _migrate_cache_dir(path: str) -> None:
    """缓存格式版本变化时清空旧缓存文件（防止历史坏图标继续被命中）。"""
    marker = os.path.join(path, ".cache-version")
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            current = fh.read().strip()
    except OSError:
        current = ""
    if current == str(_CACHE_VERSION):
        return
    logger.info("图标缓存版本变化 (%r -> %r)，清空旧缓存: %s",
                current or "0", _CACHE_VERSION, path)
    try:
        for name in os.listdir(path):
            if name == ".cache-version":
                continue
            full = os.path.join(path, name)
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                logger.debug("清理旧缓存失败: %s", full, exc_info=True)
    except OSError:
        logger.debug("枚举缓存目录失败: %s", path, exc_info=True)
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(str(_CACHE_VERSION))
    except OSError:
        logger.debug("写缓存版本标记失败: %s", marker, exc_info=True)


def _icon_cache_dir() -> str:
    path = os.path.join(_data_dir(), "icon_cache")
    try:
        os.makedirs(path, exist_ok=True)
        _migrate_cache_dir(path)
    except OSError:
        logger.debug("创建图标缓存目录失败: %s", path, exc_info=True)
    return path


def _cache_key(path: str, size: int) -> str:
    """缓存键：缓存格式版本 + 路径 + 请求尺寸 + mtime_ns 的 md5。"""
    st = os.stat(path)
    raw = "v%d\x00%s\x00%d\x00%d" % (
        _CACHE_VERSION,
        os.path.normcase(os.path.abspath(path)),
        int(size),
        int(st.st_mtime_ns),
    )
    return hashlib.md5(raw.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# HICON -> PIL
# ---------------------------------------------------------------------------


def _hicon_to_rgba(hicon, size: int):
    """把 HICON 绘制到 32bpp DIB 再转成 PIL RGBA（size x size）。"""
    if not hicon or not _user32 or not _gdi32:
        return None
    hdc_screen = None
    hdc = None
    hbmp = None
    old_obj = None
    try:
        hdc_screen = _user32.GetDC(None)
        if not hdc_screen:
            return None
        hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc:
            return None
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = int(size)
        bi.bmiHeader.biHeight = -int(size)  # 负数 = top-down
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not hbmp or not bits:
            return None
        old_obj = _gdi32.SelectObject(hdc, hbmp)
        ctypes.memset(bits, 0, int(size) * int(size) * 4)
        ok = _user32.DrawIconEx(hdc, 0, 0, hicon, int(size), int(size), 0, None, _DI_NORMAL)
        if not ok:
            logger.debug("DrawIconEx 失败: %r", ctypes.get_last_error())
        buf = ctypes.string_at(bits, int(size) * int(size) * 4)
        img = Image.frombuffer("RGBA", (int(size), int(size)), buf, "raw", "BGRA", 0, 1).copy()
        # 某些老图标没有 alpha 通道，DrawIconEx 后 alpha 全 0；用 RGB 反推不透明区域。
        try:
            if img.getchannel("A").getextrema()[1] == 0:
                alpha = img.convert("L").point(lambda v: 255 if v > 0 else 0)
                img.putalpha(alpha)
        except Exception:
            logger.debug("图标 alpha 修补失败", exc_info=True)
        return img
    except Exception:
        logger.exception("HICON 转 PIL 失败")
        return None
    finally:
        try:
            if hdc is not None and old_obj:
                _gdi32.SelectObject(hdc, old_obj)
            if hbmp:
                _gdi32.DeleteObject(hbmp)
            if hdc:
                _gdi32.DeleteDC(hdc)
            if hdc_screen:
                _user32.ReleaseDC(None, hdc_screen)
        except Exception:
            logger.debug("释放 GDI 资源失败", exc_info=True)


def _image_is_blank(img) -> bool:
    """判断图标是否空白（透明）。"""
    if img is None:
        return True
    try:
        alpha = img.getchannel("A")
        if alpha.getextrema()[1] < 8:
            return True
        hist = alpha.histogram()
        return sum(hist[16:]) < 64
    except Exception:
        return False


# ---------------------------------------------------------------------------
# generic 白页图标检测（shell 图标缓存重建窗口期的瞬时退化）
# ---------------------------------------------------------------------------

#: 与 generic 文件图标模板的平均像素差小于该值视为"白页图标"
_GENERIC_ICON_DIFF_THRESHOLD = 8.0
#: 归一化后再比较的阈值（容忍 shell 用不同尺寸重绘 generic 图标的差异）
_GENERIC_ICON_DIFF_THRESHOLD_NORMALIZED = 20.0

_GENERIC_TEMPLATE = None
_GENERIC_TEMPLATE_NORMALIZED = None
_GENERIC_TEMPLATE_LOCK = threading.Lock()


def _generic_page_template(normalized: bool = False):
    """系统图标列表 index 0（generic 文件图标/白页）作为模板；失败返回 None。"""
    global _GENERIC_TEMPLATE, _GENERIC_TEMPLATE_NORMALIZED
    if normalized and _GENERIC_TEMPLATE_NORMALIZED is not None:
        return _GENERIC_TEMPLATE_NORMALIZED
    if not normalized and _GENERIC_TEMPLATE is not None:
        return _GENERIC_TEMPLATE
    with _GENERIC_TEMPLATE_LOCK:
        if _GENERIC_TEMPLATE is None:
            try:
                img = _imagelist_icon(_GENERIC_ICON_INDEX, _SHIL_JUMBO, CARD_SIZE)
                if img is not None and not _image_is_blank(img):
                    _GENERIC_TEMPLATE = img
                    logger.debug("已获取 generic 白页图标模板")
            except Exception:
                logger.debug("获取 generic 图标模板失败", exc_info=True)
        if normalized and _GENERIC_TEMPLATE_NORMALIZED is None and _GENERIC_TEMPLATE is not None:
            try:
                _GENERIC_TEMPLATE_NORMALIZED = _normalize_icon(_GENERIC_TEMPLATE)
            except Exception:
                logger.debug("归一化 generic 模板失败", exc_info=True)
        return _GENERIC_TEMPLATE_NORMALIZED if normalized else _GENERIC_TEMPLATE


def _image_avg_diff(img_a, img_b) -> float:
    """两张 256 RGBA 图（按 contain 对齐）的平均通道差。"""
    try:
        a = _fit_image_to_card(img_a.convert("RGBA"))
        b = _fit_image_to_card(img_b.convert("RGBA"))
        stat = ImageStat.Stat(ImageChops.difference(a, b))
        return float(sum(stat.mean) / 4.0)
    except Exception:
        return 1e9


def _is_generic_page_icon(img) -> bool:
    """判断图标是否退化成 generic 白页（shell 图标缓存重建窗口期可能出现）。

    先与模板直接比较；再归一化后比较，兼容 shell 用 96px 等不同尺寸重绘的白页图标。
    """
    if img is None:
        return False
    try:
        template = _generic_page_template()
        if template is not None and _image_avg_diff(img, template) < _GENERIC_ICON_DIFF_THRESHOLD:
            return True
        normalized = _normalize_icon(img)
        normalized_template = _generic_page_template(normalized=True)
        if normalized_template is not None:
            return (_image_avg_diff(normalized, normalized_template)
                    < _GENERIC_ICON_DIFF_THRESHOLD_NORMALIZED)
    except Exception:
        logger.debug("generic 图标检测失败", exc_info=True)
    return False


# ---------------------------------------------------------------------------
# 图标内容归一化（统一不同来源图标的视觉大小）
# ---------------------------------------------------------------------------


def _solid_alpha_bbox(img):
    """按 alpha >= _ICON_ALPHA_SOLID 求"实体内容" bbox。

    忽略 shell 渲染产生的低 alpha 伪影/外围阴影，只保留真正的图标内容。
    """
    try:
        alpha = img.getchannel("A")
        mask = alpha.point(lambda v: 255 if v >= _ICON_ALPHA_SOLID else 0)
        return mask.getbbox()
    except Exception:
        try:
            return img.getchannel("A").getbbox()
        except Exception:
            return None


def _content_bbox_ratio(img) -> float:
    """实体内容 bbox 面积占画布面积的比例；无内容返回 0。"""
    try:
        bbox = _solid_alpha_bbox(img)
        if bbox is None:
            return 0.0
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        return (width * height) / float(img.width * img.height)
    except Exception:
        return 0.0


def _fit_to_canvas(img, canvas: int = CARD_SIZE) -> Image.Image:
    """等比 contain 到 canvas x canvas 的透明画布中央。"""
    img = img.convert("RGBA")
    if img.size == (canvas, canvas):
        return img
    width, height = img.size
    if width <= 0 or height <= 0:
        return Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    scale = min(canvas / float(width), canvas / float(height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    resized = img.resize((new_w, new_h), _LANCZOS)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.alpha_composite(resized, ((canvas - new_w) // 2, (canvas - new_h) // 2))
    return out


def _normalize_icon_ex(img, canvas: int = CARD_SIZE):
    """图标归一化，返回 (256 画布 RGBA, 是否裁剪放大过)。

    规则：alpha>=160 的实体内容 bbox 面积 < 画布 85% 时才裁剪（外扩 6px 保留
    抗锯齿/柔光），内容长边缩放到画布 92% 居中；全幅图标保持原样（不裁）。
    """
    try:
        work = _fit_to_canvas(img, canvas)
        bbox = _solid_alpha_bbox(work)
        if bbox is None:
            return work, False
        if _content_bbox_ratio(work) >= _ICON_CROP_BBOX_RATIO:
            return work, False
        margin = _ICON_CROP_MARGIN
        left = max(0, bbox[0] - margin)
        top = max(0, bbox[1] - margin)
        right = min(canvas, bbox[2] + margin)
        bottom = min(canvas, bbox[3] + margin)
        content_w = max(1, bbox[2] - bbox[0])
        content_h = max(1, bbox[3] - bbox[1])
        if right <= left or bottom <= top:
            return work, False
        content = work.crop((left, top, right, bottom))
        target = max(8, int(round(canvas * _ICON_CONTENT_SCALE)))
        scale = target / float(max(content_w, content_h))
        new_w = max(1, int(round(content.width * scale)))
        new_h = max(1, int(round(content.height * scale)))
        resized = content.resize((new_w, new_h), _LANCZOS)
        # 小尺寸图标被大幅放大时做轻微锐化，减少发糊感
        if scale >= 2.0:
            try:
                resized = resized.filter(
                    ImageFilter.UnsharpMask(radius=1.2, percent=65, threshold=2)
                )
            except Exception:
                pass
        # 让"实体内容"中心对准画布中心（缩放后的 bbox 中心）
        content_cx = ((bbox[0] + bbox[2]) / 2.0 - left) * scale
        content_cy = ((bbox[1] + bbox[3]) / 2.0 - top) * scale
        dest_x = int(round(canvas / 2.0 - content_cx))
        dest_y = int(round(canvas / 2.0 - content_cy))
        out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        out.paste(resized, (dest_x, dest_y), resized)
        return out, True
    except Exception:
        logger.debug("图标归一化失败", exc_info=True)
        try:
            return _fit_to_canvas(img, canvas), False
        except Exception:
            return Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0)), False


def _normalize_icon(img, canvas: int = CARD_SIZE) -> Image.Image:
    """裁掉多余透明边并把内容放大到画布约 92%，让不同来源图标视觉等大。"""
    return _normalize_icon_ex(img, canvas)[0]


def _iid_image_list() -> _GUID:
    gu = uuid.UUID(_IID_IIMAGELIST)
    return _GUID.from_buffer_copy(gu.bytes_le)


# ---------------------------------------------------------------------------
# IShellItemImageFactory 图标提取（主路径，256px 满幅内容）
# ---------------------------------------------------------------------------

_COM_INIT_TLS = threading.local()


def _ensure_com_initialized() -> None:
    """当前线程确保 COM 已初始化（IShellItemImageFactory 需要）。"""
    if getattr(_COM_INIT_TLS, "done", False):
        return
    _COM_INIT_TLS.done = True
    try:
        if _ole32 is not None:
            _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    except Exception:
        logger.debug("CoInitializeEx 失败", exc_info=True)


def _iid_shell_item_image_factory() -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(_IID_ISHELL_ITEM_IMAGE_FACTORY).bytes_le)


def _hbitmap_to_pil(hbmp):
    """HBITMAP -> PIL RGBA（IShellItemImageFactory::GetImage 的返回位图）。"""
    if not hbmp or not _gdi32 or not _user32:
        return None
    try:
        bm = _BITMAP()
        if not _gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        width, height = int(bm.bmWidth), int(bm.bmHeight)
        if width <= 0 or height <= 0:
            return None
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = width
        bi.bmiHeader.biHeight = -height  # top-down
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        hdc = _user32.GetDC(None)
        if not hdc:
            return None
        try:
            got = _gdi32.GetDIBits(hdc, hbmp, 0, height, buf, ctypes.byref(bi), 0)
        finally:
            _user32.ReleaseDC(None, hdc)
        if not got:
            return None
        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).copy()
        # 少数 DDB 没有 alpha 通道：用 RGB 反推不透明区域
        try:
            if img.getchannel("A").getextrema()[1] < 8:
                img.putalpha(img.convert("L").point(lambda v: 255 if v > 0 else 0))
        except Exception:
            logger.debug("ISF 图标 alpha 修补失败", exc_info=True)
        return img
    except Exception:
        logger.exception("HBITMAP 转 PIL 失败")
        return None


def _extract_icon_via_image_factory(path: str, size: int = CARD_SIZE):
    """IShellItemImageFactory::GetImage —— 主图标提取路径。

    对普通文件/目录/.lnk 均可；对只有 48px 图标资源的 app，返回 256 画布中居中
    的小内容（后续 `_normalize_icon` 会裁边放大，保证卡片里视觉等大）。
    """
    if not _is_windows_shell_ok() or not _ole32:
        return None
    _ensure_com_initialized()
    item = ctypes.c_void_p()
    try:
        iid = _iid_shell_item_image_factory()
        hr = _shell32.SHCreateItemFromParsingName(
            str(path), None, ctypes.byref(iid), ctypes.byref(item)
        )
        if hr != 0 or not item:
            logger.debug("SHCreateItemFromParsingName 失败 hr=0x%08X: %s", hr & 0xFFFFFFFF, path)
            return None
        vtbl = ctypes.cast(item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_image = ctypes.WINFUNCTYPE(
            _HRESULT, ctypes.c_void_p, _SIZE, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[3])
        hbmp = ctypes.c_void_p()
        hr = get_image(item, _SIZE(int(size), int(size)), _SIIGBF_ICONONLY, ctypes.byref(hbmp))
        if hr != 0 or not hbmp:
            logger.debug("IShellItemImageFactory::GetImage 失败 hr=0x%08X: %s",
                         hr & 0xFFFFFFFF, path)
            return None
        try:
            img = _hbitmap_to_pil(hbmp)
        finally:
            _gdi32.DeleteObject(hbmp)
        if img is None or _image_is_blank(img):
            return None
        return _fit_image_to_card(img)
    except Exception:
        logger.debug("IShellItemImageFactory 提取异常: %s", path, exc_info=True)
        return None
    finally:
        _release_com(item)


def _release_com(ptr) -> None:
    """调用 COM 对象 vtable[2] (Release)，避免 COM 对象泄漏。"""
    if not ptr:
        return
    try:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        release(ptr)
    except Exception:
        logger.debug("COM Release 失败", exc_info=True)


def _imagelist_icon(i_icon: int, shil: int, native_size: int):
    """从系统图标列表（IImageList）取 HICON 并转 PIL。"""
    if not _is_windows_shell_ok():
        return None
    pil = ctypes.c_void_p()
    try:
        iid = _iid_image_list()
        hr = _shell32.SHGetImageList(int(shil), ctypes.byref(iid), ctypes.byref(pil))
        if hr != 0 or not pil:
            logger.debug("SHGetImageList(%d) 失败 hr=%s", shil, hr)
            return None
        vtbl = ctypes.cast(pil, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_icon = ctypes.WINFUNCTYPE(
            _HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[10])
        hicon = ctypes.c_void_p()
        hr = get_icon(pil, int(i_icon), _ILD_TRANSPARENT, ctypes.byref(hicon))
        if hr != 0 or not hicon:
            logger.debug("IImageList::GetIcon(%d, shil=%d) 失败 hr=%s", i_icon, shil, hr)
            return None
        try:
            return _hicon_to_rgba(hicon, native_size)
        finally:
            _user32.DestroyIcon(hicon)
    except Exception:
        logger.exception("读取系统图标列表失败")
        return None
    finally:
        _release_com(pil)


def _shell_file_info_icon_index(path: str):
    """SHGetFileInfoW 取系统图标索引。"""
    try:
        info = _SHFILEINFOW()
        res = _shell32.SHGetFileInfoW(
            path, 0, ctypes.byref(info), ctypes.sizeof(info), _SHGFI_SYSICONINDEX
        )
        if not res:
            logger.debug("SHGetFileInfoW(SYSICONINDEX) 失败: %s", path)
            return None
        return int(info.iIcon)
    except Exception:
        logger.exception("SHGetFileInfoW 调用异常: %s", path)
        return None


def _extract_icon_from_shell(path: str):
    """仅走 Windows shell 的图标提取（无缓存）。

    返回 (PIL.Image | None, 系统图标列表索引 | None)。索引用于上层判断
    shell 是否瞬时退化成 generic 图标（索引 0）。
    """
    if not _is_windows_shell_ok():
        return None, None
    i_icon = _shell_file_info_icon_index(path)
    if i_icon is not None:
        img = _imagelist_icon(i_icon, _SHIL_JUMBO, CARD_SIZE)
        if img is not None and not _image_is_blank(img):
            return img, i_icon
        logger.debug("Jumbo 图标为空，尝试 48/32px 图标: %s", path)
        for shil, native in ((_SHIL_EXTRALARGE, 48), (_SHIL_LARGE, 32)):
            img = _imagelist_icon(i_icon, shil, native)
            if img is not None and not _image_is_blank(img):
                return img.resize((CARD_SIZE, CARD_SIZE), _LANCZOS), i_icon
    # 最后兜底：SHGetFileInfoW 直接取 HICON（32px）
    try:
        info = _SHFILEINFOW()
        res = _shell32.SHGetFileInfoW(
            path, 0, ctypes.byref(info), ctypes.sizeof(info),
            _SHGFI_ICON | _SHGFI_LARGEICON,
        )
        if res and info.hIcon:
            try:
                img = _hicon_to_rgba(info.hIcon, 32)
            finally:
                _user32.DestroyIcon(info.hIcon)
            if img is not None and not _image_is_blank(img):
                return img.resize((CARD_SIZE, CARD_SIZE), _LANCZOS), i_icon
    except Exception:
        logger.exception("SHGetFileInfoW(ICON) 兜底失败: %s", path)
    return None, i_icon


# ---------------------------------------------------------------------------
# 快捷方式图标回退（shell 瞬时返回 generic 图标时使用）
# ---------------------------------------------------------------------------


def _is_shortcut(path: str) -> bool:
    try:
        return os.path.splitext(str(path))[1].lower() in _SHORTCUT_EXTS
    except Exception:
        return False


_COMTYPES_AVAILABLE = None


def _ensure_comtypes_available() -> bool:
    """确认选定的 Python 环境已安装 comtypes。"""
    global _COMTYPES_AVAILABLE
    if _COMTYPES_AVAILABLE is not None:
        return _COMTYPES_AVAILABLE
    try:
        import comtypes.client  # noqa: F401

        _COMTYPES_AVAILABLE = True
        return True
    except Exception:
        logger.debug("comtypes 不可用，快捷方式解析回退被禁用", exc_info=True)
        _COMTYPES_AVAILABLE = False
        return False


def _parse_icon_location(value: str):
    """解析 'C:\\x\\app.exe,3' / ',0' / 'C:\\x\\icon.ico' -> (路径, 索引或 None)。"""
    value = (value or "").strip()
    if not value:
        return "", None
    head, sep, tail = value.rpartition(",")
    if sep and tail.strip().lstrip("+-").isdigit():
        return os.path.expandvars(head.strip().strip('"')), int(tail.strip())
    return os.path.expandvars(value.strip().strip('"')), None


def _resolve_shortcut(path: str):
    """用 WScript.Shell 解析快捷方式，返回 target / icon_path / icon_index。"""
    result = {"target": "", "icon_path": "", "icon_index": None}
    if not _ensure_comtypes_available():
        return result
    shell = shortcut = None
    try:
        import comtypes.client

        shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
        shortcut = shell.CreateShortcut(str(path))
        try:
            target = str(shortcut.TargetPath or "").strip()
        except Exception:
            target = ""
        if target:
            target = os.path.expandvars(target)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(os.path.abspath(path)), target)
            result["target"] = os.path.normpath(target)
        try:
            icon_location = str(shortcut.IconLocation or "")
        except Exception:
            icon_location = ""
        icon_path, icon_index = _parse_icon_location(icon_location)
        result["icon_path"] = icon_path or ""
        result["icon_index"] = icon_index
    except Exception:
        logger.debug("WScript.Shell 解析快捷方式失败: %s", path, exc_info=True)
    finally:
        del shortcut
        del shell
    return result


def _read_url_icon(url_path: str) -> str:
    """从 .url（INI 文本）的 IconFile= 读取图标路径。"""
    try:
        for line in _read_text_best_effort(url_path).splitlines():
            line = line.strip().lstrip("\ufeff")
            if line.lower().startswith("iconfile="):
                value = line.split("=", 1)[1].strip().strip('"')
                if value:
                    return os.path.expandvars(value)
    except Exception:
        logger.debug("读取 .url IconFile 失败: %s", url_path, exc_info=True)
    return ""


def _fit_image_to_card(img) -> Image.Image:
    """把任意尺寸 RGBA 图等比缩放到 256x256 透明画布中央。"""
    img = img.convert("RGBA")
    if img.size == (CARD_SIZE, CARD_SIZE):
        return img
    width, height = img.size
    if width <= 0 or height <= 0:
        return Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    scale = min(CARD_SIZE / float(width), CARD_SIZE / float(height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    resized = img.resize((new_w, new_h), _LANCZOS)
    canvas = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((CARD_SIZE - new_w) // 2, (CARD_SIZE - new_h) // 2))
    return canvas


def _load_ico_best(ico_path: str):
    """直接从 .ico 文件读取尽可能大的图标（优先 256px 条目）。"""
    try:
        with Image.open(ico_path) as im:
            chosen = None
            ico = getattr(im, "ico", None)
            if ico is not None:
                try:
                    sizes = sorted(tuple(s) for s in ico.sizes())
                except Exception:
                    sizes = []
                for size in reversed(sizes):
                    if size[0] > CARD_SIZE:
                        continue
                    try:
                        frame = ico.getimage(size)
                    except Exception:
                        continue
                    if frame is not None:
                        chosen = frame.convert("RGBA")
                        break
            if chosen is None:
                im.load()
                chosen = im.convert("RGBA")
        return _fit_image_to_card(chosen)
    except Exception:
        logger.debug("读取 .ico 图标失败: %s", ico_path, exc_info=True)
        return None


def _shortcut_binary_path_candidates(shortcut_path: str):
    """从 .lnk 二进制里扫描可能的目标路径（最后兜底手段）。

    某些 MSI/特殊快捷方式（实测 Adobe Illustrator 2024.lnk）的 TargetPath 和
    IconLocation 都是空的，shell 的 SHGetFileInfo 也退化为 generic 图标，但 .lnk
    文件里仍保存着完整的 UTF-16 目标路径。这里扫描可打印字符串，挑出磁盘上真实
    存在、且扩展名能提供图标的路径。
    """
    try:
        with open(shortcut_path, "rb") as fh:
            data = fh.read(65536)
    except OSError:
        return []
    strings = []
    try:
        for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", data):
            try:
                strings.append(match.group().decode("utf-16le"))
            except UnicodeDecodeError:
                continue
        for match in re.finditer(rb"[\x20-\x7e]{4,}", data):
            strings.append(match.group().decode("latin-1"))
    except Exception:
        logger.debug("扫描 .lnk 字符串失败: %s", shortcut_path, exc_info=True)
        return []
    candidates = []
    for raw in strings:
        candidate = raw.strip().strip('"').strip()
        if not candidate:
            continue
        expanded = os.path.expandvars(candidate)
        if not expanded.lower().endswith((".exe", ".ico", ".dll")):
            continue
        if not (os.path.isabs(expanded) or (len(expanded) > 2 and expanded[1] == ":")):
            continue
        try:
            if not os.path.isfile(expanded):
                continue
        except OSError:
            continue
        if expanded not in candidates:
            candidates.append(expanded)
        if len(candidates) >= 8:
            break
    return candidates


def _shortcut_aumid_candidates(shortcut_path: str):
    """从 .lnk 二进制里解析 UWP 的 AUMID（形如 Family!AppId）。

    实测：DPI-aware 进程中，复制到新文件夹的 UWP 快捷方式（如 ChatGPT.lnk）会被
    shell 解析成 generic 白页图标；此时用 AUMID 走 `shell:AppsFolder\\<AUMID>`
    的 IShellItemImageFactory 仍可拿到完整 256px 图标。
    """
    try:
        with open(shortcut_path, "rb") as fh:
            data = fh.read(65536)
    except OSError:
        return []
    found = []
    try:
        for match in re.finditer(rb"(?:[\x20-\x7e]\x00){6,}", data):
            try:
                value = match.group().decode("utf-16le").strip()
            except UnicodeDecodeError:
                continue
            if "!" in value and re.match(r"^[A-Za-z0-9_.\-]+![A-Za-z0-9_.\-]+$", value):
                if value not in found:
                    found.append(value)
        for match in re.finditer(rb"[\x20-\x7e]{6,}", data):
            value = match.group().decode("latin-1").strip()
            if "!" in value and re.match(r"^[A-Za-z0-9_.\-]+![A-Za-z0-9_.\-]+$", value):
                if value not in found:
                    found.append(value)
    except Exception:
        logger.debug("解析 .lnk AUMID 失败: %s", shortcut_path, exc_info=True)
    return found


def _extract_icon_by_aumid(aumid: str):
    """用 shell:AppsFolder\\<AUMID> 解析 UWP 应用图标（ISF, 256px）。"""
    if not aumid:
        return None
    try:
        img = _extract_icon_via_image_factory("shell:AppsFolder\\" + aumid)
        if img is None or _image_is_blank(img) or _is_generic_page_icon(img):
            return None
        logger.info("已通过 AUMID 回退提取 UWP 图标: %s", aumid)
        return img
    except Exception:
        logger.debug("AUMID 提取图标失败: %s", aumid, exc_info=True)
        return None


def _extract_icon_via_shortcut(path: str):
    """快捷方式图标回退：解析 TargetPath / IconLocation / AUMID 后再提取图标。"""
    try:
        candidates = []
        if str(path).lower().endswith(".url"):
            icon_file = _read_url_icon(path)
            if icon_file:
                candidates.append(icon_file)
        info = _resolve_shortcut(path)
        for candidate in (info.get("icon_path"), info.get("target")):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in _shortcut_binary_path_candidates(path):
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            try:
                if not candidate or not os.path.isfile(candidate):
                    continue
                if candidate.lower().endswith(".ico"):
                    img = _load_ico_best(candidate)
                    if img is not None:
                        logger.info("已通过快捷方式 IconLocation(.ico) 回退: %s -> %s", path, candidate)
                        return img
                    continue
                img = _extract_icon_via_image_factory(candidate)
                if (img is not None and not _image_is_blank(img)
                        and not _is_generic_page_icon(img)):
                    logger.info("已通过快捷方式目标(ISF)回退提取图标: %s -> %s", path, candidate)
                    return img
                img, icon_index = _extract_icon_from_shell(candidate)
                if img is not None and icon_index != _GENERIC_ICON_INDEX:
                    logger.info("已通过快捷方式目标回退提取图标: %s -> %s", path, candidate)
                    return img
            except Exception:
                logger.debug("回退提取失败: %s", candidate, exc_info=True)
        # 最后：UWP AUMID 回退（DPI-aware 进程里复制 UWP 快捷方式的唯一可靠路径）
        for aumid in _shortcut_aumid_candidates(path):
            img = _extract_icon_by_aumid(aumid)
            if img is not None:
                return img
        if not candidates:
            logger.debug("快捷方式无可用回退目标: %s", path)
        return None
    except Exception:
        logger.exception("快捷方式图标回退异常: %s", path)
        return None


def _extract_icon_uncached(path: str):
    """无缓存图标提取。

    顺序：
    1. 主路径：IShellItemImageFactory::GetImage(256)（对 48px 小图标资源更友好）；
       快捷方式在该路径上做 generic 白页检测（shell 图标缓存重建窗口期可能出现），
       命中则重试，再失败走目标回退；内容过小的快捷方式也优先目标回退；
    2. 回落旧链路：SHGetFileInfo(SYSICONINDEX) + IImageList(JUMBO)（含 .lnk 的
       generic 检测与重试）；
    3. 再回落快捷方式目标解析：WScript.Shell 的 TargetPath/IconLocation + .lnk
       二进制路径扫描。
    最后统一 `_normalize_icon`（裁掉多余透明边并放大内容）；失败结果返回 None，
    由上层保证不写缓存。
    """
    shortcut = _is_shortcut(path)

    # 1) 主路径：IShellItemImageFactory
    isf_attempts = _SHORTCUT_EXTRACT_ATTEMPTS if shortcut else 1
    fallback_tried = False
    for attempt in range(isf_attempts):
        if attempt > 0:
            delay = _SHORTCUT_RETRY_DELAYS[min(attempt - 1, len(_SHORTCUT_RETRY_DELAYS) - 1)]
            time.sleep(delay)
        primary = _extract_icon_via_image_factory(path)
        if primary is None or _image_is_blank(primary):
            continue
        if shortcut and _is_generic_page_icon(primary):
            logger.warning(
                "ISF 对快捷方式返回 generic 白页图标，第 %d/%d 次: %s",
                attempt + 1, isf_attempts, path,
            )
            # 第一次命中就立即尝试目标/AUMID 回退（DPI-aware 进程里 UWP 快捷方式
            # 会稳定退化成 generic，此时重试无意义，直接走回退更快）
            if not fallback_tried:
                fallback_tried = True
                alt = _extract_icon_via_shortcut(path)
                if (alt is not None and not _image_is_blank(alt)
                        and not _is_generic_page_icon(alt)):
                    logger.info("ISF generic 白页，改用目标/AUMID 回退: %s", path)
                    return _normalize_icon(alt)
            continue
        if shortcut:
            try:
                ratio = _content_bbox_ratio(primary)
            except Exception:
                ratio = 1.0
            if ratio < 0.25:
                alt = _extract_icon_via_shortcut(path)
                try:
                    alt_ratio = _content_bbox_ratio(alt) if alt is not None else 0.0
                except Exception:
                    alt_ratio = 0.0
                if (alt is not None and not _image_is_blank(alt)
                        and not _is_generic_page_icon(alt) and alt_ratio > ratio):
                    logger.info("主路径图标内容过小(%.3f)，改用目标回退: %s", ratio, path)
                    return _normalize_icon(alt)
        return _normalize_icon(primary)

    # 2) 回落旧 shell 链路（快捷方式 generic 检测 + 重试）
    legacy_attempts = _SHORTCUT_EXTRACT_ATTEMPTS if shortcut else 1
    last_icon_index = None
    for attempt in range(legacy_attempts):
        if attempt > 0:
            delay = _SHORTCUT_RETRY_DELAYS[min(attempt - 1, len(_SHORTCUT_RETRY_DELAYS) - 1)]
            time.sleep(delay)
        img, icon_index = _extract_icon_from_shell(path)
        last_icon_index = icon_index
        if img is not None:
            if shortcut and icon_index == _GENERIC_ICON_INDEX:
                logger.warning(
                    "shell 对快捷方式返回通用文件图标(index=0)，第 %d/%d 次: %s",
                    attempt + 1, legacy_attempts, path,
                )
            elif shortcut and _is_generic_page_icon(img):
                logger.warning(
                    "shell 返回 generic 白页图标，第 %d/%d 次: %s",
                    attempt + 1, legacy_attempts, path,
                )
            else:
                return _normalize_icon(img)

    # 3) 快捷方式目标回退（WScript.Shell / .lnk 二进制路径扫描 / .ico）
    if shortcut:
        if last_icon_index == _GENERIC_ICON_INDEX:
            logger.warning("快捷方式图标索引持续为 0，尝试解析目标回退: %s", path)
        img = _extract_icon_via_shortcut(path)
        if img is not None and not _image_is_blank(img) and not _is_generic_page_icon(img):
            return _normalize_icon(img)
    return None


# ---------------------------------------------------------------------------
# 图标磁盘/内存缓存
# ---------------------------------------------------------------------------

_MEM_CACHE: dict[str, Image.Image] = {}
_MEM_CACHE_LIMIT = 256
_MEM_CACHE_LOCK = threading.RLock()


def _load_cached_icon(cache_file: str):
    if not os.path.isfile(cache_file):
        return None
    try:
        with Image.open(cache_file) as im:
            im.load()
            img = im.convert("RGBA")
        if img.size != (CARD_SIZE, CARD_SIZE):
            raise ValueError("缓存尺寸异常: %r" % (img.size,))
        return img
    except Exception:
        logger.debug("读取图标缓存失败，删除损坏缓存: %s", cache_file, exc_info=True)
        try:
            os.remove(cache_file)
        except OSError:
            pass
        return None


def _save_cached_icon(cache_file: str, img: Image.Image) -> None:
    tmp = "%s.tmp.%d.%d" % (cache_file, os.getpid(), threading.get_ident())
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, cache_file)
    except Exception:
        logger.debug("写入图标缓存失败: %s", cache_file, exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 公开 API：图标提取
# ---------------------------------------------------------------------------


def extract_icon(path: str):
    """提取一个文件/快捷方式的 256x256 RGBA 图标；失败返回 None。

    磁盘缓存位于 %APPDATA%\\WinFolder\\icon_cache（若 winfolder.config.DATA_DIR
    存在则用 DATA_DIR\\icon_cache），键为 缓存版本+路径+尺寸+mtime_ns 的 md5。
    失败结果（None）不会写入缓存；快捷方式遇到 shell 瞬时返回的 generic 图标时
    会重试并回退到解析 TargetPath / IconLocation。
    """
    try:
        if not path:
            return None
        path = os.path.abspath(os.fspath(path))
        if not os.path.exists(path):
            logger.debug("extract_icon: 路径不存在: %s", path)
            return None
        if not _is_windows_shell_ok():
            return None
        try:
            key = _cache_key(path, CARD_SIZE)
        except OSError:
            logger.debug("extract_icon: stat 失败: %s", path, exc_info=True)
            return None

        with _MEM_CACHE_LOCK:
            mem = _MEM_CACHE.get(key)
            if mem is not None:
                return mem.copy()

        cache_file = os.path.join(_icon_cache_dir(), key + ".png")
        img = _load_cached_icon(cache_file)
        if img is None:
            img = _extract_icon_uncached(path)
            if img is None:
                logger.warning("extract_icon: 无法提取图标（不写缓存）: %s", path)
                return None
            _save_cached_icon(cache_file, img)

        with _MEM_CACHE_LOCK:
            if len(_MEM_CACHE) >= _MEM_CACHE_LIMIT:
                _MEM_CACHE.clear()
            _MEM_CACHE[key] = img.copy()
        return img
    except Exception:
        logger.exception("extract_icon 异常: %r", path)
        return None


# ---------------------------------------------------------------------------
# 公开 API：卡片合成
# ---------------------------------------------------------------------------


def _rounded_mask(size: int):
    """生成圆角矩形蒙版（超采样后），返回 (mask, scale, box, radius)。"""
    work = max(8, int(size) * int(_CARD_SUPERSAMPLE))
    scale = work / float(CARD_SIZE)
    mask = Image.new("L", (work, work), 0)
    draw = ImageDraw.Draw(mask)
    margin = max(1, int(round(_CARD_MARGIN * scale)))
    radius = max(2, int(round(_CARD_RADIUS * scale)))
    box = [margin, margin, work - margin - 1, work - margin - 1]
    draw.rounded_rectangle(box, radius=radius, fill=255)
    return mask, scale, box, radius


def _draw_card(size: int) -> Image.Image:
    """半透明磨砂/液态玻璃卡片：竖渐变半透明白 + 顶部高光描边 + 内侧柔光/暗边 + 细白描边。"""
    size = _coerce_size(size)
    try:
        mask, scale, box, radius = _rounded_mask(size)
        work = mask.width
        ramp = Image.linear_gradient("L").resize((work, work), Image.BILINEAR)
        # 竖向颜色 + alpha 渐变：顶部更实、底部更透
        color = ImageOps.colorize(
            ramp, black=_GLASS_TOP_COLOR, white=_GLASS_BOTTOM_COLOR
        ).convert("RGBA")
        top_alpha = float(_GLASS_TOP_ALPHA)
        bottom_alpha = float(_GLASS_BOTTOM_ALPHA)
        alpha = ramp.point(
            lambda v: max(0, min(255, int(round(
                top_alpha + (bottom_alpha - top_alpha) * v / 255.0))))
        )
        color.putalpha(ImageChops.multiply(alpha, mask))
        card = color

        # 顶部内侧柔光
        highlight_height = max(1.0, _GLASS_TOP_HIGHLIGHT_HEIGHT * scale)
        sheen_alpha = ramp.point(
            lambda v: max(0, min(255, int(round(
                _GLASS_TOP_HIGHLIGHT_ALPHA * max(0.0, 1.0 - v / highlight_height)))))
        )
        sheen = Image.new("RGBA", (work, work), (255, 255, 255, 255))
        sheen.putalpha(ImageChops.multiply(sheen_alpha, mask))
        card = Image.alpha_composite(card, sheen)

        # 底部内侧暗边（玻璃厚度感）
        edge_start = float(_GLASS_BOTTOM_EDGE_START)
        edge_alpha = ramp.point(
            lambda v: max(0, min(255, int(round(
                _GLASS_BOTTOM_EDGE_ALPHA * max(0.0, (v - edge_start) / (255.0 - edge_start))))))
        )
        edge = Image.new("RGBA", (work, work), _GLASS_BOTTOM_EDGE_COLOR + (255,))
        edge.putalpha(ImageChops.multiply(edge_alpha, mask))
        card = Image.alpha_composite(card, edge)

        # 细白描边 + 顶部高光描边（向下淡出）
        rim_width = max(1, int(round(1.4 * scale)))
        rim = Image.new("RGBA", (work, work), (0, 0, 0, 0))
        ImageDraw.Draw(rim).rounded_rectangle(
            box, radius=radius, outline=(255, 255, 255, _GLASS_RIM_ALPHA), width=rim_width
        )
        card = Image.alpha_composite(card, rim)
        top_rim = Image.new("RGBA", (work, work), (0, 0, 0, 0))
        ImageDraw.Draw(top_rim).rounded_rectangle(
            box, radius=radius, outline=(255, 255, 255, _GLASS_TOP_RIM_ALPHA), width=rim_width
        )
        top_mask = ramp.point(
            lambda v: max(0, min(255, int(round(
                255.0 * max(0.0, 1.0 - v / (140.0 * scale))))))
        )
        top_rim.putalpha(ImageChops.multiply(top_rim.getchannel("A"), top_mask))
        card = Image.alpha_composite(card, top_rim)

        return card.resize((size, size), _LANCZOS)
    except Exception:
        logger.exception("绘制玻璃卡片失败，退回简单半透明卡片")
        card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        margin = max(1, int(round(_CARD_MARGIN * size / float(CARD_SIZE))))
        radius = max(2, int(round(_CARD_RADIUS * size / float(CARD_SIZE))))
        draw.rounded_rectangle(
            [margin, margin, size - margin - 1, size - margin - 1],
            radius=radius, fill=(255, 255, 255, 185), outline=(255, 255, 255, 205),
            width=max(1, size // 128),
        )
        return card


def _coerce_size(size) -> int:
    try:
        value = int(size)
    except (TypeError, ValueError):
        value = CARD_SIZE
    return max(16, min(value, 1024))


def _cell_boxes(count: int, size: int):
    """返回 [(x, y, cell)]（基准 256 布局按 size 缩放）。

    1 个：居中 150；2 个：左右各 104、间距 12；3 个：上方两个 96 + 下方居中 96；
    >=4 个：2x2 每格 84、间距 10，整体居中。均不重叠且留足边距。
    """
    count = max(1, min(int(count), 4))
    if count == 1:
        cell = _ICON_LAYOUTS[1]
        x0 = (CARD_SIZE - cell) / 2.0
        boxes_256 = [(x0, x0, cell)]
    elif count == 2:
        cell, gap = _ICON_LAYOUTS[2], _ICON_LAYOUT_GAP_2
        total = cell * 2 + gap
        x0 = (CARD_SIZE - total) / 2.0
        y0 = (CARD_SIZE - cell) / 2.0
        boxes_256 = [(x0, y0, cell), (x0 + cell + gap, y0, cell)]
    elif count == 3:
        cell, gap = _ICON_LAYOUTS[3], _ICON_LAYOUT_GAP
        total = cell * 2 + gap
        x0 = (CARD_SIZE - total) / 2.0
        ytop = (CARD_SIZE - total) / 2.0
        boxes_256 = [
            (x0, ytop, cell),
            (x0 + cell + gap, ytop, cell),
            ((CARD_SIZE - cell) / 2.0, ytop + cell + gap, cell),
        ]
    else:
        cell, gap = _ICON_LAYOUTS[4], _ICON_LAYOUT_GAP
        total = cell * 2 + gap
        x0 = (CARD_SIZE - total) / 2.0
        y0 = x0
        boxes_256 = [
            (x0, y0, cell),
            (x0 + cell + gap, y0, cell),
            (x0, y0 + cell + gap, cell),
            (x0 + cell + gap, y0 + cell + gap, cell),
        ]
    scale = size / float(CARD_SIZE)
    return [
        (int(round(x * scale)), int(round(y * scale)), max(4, int(round(cell * scale))))
        for x, y, cell in boxes_256
    ]


def _paste_icon(card: Image.Image, img: Image.Image, x: int, y: int, cell: int) -> None:
    """把单个图标画进 cell 区域（统一视觉大小约 cell 的 92%）。"""
    try:
        normalized, cropped = _normalize_icon_ex(img)
        if cropped:
            # 已归一化：内容占画布 92%，直接铺满 cell
            tile = normalized.resize((cell, cell), _LANCZOS)
        else:
            # 全幅图标：缩到 cell 的 92% 居中
            inner = max(1, int(round(cell * _ICON_FULL_CELL_SCALE)))
            tile = normalized.resize((inner, inner), _LANCZOS)
        layer = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        layer.alpha_composite(tile, ((cell - tile.width) // 2, (cell - tile.height) // 2))
        card.alpha_composite(layer, (int(x), int(y)))
    except Exception:
        logger.debug("绘制单个图标失败", exc_info=True)


def compose_group_card(images: Sequence[Image.Image], size: int = CARD_SIZE) -> Image.Image:
    """把最多 4 个应用图标合成为毛玻璃风格分组卡片（均匀布局、不重叠）。"""
    size = _coerce_size(size)
    try:
        items = []
        for im in list(images or [])[:4]:
            if im is None:
                continue
            try:
                items.append(im.convert("RGBA") if im.mode != "RGBA" else im)
            except Exception:
                logger.debug("compose_group_card: 跳过非法图像 %r", im, exc_info=True)
        if not items:
            return render_empty_card(size)
        card = _draw_card(size)
        for (x, y, cell), img in zip(_cell_boxes(len(items), size), items):
            _paste_icon(card, img, x, y, cell)
        return card
    except Exception:
        logger.exception("compose_group_card 失败")
        try:
            return render_empty_card(size)
        except Exception:
            return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def render_empty_card(size: int = CARD_SIZE) -> Image.Image:
    """空分组卡片：毛玻璃卡片中央一个淡灰 "+"。"""
    size = _coerce_size(size)
    try:
        card = _draw_card(size)
        draw = ImageDraw.Draw(card)
        scale = size / float(CARD_SIZE)
        arm = max(4, int(round(_PLUS_ARM * scale)))
        thick = max(2, int(round(_PLUS_THICK * scale)))
        cx = size // 2
        cy = size // 2
        hx = cx - arm // 2
        hy = cy - thick // 2
        draw.rounded_rectangle(
            [hx, hy, hx + arm - 1, hy + thick - 1],
            radius=max(1, thick // 2), fill=_PLUS_COLOR,
        )
        vx = cx - thick // 2
        vy = cy - arm // 2
        draw.rounded_rectangle(
            [vx, vy, vx + thick - 1, vy + arm - 1],
            radius=max(1, thick // 2), fill=_PLUS_COLOR,
        )
        return card
    except Exception:
        logger.exception("render_empty_card 失败")
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))


# ---------------------------------------------------------------------------
# 公开 API：内容签名
# ---------------------------------------------------------------------------


def icon_signature(app_paths: Sequence[str]) -> str:
    """分组内容签名：每个文件的 basename+size+mtime_ns + 卡片版本号。

    用于判断是否需要重新生成图标（跳过无变化刷新）。
    """
    try:
        digest = hashlib.md5()
        digest.update(CARD_VERSION.encode("utf-8"))
        digest.update(b"\x00")
        for path in app_paths or []:
            try:
                if not path:
                    continue
                full = os.path.abspath(os.fspath(path))
                name = os.path.basename(full).lower()
                try:
                    st = os.stat(full)
                    part = "%s|%d|%d" % (name, int(st.st_size), int(st.st_mtime_ns))
                except OSError:
                    part = "%s|missing" % name
                digest.update(part.encode("utf-8", "surrogatepass"))
                digest.update(b"\x00")
            except Exception:
                logger.debug("icon_signature: 跳过非法路径 %r", path, exc_info=True)
        return digest.hexdigest()
    except Exception:
        logger.exception("icon_signature 失败")
        return ""


# ---------------------------------------------------------------------------
# 文件属性 / desktop.ini 辅助
# ---------------------------------------------------------------------------


def _get_attrs(path: str) -> int:
    try:
        return int(_kernel32.GetFileAttributesW(path)) & 0xFFFFFFFF
    except Exception:
        return _INVALID_FILE_ATTRIBUTES


def _set_attrs(path: str, attrs: int) -> bool:
    try:
        ok = bool(_kernel32.SetFileAttributesW(path, int(attrs) & 0xFFFFFFFF))
        if not ok:
            logger.debug("SetFileAttributesW 失败: %s -> 0x%X", path, attrs)
        return ok
    except Exception:
        logger.debug("SetFileAttributesW 异常: %s", path, exc_info=True)
        return False


def _clear_readonly(path: str) -> None:
    """清掉 READONLY 位（保留其它属性），失败忽略。"""
    try:
        attrs = _get_attrs(path)
        if attrs == _INVALID_FILE_ATTRIBUTES:
            return
        if attrs & _FILE_ATTRIBUTE_READONLY:
            _set_attrs(path, attrs & ~_FILE_ATTRIBUTE_READONLY)
    except Exception:
        logger.debug("清除只读属性失败: %s", path, exc_info=True)


def _set_readonly(path: str) -> None:
    try:
        attrs = _get_attrs(path)
        if attrs == _INVALID_FILE_ATTRIBUTES:
            attrs = 0
        if not (attrs & _FILE_ATTRIBUTE_READONLY):
            _set_attrs(path, attrs | _FILE_ATTRIBUTE_READONLY)
    except Exception:
        logger.debug("设置只读属性失败: %s", path, exc_info=True)


def _read_text_best_effort(path: str) -> str:
    """按 desktop.ini 常见编码读取文本；失败返回空串。"""
    for encoding in ("utf-16", "utf-8-sig", "mbcs", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as fh:
                return fh.read()
        except (UnicodeError, LookupError):
            continue
        except OSError:
            return ""
    return ""


def _read_icon_resource(group_dir: str):
    """读取 desktop.ini 里的 IconResource 图标文件绝对路径；没有返回 None。"""
    try:
        ini = os.path.join(group_dir, "desktop.ini")
        if not os.path.isfile(ini):
            return None
        for line in _read_text_best_effort(ini).splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line.lower().startswith("iconresource="):
                continue
            value = line.split("=", 1)[1].strip()
            head, sep, tail = value.rpartition(",")
            if sep and tail.strip().lstrip("+-").isdigit():
                value = head.strip()
            value = value.strip().strip('"')
            if value:
                return os.path.abspath(os.path.expandvars(value))
        return None
    except Exception:
        logger.debug("解析 desktop.ini 失败: %s", group_dir, exc_info=True)
        return None


def _compose_desktop_ini_text(existing: str, ico_path: str) -> str:
    """保留原有其它键值，只写入/替换 [.ShellClassInfo] 下的 IconResource。"""
    icon_line = "IconResource=%s,0" % ico_path
    text = (existing or "").replace("\ufeff", "")
    lines = text.splitlines()
    out: list[str] = []
    in_shell_class = False
    section_found = False
    icon_written = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_shell_class and not icon_written:
                out.append(icon_line)
                icon_written = True
            in_shell_class = line.lower() == "[.shellclassinfo]"
            section_found = section_found or in_shell_class
            out.append(line)
            continue
        if in_shell_class:
            if line.lower().startswith("iconresource="):
                if not icon_written:
                    out.append(icon_line)
                    icon_written = True
                continue
            out.append(line)
        else:
            out.append(line)
    if not section_found:
        out = ["[.ShellClassInfo]", icon_line] + out
    elif not icon_written:
        for idx, line in enumerate(out):
            if line.lower() == "[.shellclassinfo]":
                out.insert(idx + 1, icon_line)
                break
        icon_written = True
    return "\r\n".join(out) + "\r\n"


def _write_desktop_ini(group_dir: str, ico_path: str) -> bool:
    """写 <group_dir>\\desktop.ini（UTF-16，IconResource 指向 ico），并设 Hidden|System。"""
    ini = os.path.join(group_dir, "desktop.ini")
    tmp = ini + ".wf.tmp"
    try:
        existing = _read_text_best_effort(ini) if os.path.isfile(ini) else ""
        _clear_readonly(ini)
        content = _compose_desktop_ini_text(existing, ico_path)
        with open(tmp, "wb") as fh:
            fh.write(content.encode("utf-16"))
        os.replace(tmp, ini)
        _set_attrs(ini, _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)
        return True
    except Exception:
        logger.exception("写 desktop.ini 失败: %s", ini)
        try:
            if os.path.exists(tmp):
                _clear_readonly(tmp)
                os.remove(tmp)
        except OSError:
            pass
        return False


def _write_signature(group_dir: str, signature: str) -> bool:
    path = os.path.join(group_dir, ".wf", "icon.sig")
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _clear_readonly(path)
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(signature + "\n")
        os.replace(tmp, path)
        return True
    except Exception:
        logger.exception("写 icon.sig 失败: %s", path)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Shell 刷新
# ---------------------------------------------------------------------------


def _sh_change_notify(event: int, path1: str | None, path2: str | None = None) -> None:
    try:
        if not _is_windows_shell_ok():
            return
        flags = _SHCNF_PATHW | _SHCNF_FLUSH
        p1 = ctypes.c_wchar_p(path1) if path1 else None
        p2 = ctypes.c_wchar_p(path2) if path2 else None
        _shell32.SHChangeNotify(ctypes.c_long(event), flags, p1, p2)
    except Exception:
        logger.debug("SHChangeNotify(0x%X, %r) 失败", event, path1, exc_info=True)


def _apply_folder_custom_settings(group_dir: str, ico_path: str) -> bool:
    """调用 SHGetSetFolderCustomSettings(FCSM_ICONFILE|FCS_FORCEWRITE) 强制写图标设置。"""
    fn = _FOLDER_SETTINGS_FN
    if fn is None:
        return False
    try:
        fcs = _SHFOLDERCUSTOMSETTINGS()
        fcs.dwSize = ctypes.sizeof(_SHFOLDERCUSTOMSETTINGS)
        fcs.dwMask = _FCSM_ICONFILE
        fcs.dwFlags = 0
        fcs.pszIconFile = str(ico_path)
        fcs.cchIconFile = len(str(ico_path)) + 1
        fcs.iIconIndex = 0
        hr = fn(ctypes.byref(fcs), str(group_dir), _FCS_FORCEWRITE)
        if hr != 0:
            logger.debug("SHGetSetFolderCustomSettings hr=0x%X", hr & 0xFFFFFFFF)
            return False
        return True
    except Exception:
        logger.debug("SHGetSetFolderCustomSettings 调用异常", exc_info=True)
        return False


#: 批量/连续 apply 时避免重复排队延迟刷新的最短间隔（秒）
_DELAYED_REFRESH_DELAY = 0.35

#: 是否启用"延迟二次刷新"（实测可把桌面图标刷新延迟从 ~2.3s 降到 ~0.5s）
_DELAYED_REFRESH_ENABLED = True

_DELAYED_REFRESH_LOCK = threading.Lock()
_DELAYED_REFRESH_PENDING: set[str] = set()


def _schedule_delayed_refresh(group_dir: str, ico_path: str) -> None:
    """后台延迟 ~0.35s 再调用一次 shell API + 通知。

    实测（_research/icons_refresh_experiment2.json）：只靠第一次
    SHGetSetFolderCustomSettings，Explorer 桌面要约 2.3-2.6s 才把文件夹图标换成卡片；
    在 0.35s 后补一次强制写设置 + 通知，可把首次可见刷新降到 ~0.5s。
    使用 daemon 线程，任何异常只记日志，不影响调用方。
    """
    if not _DELAYED_REFRESH_ENABLED:
        return
    try:
        key = os.path.normcase(os.path.abspath(group_dir))
        with _DELAYED_REFRESH_LOCK:
            if key in _DELAYED_REFRESH_PENDING:
                return
            _DELAYED_REFRESH_PENDING.add(key)
    except Exception:
        logger.debug("延迟刷新排队失败", exc_info=True)
        return

    def _worker():
        try:
            time.sleep(_DELAYED_REFRESH_DELAY)
            _apply_folder_custom_settings(group_dir, ico_path)
            # shell 可能重写过 desktop.ini，确保内容仍指向我们的 ico，并恢复属性
            try:
                current = _read_icon_resource(group_dir)
                if (not current
                        or os.path.normcase(os.path.abspath(current)) != os.path.normcase(ico_path)):
                    _clear_readonly(group_dir)
                    _write_desktop_ini(group_dir, ico_path)
                    _set_readonly(group_dir)
            except Exception:
                logger.debug("延迟刷新后修正 desktop.ini 失败", exc_info=True)
            try:
                _set_attrs(os.path.join(group_dir, "desktop.ini"),
                           _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)
            except Exception:
                pass
            _set_readonly(group_dir)
            _sh_change_notify(_SHCNE_UPDATEITEM, group_dir)
            _sh_change_notify(_SHCNE_UPDATEDIR, group_dir)
            parent = os.path.dirname(os.path.normpath(group_dir))
            if parent and parent != group_dir:
                _sh_change_notify(_SHCNE_UPDATEDIR, parent)
        except Exception:
            logger.debug("延迟刷新失败: %s", group_dir, exc_info=True)
        finally:
            with _DELAYED_REFRESH_LOCK:
                _DELAYED_REFRESH_PENDING.discard(key)

    try:
        thread = threading.Thread(target=_worker, name="wf-icon-refresh", daemon=True)
        thread.start()
    except Exception:
        logger.debug("启动延迟刷新线程失败", exc_info=True)
        with _DELAYED_REFRESH_LOCK:
            _DELAYED_REFRESH_PENDING.discard(key)


def _notify_group_icon_changed(group_dir: str, ico_path: str) -> None:
    """通知 shell：ico 变了、分组目录变了、父目录（桌面）变了；再尝试强制写设置。"""
    parent = os.path.dirname(os.path.normpath(group_dir))
    # 1) 让 shell 重新读取桌面上的分组项
    _sh_change_notify(_SHCNE_UPDATEDIR, group_dir)
    if parent and parent != group_dir:
        _sh_change_notify(_SHCNE_UPDATEDIR, parent)
    _sh_change_notify(_SHCNE_UPDATEITEM, group_dir)
    # 2) 通知 ico 本身变化（图标缓存按文件路径失效）
    _sh_change_notify(_SHCNE_UPDATEITEM, ico_path)
    # 3) shell API 强制重写文件夹自定义图标（补充刷新手段，失败忽略）
    if _apply_folder_custom_settings(group_dir, ico_path):
        # API 可能重写过 desktop.ini，再通知一次目录
        _sh_change_notify(_SHCNE_UPDATEDIR, group_dir)
        if parent and parent != group_dir:
            _sh_change_notify(_SHCNE_UPDATEDIR, parent)


# ---------------------------------------------------------------------------
# 公开 API：分组目录 / 图标应用
# ---------------------------------------------------------------------------


def ensure_icon_dir(group_dir: str) -> str:
    """创建 <group_dir>\\.wf\\icons 并隐藏 .wf，返回 icons 目录绝对路径。"""
    group_dir = os.path.abspath(os.fspath(group_dir))
    icons_dir = os.path.join(group_dir, ".wf", "icons")
    try:
        os.makedirs(icons_dir, exist_ok=True)
    except OSError:
        logger.exception("创建 .wf/icons 失败: %s", icons_dir)
    try:
        wf_dir = os.path.join(group_dir, ".wf")
        attrs = _get_attrs(wf_dir)
        if attrs != _INVALID_FILE_ATTRIBUTES:
            wanted = attrs | _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
            if wanted != attrs:
                _set_attrs(wf_dir, wanted)
    except Exception:
        logger.debug("隐藏 .wf 失败: %s", group_dir, exc_info=True)
    return icons_dir


def _save_ico(card: Image.Image, ico_path: str) -> bool:
    tmp = ico_path + ".tmp"
    try:
        _clear_readonly(ico_path)
        card.save(tmp, format="ICO", sizes=list(ICO_SIZES))
        os.replace(tmp, ico_path)
        return True
    except Exception:
        logger.exception("保存 ico 失败: %s", ico_path)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _cleanup_old_icos(icons_dir: str, keep_name: str) -> None:
    try:
        for name in os.listdir(icons_dir):
            if name == keep_name:
                continue
            if not name.lower().endswith((".ico", ".tmp")):
                continue
            path = os.path.join(icons_dir, name)
            try:
                if os.path.isdir(path):
                    continue
                _set_attrs(path, _FILE_ATTRIBUTE_NORMAL)
                os.remove(path)
            except OSError:
                logger.debug("清理旧 ico 失败: %s", path, exc_info=True)
    except OSError:
        logger.debug("枚举 icons 目录失败: %s", icons_dir, exc_info=True)


def apply_group_icon(group_dir: str, app_paths: Sequence[str]) -> bool:
    """生成/更新分组文件夹图标（卡片 .ico + desktop.ini + shell 刷新）。

    内部用 extract_icon 取前 4 个图标（调用方已排序），生成多尺寸 ico，
    写 desktop.ini，设置文件夹 READONLY / desktop.ini Hidden|System，
    写 .wf\\icon.sig，并触发 shell 刷新。返回 True 表示已生成并应用。
    """
    try:
        if not group_dir:
            return False
        group_dir = os.path.abspath(os.fspath(group_dir))
        if not os.path.isdir(group_dir):
            logger.warning("apply_group_icon: 分组目录不存在: %s", group_dir)
            return False
        paths = []
        try:
            for path in app_paths or []:
                if path and os.path.exists(path):
                    paths.append(os.path.abspath(os.fspath(path)))
        except Exception:
            logger.debug("apply_group_icon: 过滤 app_paths 失败", exc_info=True)

        signature = icon_signature(paths)
        icons_dir = ensure_icon_dir(group_dir)

        images = []
        for path in paths[:4]:
            img = extract_icon(path)
            if img is not None:
                images.append(img)
        card = compose_group_card(images) if images else render_empty_card()

        ico_name = "folder_%s.ico" % (signature[:10] or "icon")
        ico_path = os.path.join(icons_dir, ico_name)
        if not _save_ico(card, ico_path):
            return False

        # 文件夹的 READONLY 位是 desktop.ini 自定义图标生效的前提之一，写 ini 前先清掉
        _clear_readonly(group_dir)
        ini_ok = _write_desktop_ini(group_dir, ico_path)
        _set_readonly(group_dir)
        if not ini_ok:
            logger.warning("apply_group_icon: desktop.ini 写入失败: %s", group_dir)

        _notify_group_icon_changed(group_dir, ico_path)

        # shell API 可能重写 desktop.ini（编码/格式变化）；确保最终仍指向我们的 ico，
        # 并恢复属性：文件夹 READONLY、desktop.ini Hidden|System。
        current = _read_icon_resource(group_dir)
        if not current or os.path.normcase(os.path.abspath(current)) != os.path.normcase(ico_path):
            logger.debug("desktop.ini 被 shell 重写，重新写入: %s", group_dir)
            _clear_readonly(group_dir)
            ini_ok = _write_desktop_ini(group_dir, ico_path)
            _set_readonly(group_dir)
            if not ini_ok:
                return False
            _sh_change_notify(_SHCNE_UPDATEDIR, group_dir)
            _sh_change_notify(_SHCNE_UPDATEITEM, ico_path)
        ini_path = os.path.join(group_dir, "desktop.ini")
        _set_attrs(ini_path, _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)
        _set_readonly(group_dir)
        if not _write_signature(group_dir, signature):
            return False
        _cleanup_old_icos(icons_dir, ico_name)
        # 实测 Explorer 首次按 desktop.ini 刷新图标有明显延迟（~2.3s）；
        # 后台 0.35s 后补一次强制写设置 + 通知，可把桌面可见刷新降到 ~0.5s。
        _schedule_delayed_refresh(group_dir, ico_path)
        return True
    except Exception:
        logger.exception("apply_group_icon 异常: %r", group_dir)
        return False


def read_signature(group_dir: str):
    """读取 <group_dir>\\.wf\\icon.sig 的内容；不存在/失败返回 None。"""
    try:
        if not group_dir:
            return None
        path = os.path.join(os.path.abspath(os.fspath(group_dir)), ".wf", "icon.sig")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except Exception:
        logger.debug("read_signature 失败: %r", group_dir, exc_info=True)
        return None


def needs_icon_refresh(group_dir: str, app_paths: Sequence[str]) -> bool:
    """判断分组图标是否需要重新生成：无 ico / 无 sig / sig 不一致 / ico 缺失时 True。"""
    try:
        if not group_dir:
            return True
        group_dir = os.path.abspath(os.fspath(group_dir))
        if not os.path.isdir(group_dir):
            return True
        icons_dir = os.path.join(group_dir, ".wf", "icons")
        if not os.path.isdir(icons_dir):
            return True
        signature = icon_signature(app_paths)
        if not signature or read_signature(group_dir) != signature:
            return True
        ico_path = _read_icon_resource(group_dir)
        if not ico_path or not os.path.isfile(ico_path):
            return True
        return False
    except Exception:
        logger.exception("needs_icon_refresh 异常: %r", group_dir)
        return True
