# -*- coding: utf-8 -*-
"""WinFolder 弹出面板：安卓风格的桌面分组应用面板（PySide6）。

模块职责
========
* :class:`FolderPopup` —— 无边框、半透明、深色毛玻璃风格面板。显示一个桌面
  分组文件夹里的快捷方式：4 列图标网格（图标 48、名称单行省略、悬停高亮），
  超过 16 个时使用 ``QScrollArea``（可见 4 行，深色细滚动条）。
* 面板出现在鼠标点击位置：``QCursor.pos()``（Qt 逻辑坐标）+ ``QGuiApplication.screenAt()``
  判定所在屏幕，靠近屏幕边缘时翻转并夹紧到 ``availableGeometry`` 内（8px 边距）。
* 点击面板外 / ``Esc`` / 启动应用后自动关闭；任何方式隐藏时发出 ``closed`` 信号。
* 右键菜单：打开 / 打开文件所在位置 / 从分组移出到桌面 / 删除快捷方式（回收站）；
  右键面板空白：打开文件夹 / 刷新图标（调用 ``winfolder.icons.apply_group_icon``）。
* 全局单例 API：:func:`get_popup` / :func:`hide_popup`，供 ``app.py`` 调用。

公开 API（签名不可变）
======================
``FolderPopup.closed``、``FolderPopup.open_group(group_name, folder_path)``、
``FolderPopup.refresh_if_same(folder_path)``、``get_popup()``、``hide_popup()``。

独立运行（demo）::

    python winfolder/popup.py [文件夹路径]     # 默认显示桌面文件夹内容

依赖：仅 PySide6（+ 可选 Pillow，用于接收 ``winfolder.icons.extract_icon`` 的返回值）。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional, Sequence

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QFileInfo,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileIconProvider,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:  # 包内导入
    from . import dialogs, theme
except ImportError:  # 作为单文件脚本运行（python winfolder/popup.py）时的兜底
    import dialogs  # type: ignore[no-redef]
    import theme  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 尺寸常量（Qt 逻辑坐标；本机主屏 150% 缩放）
# ---------------------------------------------------------------------------
FONT_FAMILY = theme.FONT_FAMILY

COLS = 4                        # 网格列数
TILE_W, TILE_H = 96, 100        # 单个 AppTile 尺寸
ICON_SIZE = 48                  # 图标绘制尺寸
ICON_TOP = 12                   # 图标相对 tile 顶部的偏移
NAME_TOP = 66                   # 名称相对 tile 顶部的偏移
NAME_H = 20                     # 名称行高
H_GAP, V_GAP = 4, 4             # 网格间距
GRID_W = COLS * TILE_W + (COLS - 1) * H_GAP          # 396
PANEL_PAD = 18                  # 面板内边距
PANEL_RADIUS = 22               # 面板圆角
SCROLLBAR_W = 8                 # 滚动条宽度（深色细样式）
VISIBLE_ROWS = 4                # 滚动区域最多可见 4 行
MAX_VISIBLE_ITEMS = COLS * VISIBLE_ROWS              # 16：超过后启用滚动
SCROLL_VIEW_W = GRID_W + SCROLLBAR_W                 # 404
SCROLL_VIEW_H = VISIBLE_ROWS * TILE_H + (VISIBLE_ROWS - 1) * V_GAP   # 412

# 窗口内留给自绘阴影的外边距（阴影偏移向下，所以底部略大）
SHADOW_L, SHADOW_T, SHADOW_R, SHADOW_B = 32, 26, 32, 44
FADE_MS = 120                   # 淡入动画时长
EDGE_MARGIN = 8                 # 面板与屏幕可用区域的最小边距
CURSOR_OFFSET = QPoint(14, 8)   # 面板相对鼠标的偏移
TITLE_W = 196                   # 标题最大宽度（超出省略）
LAUNCH_GUARD_S = 0.6            # 同一 tile 双击保护窗口

# 面板本身在 paintEvent 里自绘（避免 QGraphicsDropShadowEffect 在透明窗口上
# 出现黑色残影）；子控件样式与浅色/深色颜色由 winfolder.theme 统一提供。

_STRIP_EXTENSIONS = {".lnk", ".url", ".exe", ".bat", ".cmd", ".appref-ms"}

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4

# SHFileOperationW：把文件移入回收站（FOF_ALLOWUNDO）
_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400

_get_foreground_window = ctypes.windll.user32.GetForegroundWindow
_get_foreground_window.argtypes = []
_get_foreground_window.restype = wintypes.HWND


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


# ---------------------------------------------------------------------------
# 纯函数工具
# ---------------------------------------------------------------------------
def _clamp_popup_pos(
    cursor_pos: QPoint,
    size: QSize,
    available: QRect,
    offset: QPoint = CURSOR_OFFSET,
    margin: int = EDGE_MARGIN,
) -> QPoint:
    """计算面板左上角：优先鼠标右下方，放不下时翻转，最后夹紧到可用区域内。"""
    x = cursor_pos.x() + offset.x()
    y = cursor_pos.y() + offset.y()
    if x + size.width() > available.right() + 1:
        x = cursor_pos.x() - size.width() - offset.x()
    if y + size.height() > available.bottom() + 1:
        y = cursor_pos.y() - size.height() - offset.y()

    min_x = available.left() + margin
    min_y = available.top() + margin
    max_x = available.right() + 1 - size.width() - margin
    max_y = available.bottom() + 1 - size.height() - margin
    # 面板比屏幕还大时 max < min，此时贴左上角（右/下自然被裁掉）
    x = max(min_x, min(x, max_x))
    y = max(min_y, min(y, max_y))
    return QPoint(x, y)


def _pil_to_qimage(image) -> Optional[QImage]:
    """PIL.Image -> QImage（RGBA8888），不依赖 Pillow 的 ImageQt。"""
    try:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        data = image.tobytes("raw", "RGBA")
        qimage = QImage(
            data,
            image.width,
            image.height,
            image.width * 4,
            QImage.Format.Format_RGBA8888,
        )
        return qimage.copy()  # 复制一份，避免 QImage 引用临时 bytes
    except Exception:
        logger.warning("PIL 图像转换为 QImage 失败", exc_info=True)
        return None


def _display_name(path: str) -> str:
    """快捷方式显示名：去掉 .lnk/.url/.exe 等后缀。"""
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    return stem if ext.lower() in _STRIP_EXTENSIONS else name


def _is_hidden_or_system(path: str) -> bool:
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return False
    if attrs == -1 or attrs == 0xFFFFFFFF:
        return False
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def _natural_key(name: str) -> tuple:
    """自然排序 key（与 groups.util.natural_key 一致：'app 2' < 'app 10'）。"""
    text = (name or "").casefold()
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)
    )


def _groups_api():
    """拿到 ``winfolder.groups``（延迟导入，兼容独立脚本运行）。"""
    try:
        from . import groups as groups_module
        return groups_module
    except ImportError:
        pass
    try:
        from winfolder import groups as groups_module
        return groups_module
    except Exception:  # noqa: BLE001 - 面板自身不应因顺序模块失败而崩溃
        logger.debug("无法导入 winfolder.groups，忽略自定义顺序", exc_info=True)
        return None


def _list_app_paths(folder: str) -> list[str]:
    """列出分组内的文件和子文件夹（跳过隐藏条目 / desktop.ini）。

    若存在 ``.wf/order.json``（拖动排序结果），已记录的应用按记录顺序靠前，
    新出现/未记录的应用按自然顺序追加——与 ``groups.app_entries`` 保持一致。
    """
    groups_module = _groups_api()
    if groups_module is not None:
        return groups_module.app_entries(folder)
    paths: list[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not (entry.is_file() or entry.is_dir()):
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.startswith(".") or name.lower() == "desktop.ini":
                    continue
                if _is_hidden_or_system(entry.path):
                    continue
                paths.append(entry.path)
    except OSError:
        logger.warning("读取分组文件夹失败: %s", folder, exc_info=True)
        return []
    paths.sort(key=lambda p: _natural_key(os.path.basename(p)))

    return paths


def _startfile(path: str) -> None:
    """用 shell 打开文件/快捷方式（.lnk/.url/.exe/.文件夹都支持）。"""
    os.startfile(path)  # type: ignore[attr-defined]


def _reveal_in_explorer(path: str) -> bool:
    """在资源管理器中选中该文件。"""
    try:
        subprocess.Popen(["explorer", "/select," + os.path.normpath(path)])
        return True
    except OSError:
        logger.warning("打开文件所在位置失败: %s", path, exc_info=True)
        return False


def _desktop_dir() -> str:
    """桌面真实目录（兼容 OneDrive 重定向）。"""
    try:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation
        )
    except Exception:
        location = ""
    if location and os.path.isdir(location):
        return location
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def _unique_dest(directory: str, filename: str) -> str:
    """目标目录下的唯一路径：重名时自动加 " (2)"、"(3)" …"""
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem} ({index}){ext}")
        index += 1
    return candidate


def _move_to_desktop(path: str, dest_dir: Optional[str] = None) -> str:
    """把文件移回桌面（或指定目录）；返回最终路径。"""
    target_dir = dest_dir or _desktop_dir()
    if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(
        os.path.abspath(target_dir)
    ):
        raise OSError("文件已经在目标目录中")
    destination = _unique_dest(target_dir, os.path.basename(path))
    shutil.move(path, destination)
    return destination


def _recycle_path(path: str) -> bool:
    """用 SHFileOperationW(FOF_ALLOWUNDO) 把文件移入回收站。"""
    if not os.path.exists(path):
        return False
    try:
        buffer = ctypes.create_unicode_buffer(path + "\0")  # 双 NUL 结尾
        operation = _SHFILEOPSTRUCTW()
        operation.hwnd = None
        operation.wFunc = _FO_DELETE
        operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
        operation.pTo = None
        operation.fFlags = (
            _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
        )
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result != 0:
            logger.warning("SHFileOperationW 返回错误码 %s: %s", result, path)
        return result == 0 and not operation.fAnyOperationsAborted
    except Exception:
        logger.warning("移入回收站失败: %s", path, exc_info=True)
        return False


def _make_menu(parent: QWidget) -> QMenu:
    """跟随当前主题的圆角右键菜单。"""
    menu = QMenu(parent)
    theme.apply_menu_style(menu)
    return menu


def _load_icon_source(path: str, provider: QFileIconProvider):
    """取图标来源。

    返回 ``("image", QImage)`` / ``("icon", QIcon)`` / ``("none", None)``。
    优先 ``winfolder.icons.extract_icon``（256px，由图标模块并行提供），
    导入或提取失败时回退到 ``QFileIconProvider``，保证模块未就绪时也能工作。
    """
    try:
        from winfolder.icons import extract_icon  # 延迟导入，便于并行开发
    except Exception:
        extract_icon = None

    if extract_icon is not None:
        try:
            pil_image = extract_icon(path)
        except Exception:
            logger.warning("extract_icon 失败，回退 QFileIconProvider: %s", path,
                           exc_info=True)
            pil_image = None
        if pil_image is not None:
            qimage = _pil_to_qimage(pil_image)
            if qimage is not None and not qimage.isNull():
                return ("image", qimage)

    try:
        icon = provider.icon(QFileInfo(path))
    except Exception:
        logger.warning("QFileIconProvider 取图标失败: %s", path, exc_info=True)
        icon = QIcon()
    if not icon.isNull():
        return ("icon", icon)
    return ("none", None)


# ---------------------------------------------------------------------------
# 单个应用图标
# ---------------------------------------------------------------------------
class AppTile(QWidget):
    """网格中的一个应用：48px 图标居中 + 单行省略名称 + 悬停高亮 + 拖动排序。"""

    activated = Signal(str)
    context_menu_requested = Signal(str, QPoint)
    drag_started = Signal(str)          # 超过拖动阈值，path
    drag_moved = Signal(QPoint)         # 拖动中，全局坐标
    drag_released = Signal(QPoint)      # 释放，全局坐标

    def __init__(self, path: str, title: str, icon_source,
                 theme_name: Optional[str] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.path = path
        self.title = title
        self._icon_source = icon_source
        self._theme = theme.resolve(theme_name)
        self._pixmap: Optional[QPixmap] = None
        self._pixmap_dpr = -1.0
        self._hover = False
        self._pressed = False
        self._dragging = False
        self._press_global = QPoint()

        self.setFixedSize(TILE_W, TILE_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{title}\n拖动可调整顺序")
        self.setFont(QFont(FONT_FAMILY, 9))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_custom_context_menu)

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, event) -> None:
        colors = theme.colors(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        hover_rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self._hover or self._pressed:
            painter.setPen(Qt.PenStyle.NoPen)
            key = "tile_pressed" if self._pressed else "tile_hover"
            painter.setBrush(theme.to_qcolor(colors[key]))
            painter.drawRoundedRect(hover_rect, 12, 12)

        pixmap = self._current_pixmap()
        if pixmap is not None and not pixmap.isNull():
            target = QRectF(
                (self.width() - ICON_SIZE) / 2.0, ICON_TOP, ICON_SIZE, ICON_SIZE
            )
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))

        # 名称：单行、右端省略（浅色主题用深色文字，深色主题用浅色文字）
        painter.setFont(self.font())
        painter.setPen(theme.to_qcolor(colors["tile_text"]))
        text_rect = QRectF(4, NAME_TOP, self.width() - 8, NAME_H)
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(
            self.title, Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )

    def _current_pixmap(self) -> Optional[QPixmap]:
        """按窗口当前 DPR 生成 / 缓存图标（多屏不同缩放下保持清晰）。"""
        dpr = self.devicePixelRatioF()
        if dpr <= 0:
            dpr = 1.0
        if self._pixmap is not None and abs(self._pixmap_dpr - dpr) < 0.01:
            return self._pixmap

        kind, payload = self._icon_source
        pixmap: Optional[QPixmap] = None
        if kind == "image" and payload is not None:
            target = max(1, round(ICON_SIZE * dpr))
            scaled = payload.scaled(
                target,
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap = QPixmap.fromImage(scaled)
        elif kind == "icon" and payload is not None:
            pixmap = self._pixmap_from_icon(payload, dpr)

        if pixmap is not None and not pixmap.isNull():
            pixmap.setDevicePixelRatio(dpr)
        else:
            pixmap = None
        self._pixmap = pixmap
        self._pixmap_dpr = dpr
        return pixmap

    @staticmethod
    def _pixmap_from_icon(icon: QIcon, dpr: float) -> Optional[QPixmap]:
        target = max(1, round(ICON_SIZE * dpr))
        pixmap = icon.pixmap(QSize(target, target))
        if pixmap.isNull():
            return None
        if pixmap.width() != target:
            pixmap = pixmap.scaled(
                target,
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pixmap

    # -- 交互 ---------------------------------------------------------------
    def icon_pixmap(self) -> Optional[QPixmap]:
        """当前 DPR 下的图标（供拖拽 ghost 复用）。"""
        return self._current_pixmap()

    def is_dragging(self) -> bool:
        return self._dragging

    def cancel_drag(self) -> None:
        """外部取消（例如 Esc）：复位拖动状态，不触发排序。"""
        self._dragging = False
        self._pressed = False
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        if not self._dragging:
            self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._press_global = event.globalPosition().toPoint()
            self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            # 右键交给 CustomContextMenu（QContextMenuEvent）处理
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        if not self._dragging:
            distance = (global_pos - self._press_global).manhattanLength()
            if distance < QApplication.startDragDistance():
                return
            self._dragging = True
            self._pressed = False
            self.update()
            self.drag_started.emit(self.path)
        self.drag_moved.emit(global_pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._dragging
            self._dragging = False
            self._pressed = False
            self.update()
            if was_dragging:
                # 拖动结束后这次抬起只用于完成排序，不能触发启动
                self.drag_released.emit(event.globalPosition().toPoint())
                event.accept()
                return
            if self.rect().contains(event.position().toPoint()):
                self.activated.emit(self.path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_custom_context_menu(self, pos: QPoint) -> None:
        self.context_menu_requested.emit(self.path, self.mapToGlobal(pos))


# ---------------------------------------------------------------------------
# 拖动排序的浮层：半透明 ghost + 目标位置高亮 + 插入指示线
# ---------------------------------------------------------------------------
class _DragOverlay(QWidget):
    """覆盖在面板内容之上的无鼠标拖拽指示层（不参与点击）。"""

    def __init__(self, popup: "FolderPopup"):
        super().__init__(popup)
        self._popup = popup
        self._state: Optional[dict] = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_state(self, state: Optional[dict]) -> None:
        self._state = state
        if state is None:
            self.hide()
        else:
            self.show()
            self.raise_()
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        state = self._state
        if not state:
            return
        colors = theme.colors(self._popup._theme)
        accent = theme.to_qcolor(colors["accent"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 目标 tile 高亮
        highlight = state.get("highlight")
        if highlight:
            rect, radius = highlight
            color = QColor(accent)
            color.setAlpha(34)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect), radius, radius)

        # 插入指示线
        indicator = state.get("indicator")
        if indicator:
            rect = indicator
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(QRectF(rect), 2, 2)

        # 拖拽 ghost（半透明图标 + 名称，跟随光标）
        tile = state.get("tile")
        cursor_pos = state.get("cursor", QPoint())
        if tile is not None:
            ghost = QRectF(cursor_pos.x() - TILE_W // 2, cursor_pos.y() - TILE_H // 2,
                           TILE_W, TILE_H)
            background = theme.to_qcolor(colors["panel_top"])
            background.setAlpha(215)
            border = theme.to_qcolor(colors["panel_border"])
            border.setAlpha(120)
            painter.setBrush(background)
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(ghost.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)

            pixmap = tile.icon_pixmap()
            if pixmap is not None and not pixmap.isNull():
                target = QRectF(ghost.x() + (TILE_W - ICON_SIZE) / 2.0,
                                ghost.y() + ICON_TOP, ICON_SIZE, ICON_SIZE)
                painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))

            painter.setFont(QFont(FONT_FAMILY, 9))
            painter.setPen(theme.to_qcolor(colors["tile_text"]))
            text_rect = QRectF(ghost.x() + 4, ghost.y() + NAME_TOP, TILE_W - 8, NAME_H)
            metrics = QFontMetrics(QFont(FONT_FAMILY, 9))
            elided = metrics.elidedText(
                tile.title, Qt.TextElideMode.ElideRight, int(text_rect.width())
            )
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )


# ---------------------------------------------------------------------------
# 弹出面板
# ---------------------------------------------------------------------------
class FolderPopup(QWidget):
    """安卓风格的桌面分组弹出面板。

    ``closed`` 在任何方式隐藏时发出（点击外部 / Esc / 启动应用 / 手动 hide）。
    """

    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setObjectName("wfPopup")
        self.setWindowTitle("WinFolder 分组面板")

        self._group_name = ""
        self._folder = ""
        self._apps: list[str] = []
        self._content: Optional[QWidget] = None
        self._icon_provider: Optional[QFileIconProvider] = None
        self._icon_cache: dict[str, tuple[str, object]] = {}
        self._last_launch: dict[str, float] = {}
        self._fade_anim: Optional[QPropertyAnimation] = None
        self._theme = theme.current_theme()

        # 拖动排序状态
        self._drag_active = False
        self._drag_tile: Optional[AppTile] = None
        self._drag_path = ""
        self._drag_slot: Optional[int] = None
        self._overlay = _DragOverlay(self)

        # 失焦自动隐藏：guard 计数（菜单/对话框/主题重应用/拖拽期间不隐藏）
        self._deactivate_guard = 0
        self._auto_hide_enabled = True
        self._suppress_auto_hide_until = 0.0
        self._opening_foreground: Optional[int] = None
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setInterval(90)
        self._auto_hide_timer.timeout.connect(self._maybe_auto_hide)

        # 自绘阴影 / 面板：窗口内容区 = 阴影外边距 + 面板内边距
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            SHADOW_L + PANEL_PAD,
            SHADOW_T + PANEL_PAD,
            SHADOW_R + PANEL_PAD,
            SHADOW_B + PANEL_PAD,
        )
        self._layout.setSpacing(0)
        self.setStyleSheet(theme.popup_qss(self._theme))

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_blank_context_menu)

    # ==================================================================
    # 失焦自动隐藏（Alt+Tab / 点其它窗口后自动收起）
    # ==================================================================
    def event(self, event) -> bool:  # noqa: N802 - Qt 命名
        event_type = event.type()
        if event_type in (
            QEvent.Type.WindowDeactivate,
            QEvent.Type.ApplicationDeactivate,
        ):
            self._schedule_auto_hide_check()
        elif event_type == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                self._schedule_auto_hide_check()
        return super().event(event)

    def _schedule_auto_hide_check(self) -> None:
        if not self._auto_hide_enabled or not self.isVisible():
            return
        self._auto_hide_timer.start()

    def _auto_hide_guarded(self) -> bool:
        """当前是否处于“不能因失焦关闭”的状态。"""
        if not self._auto_hide_enabled or self._drag_active or self._drag_tile is not None:
            return True
        if self._deactivate_guard > 0:
            return True
        if time.monotonic() < self._suppress_auto_hide_until:
            return True
        app = QApplication.instance()
        if app is not None:
            active_popup = app.activePopupWidget()
            if active_popup is not None and active_popup is not self:
                return True
            if app.activeModalWidget() is not None:  # 我们的对话框打开中
                return True
        return False

    def _maybe_auto_hide(self) -> None:
        if not self.isVisible():
            return
        # 后台监听弹出的 Qt.Popup 可能未获得 Windows 前台焦点；此时 Qt 的
        # activeWindow / isActiveWindow 仍可能为真，也不会再收到失焦事件。
        foreground = _get_foreground_window()
        if not foreground:
            return  # Windows 正在切换前台窗口
        if foreground == int(self.winId()):
            self._opening_foreground = None
            return
        if self._auto_hide_guarded():
            return
        # 未取得前台焦点时允许打开它的桌面继续作为前台；切到别的窗口才收起。
        if foreground == self._opening_foreground:
            return
        logger.debug("面板失焦，自动隐藏")
        self.hide()

    @contextlib.contextmanager
    def _auto_hide_guard(self):
        """在菜单/对话框/主题重应用等期间临时禁止失焦自动隐藏。"""
        self._deactivate_guard += 1
        try:
            yield
        finally:
            self._deactivate_guard = max(0, self._deactivate_guard - 1)
            if self.isVisible():
                # 回到面板后重新判断一次（期间若已切到别的窗口则收起）
                QTimer.singleShot(0, self._maybe_auto_hide)

    def _restore_popup_activation(self) -> None:
        """菜单/对话框关闭后把激活状态还给面板，避免被误判为失焦。"""
        if not self.isVisible() or self._drag_active:
            return
        try:
            self.raise_()
            self.activateWindow()
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 公开 API（签名不可变）
    # ==================================================================
    def open_group(self, group_name: str, folder_path: str) -> None:
        """读取分组文件夹内容，并让面板出现在当前鼠标位置。"""
        folder = os.path.normpath(os.path.abspath(folder_path))
        if not os.path.isdir(folder):
            logger.warning("open_group: 文件夹不存在: %s", folder)
        self._group_name = group_name or os.path.basename(folder) or folder
        self._folder = folder

        was_visible = self.isVisible()
        self._opening_foreground = _get_foreground_window()
        self._reload()
        # 可见窗口重建内容后，Qt 要等事件循环才更新外层布局的 sizeHint，
        # 直接 adjustSize() 会拿到旧的最小尺寸，导致随后的定位夹紧算错
        # （面板被放到屏幕外）。用内容 sizeHint + 边距立即算出最终尺寸。
        self._resize_to_content()
        self._place_at_cursor()

        if was_visible:
            self.raise_()
            self.update()
        else:
            self._show_with_fade()
        self._auto_hide_timer.start()

    def _resize_to_content(self) -> None:
        """把窗口调整到与当前内容匹配的最终尺寸（见 open_group 里的说明）。"""
        self.adjustSize()
        content = self._content
        if content is None:
            return
        margins = self._layout.contentsMargins()
        hint = content.sizeHint()
        if not hint.isValid():
            return
        width = hint.width() + margins.left() + margins.right()
        height = hint.height() + margins.top() + margins.bottom()
        if width > 0 and height > 0 and (self.width() != width or self.height() != height):
            self.resize(width, height)

    def _scroll_area(self) -> Optional[QScrollArea]:
        return self.findChild(QScrollArea)

    def _scroll_value(self) -> int:
        scroll = self._scroll_area()
        if scroll is None:
            return 0
        return scroll.verticalScrollBar().value()

    def _restore_scroll(self, value: int) -> None:
        scroll = self._scroll_area()
        if scroll is None or value <= 0:
            return
        bar = scroll.verticalScrollBar()
        bar.setValue(min(value, bar.maximum()))

    def _refresh_content(self, preserve_scroll: bool = True) -> None:
        """重建内容并尽量保留滚动位置（排序/删除/刷新后调用）。"""
        value = self._scroll_value() if preserve_scroll else 0
        self._reload()
        self._resize_to_content()
        self._ensure_on_screen()
        self._restore_scroll(value)
        self.update()

    def refresh_if_same(self, folder_path: str) -> None:
        """面板正在显示同一文件夹时刷新内容（文件夹内容变化后调用）。"""
        if not self.isVisible():
            return
        folder = os.path.normpath(os.path.abspath(folder_path))
        if os.path.normcase(folder) != os.path.normcase(self._folder):
            return
        self._refresh_content(preserve_scroll=True)

    def apply_theme(self, theme_name: Optional[str] = None) -> None:
        """切换浅色/深色主题（app.py 在系统主题变化时调用）。"""
        with self._auto_hide_guard():
            self._theme = theme.resolve(theme_name)
            self.setStyleSheet(theme.popup_qss(self._theme))
            if self._content is not None:
                # 重建内容，让 QLabel / QPushButton 用新主题的 QSS 重新渲染
                self._refresh_content(preserve_scroll=True)
            self.update()

    # ==================================================================
    # 外观：自绘阴影 + 主题化毛玻璃圆角面板
    # ==================================================================
    def _card_rect(self) -> QRect:
        return self.rect().adjusted(SHADOW_L, SHADOW_T, -SHADOW_R, -SHADOW_B)

    def paintEvent(self, event) -> None:
        colors = theme.colors(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(self.rect()).adjusted(SHADOW_L, SHADOW_T, -SHADOW_R, -SHADOW_B)

        self._paint_shadow(painter, card, colors)

        # 毛玻璃：浅色 = 半透明白玻璃，深色 = 深色玻璃；上浅下深渐变 + 1px 描边
        gradient = QLinearGradient(card.topLeft(), card.bottomLeft())
        gradient.setColorAt(0.0, theme.to_qcolor(colors["panel_top"]))
        gradient.setColorAt(0.55, theme.to_qcolor(colors["panel_mid"]))
        gradient.setColorAt(1.0, theme.to_qcolor(colors["panel_bottom"]))
        painter.setPen(QPen(theme.to_qcolor(colors["panel_border"]), 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(
            card.adjusted(0.5, 0.5, -0.5, -0.5), PANEL_RADIUS, PANEL_RADIUS
        )

    @staticmethod
    def _paint_shadow(painter: QPainter, card: QRectF, colors: dict) -> None:
        """多层圆角矩形叠加模拟柔和外发光阴影（整体略微向下偏移）。"""
        layers = 20
        spread = 26.0
        step = spread / layers
        peak = int(colors.get("shadow_peak", 9))
        shadow_color = theme.to_qcolor(colors.get("shadow_color", (0, 0, 0, 255)))
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(layers, 0, -1):
            grow = index * step
            alpha = int(peak * ((layers - index + 1) / layers) ** 2)
            rect = card.adjusted(-grow, -grow + 5.0, grow, grow + 5.0)
            color = QColor(shadow_color)
            color.setAlpha(max(0, alpha))
            painter.setBrush(color)
            painter.drawRoundedRect(rect, PANEL_RADIUS + grow, PANEL_RADIUS + grow)

    # ==================================================================
    # 内容构建
    # ==================================================================
    def _provider(self) -> QFileIconProvider:
        if self._icon_provider is None:
            self._icon_provider = QFileIconProvider()
        return self._icon_provider

    def _icon_source_for(self, path: str):
        cached = self._icon_cache.get(path)
        if cached is not None:
            return cached
        source = _load_icon_source(path, self._provider())
        self._icon_cache[path] = source
        return source

    def _reload(self) -> None:
        """重新扫描文件夹并重建面板内容。"""
        self._apps = _list_app_paths(self._folder)
        # 图标引擎按文件修改时间缓存；面板按路径的缓存只保留到本次重建。
        self._icon_cache.clear()
        if self._content is not None:
            self._content.setParent(None)
            self._content.deleteLater()
            self._content = None
        self._content = self._build_content()
        self._layout.addWidget(self._content)
        self._layout.activate()
        logger.debug("分组 %r 重新加载：%d 个应用", self._group_name, len(self._apps))

    def _build_content(self) -> QWidget:
        scrolling = len(self._apps) > MAX_VISIBLE_ITEMS
        inner_w = SCROLL_VIEW_W if scrolling else GRID_W

        content = QWidget(self)
        content.setObjectName("wfContent")
        content.setFixedWidth(inner_w)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        column.addLayout(self._build_header())
        column.addSpacing(10)

        if not self._apps:
            column.addWidget(self._build_empty_state())
        else:
            if scrolling:
                column.addWidget(self._build_scroll_area())
            else:
                column.addWidget(self._build_grid())
            column.addSpacing(8)
            column.addWidget(self._build_footer())

        self._install_blank_context(content)
        return content

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel(self)
        title_font = QFont(FONT_FAMILY, 10)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        title_metrics = QFontMetrics(title_font)
        elided_title = title_metrics.elidedText(
            self._group_name, Qt.TextElideMode.ElideRight, TITLE_W - 4
        )
        title.setText(elided_title)
        title.setObjectName("wfTitle")
        # 短标题不要占满固定宽度，让“应用数量”紧跟在标题后面
        title.setFixedWidth(min(TITLE_W, title_metrics.horizontalAdvance(elided_title) + 4))
        title.setToolTip(self._group_name)
        self._install_blank_context(title)

        count = QLabel(self)
        count.setObjectName("wfCount")
        count.setText("暂无应用" if not self._apps else f"{len(self._apps)} 个应用")
        count.setFont(QFont(FONT_FAMILY, 8))
        self._install_blank_context(count)

        open_button = QPushButton("打开文件夹", self)
        open_button.setObjectName("wfHeaderButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        open_button.setFixedHeight(22)
        open_button.setFont(QFont(FONT_FAMILY, 8))
        open_button.clicked.connect(self._open_folder)

        row.addWidget(title)
        row.addWidget(count)
        row.addStretch(1)
        row.addWidget(open_button)
        return row

    def _build_grid(self) -> QWidget:
        widget = QWidget(self)
        widget.setObjectName("wfGrid")
        widget.setFixedWidth(GRID_W)
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(H_GAP)
        grid.setVerticalSpacing(V_GAP)
        for index, path in enumerate(self._apps):
            tile = self._make_tile(path, widget)
            grid.addWidget(tile, index // COLS, index % COLS)
        self._install_blank_context(widget)
        return widget

    def _build_scroll_area(self) -> QScrollArea:
        rows = (len(self._apps) + COLS - 1) // COLS
        scroll = QScrollArea(self)
        scroll.setObjectName("wfScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(False)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFixedSize(SCROLL_VIEW_W, SCROLL_VIEW_H)
        scroll.viewport().setAutoFillBackground(False)

        body = QWidget(scroll)
        body.setObjectName("wfScrollBody")
        body.setFixedSize(GRID_W, rows * TILE_H + (rows - 1) * V_GAP)
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(H_GAP)
        grid.setVerticalSpacing(V_GAP)
        for index, path in enumerate(self._apps):
            tile = self._make_tile(path, body)
            grid.addWidget(tile, index // COLS, index % COLS)

        scroll_bar = scroll.verticalScrollBar()
        scroll_bar.setSingleStep(TILE_H // 3)
        scroll_bar.setPageStep(SCROLL_VIEW_H)
        scroll.setWidget(body)

        self._install_blank_context(scroll)
        self._install_blank_context(scroll.viewport())
        self._install_blank_context(body)
        return scroll

    def _build_footer(self) -> QLabel:
        footer = QLabel(
            "拖动图标可排序 · 拖入文件即可添加 · 点面板外或按 Esc 关闭", self
        )
        footer.setObjectName("wfFooter")
        footer.setFont(QFont(FONT_FAMILY, 8))
        footer.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        footer.setFixedHeight(18)
        self._install_blank_context(footer)
        return footer

    def _build_empty_state(self) -> QWidget:
        box = QWidget(self)
        box.setObjectName("wfEmpty")
        box.setFixedHeight(178)
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addStretch(1)

        glyph = QLabel("＋", box)
        glyph.setObjectName("wfEmptyGlyph")
        glyph.setFont(QFont(FONT_FAMILY, 20))
        glyph.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        glyph.setFixedHeight(42)

        hint = QLabel("把桌面图标拖到这个文件夹上", box)
        hint.setObjectName("wfEmptyHint")
        hint.setFont(QFont(FONT_FAMILY, 9))
        hint.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        hint.setFixedHeight(26)

        open_button = QPushButton("打开文件夹", box)
        open_button.setObjectName("wfEmptyButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        open_button.setFixedHeight(30)
        open_button.setFont(QFont(FONT_FAMILY, 9))
        open_button.clicked.connect(self._open_folder)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(open_button)
        button_row.addStretch(1)

        column.addWidget(glyph)
        column.addSpacing(4)
        column.addWidget(hint)
        column.addSpacing(14)
        column.addLayout(button_row)
        column.addStretch(1)

        self._install_blank_context(box)
        self._install_blank_context(glyph)
        self._install_blank_context(hint)
        return box

    def _make_tile(self, path: str, parent: QWidget) -> AppTile:
        tile = AppTile(
            path, _display_name(path), self._icon_source_for(path), self._theme, parent
        )
        tile.activated.connect(self._launch)
        tile.context_menu_requested.connect(self._show_tile_menu)
        tile.drag_started.connect(self._on_tile_drag_started)
        tile.drag_moved.connect(self._on_tile_drag_moved)
        tile.drag_released.connect(self._on_tile_drag_released)
        return tile

    def _install_blank_context(self, widget: QWidget) -> None:
        """右键空白区域（标题/页脚/网格底板等）弹出“打开文件夹 / 刷新图标”。"""
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, source=widget: self._on_blank_context_menu(source.mapToGlobal(pos))
        )

    # ==================================================================
    # 显示 / 定位 / 关闭
    # ==================================================================
    def _place_at_cursor(self) -> None:
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        target = _clamp_popup_pos(cursor_pos, self.size(), screen.availableGeometry())
        self.move(target)

    def _ensure_on_screen(self) -> None:
        """面板尺寸变化后，保证它仍在当前屏幕可用区域内。"""
        point = self.pos()
        screen = (
            QGuiApplication.screenAt(point)
            or self.screen()
            or QGuiApplication.primaryScreen()
        )
        if screen is None:
            return
        area = screen.availableGeometry()
        x = max(
            area.left() + EDGE_MARGIN,
            min(point.x(), area.right() + 1 - self.width() - EDGE_MARGIN),
        )
        y = max(
            area.top() + EDGE_MARGIN,
            min(point.y(), area.bottom() + 1 - self.height() - EDGE_MARGIN),
        )
        self.move(x, y)

    # ==================================================================
    # 拖动排序（面板内自绘拖拽，全程不关闭面板）
    # ==================================================================
    def _tiles(self) -> list[AppTile]:
        return self.findChildren(AppTile)

    def _tile_rect(self, tile: AppTile) -> QRect:
        return QRect(tile.mapTo(self, QPoint(0, 0)), tile.size())

    def _compute_drop_slot(self, global_pos: QPoint) -> int:
        """返回插入位置 0..n：光标落在第 i 个 tile 左半区 = 插到 i 之前。"""
        tiles = self._tiles()
        if not tiles:
            return 0
        local = self.mapFromGlobal(global_pos)
        rows = [tiles[index:index + COLS] for index in range(0, len(tiles), COLS)]

        # 先按垂直距离找目标行（处理行距 / 跨行拖动），再在该行内按左右半区定位
        best_row_index = 0
        best_distance: Optional[float] = None
        for row_index, row in enumerate(rows):
            rects = [self._tile_rect(tile) for tile in row]
            row_top = min(rect.top() for rect in rects)
            row_bottom = max(rect.bottom() for rect in rects)
            distance = abs(local.y() - (row_top + row_bottom) / 2.0)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_row_index = row_index

        row = rows[best_row_index]
        row_start = best_row_index * COLS
        for offset, tile in enumerate(row):
            rect = self._tile_rect(tile)
            if local.x() < rect.center().x():
                return row_start + offset
        return row_start + len(row)

    def _drop_visual(
        self, slot: int
    ) -> tuple[Optional[tuple[QRect, int]], Optional[QRect]]:
        """插入位置 -> (目标 tile 高亮矩形, 插入指示线矩形)。"""
        tiles = self._tiles()
        if not tiles:
            return None, None
        slot = max(0, min(slot, len(tiles)))
        highlight = None
        if slot < len(tiles):
            rect = self._tile_rect(tiles[slot])
            highlight = (rect.adjusted(2, 2, -2, -2), 12)
            line_rect = QRect(rect.left() - 2, rect.top() + 8, 4, rect.height() - 16)
        else:
            rect = self._tile_rect(tiles[-1])
            line_rect = QRect(rect.right() + 1, rect.top() + 8, 4, rect.height() - 16)
        return highlight, line_rect

    def _on_tile_drag_started(self, path: str) -> None:
        if self._drag_active:
            return
        tile = next((item for item in self._tiles() if item.path == path), None)
        if tile is None:
            return
        self._drag_active = True
        self._drag_tile = tile
        self._drag_path = path
        self._drag_slot = None
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        except Exception:  # noqa: BLE001
            pass
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        self._overlay.set_state(
            {
                "tile": tile,
                "cursor": self.mapFromGlobal(QCursor.pos()),
                "highlight": None,
                "indicator": None,
            }
        )
        logger.debug("开始拖动排序: %s", _display_name(path))

    def _on_tile_drag_moved(self, global_pos: QPoint) -> None:
        if not self._drag_active:
            return
        self._auto_scroll_for_drag(global_pos)
        slot = self._compute_drop_slot(global_pos)
        self._drag_slot = slot
        highlight, indicator = self._drop_visual(slot)
        self._overlay.set_state(
            {
                "tile": self._drag_tile,
                "cursor": self.mapFromGlobal(global_pos),
                "highlight": highlight,
                "indicator": indicator,
            }
        )

    def _auto_scroll_for_drag(self, global_pos: QPoint) -> None:
        """拖动到滚动区域上下边缘时自动滚动（>16 个应用时可用）。"""
        scroll = self._scroll_area()
        if scroll is None:
            return
        viewport = scroll.viewport()
        local = viewport.mapFromGlobal(global_pos)
        bar = scroll.verticalScrollBar()
        step = max(12, TILE_H // 4)
        if local.y() < 26:
            bar.setValue(bar.value() - step)
        elif local.y() > viewport.height() - 26:
            bar.setValue(bar.value() + step)

    def _on_tile_drag_released(self, global_pos: QPoint) -> None:
        if not self._drag_active:
            return
        slot = self._compute_drop_slot(global_pos)
        inside = self._card_rect().contains(self.mapFromGlobal(global_pos))
        try:
            if inside:
                self._finish_drag(slot)
        finally:
            self._end_drag()

    def _end_drag(self) -> None:
        self._drag_active = False
        self._drag_tile = None
        self._drag_path = ""
        self._drag_slot = None
        self._overlay.set_state(None)
        try:
            QApplication.restoreOverrideCursor()
        except Exception:  # noqa: BLE001
            pass

    def _cancel_drag(self) -> None:
        if self._drag_tile is not None:
            try:
                self._drag_tile.cancel_drag()
            except Exception:  # noqa: BLE001
                pass
        self._end_drag()
        self.update()

    def _finish_drag(self, slot: int) -> None:
        """把拖动的应用插入下标 *slot*，持久化并刷新面板/桌面卡片。"""
        apps = list(self._apps)
        if len(apps) < 2 or self._drag_path not in apps:
            return
        current = apps.index(self._drag_path)
        slot = max(0, min(slot, len(apps)))
        if slot in (current, current + 1):
            return  # 顺序未变化
        destination = slot if slot < current else slot - 1
        reordered = list(apps)
        item = reordered.pop(current)
        reordered.insert(destination, item)
        if reordered == apps:
            return

        groups_module = _groups_api()
        if groups_module is None:
            self._show_message("排序失败", "分组模块当前不可用，无法保存顺序。")
            return
        try:
            groups_module.save_app_order(self._folder, reordered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存拖动顺序失败: %s", exc)
            self._show_message("排序失败", f"无法保存顺序：\n{exc}")
            return
        self._apps = reordered
        self._refresh_content(preserve_scroll=True)
        self._refresh_group_icon_async()
        logger.info(
            "拖动排序完成: %s", [os.path.basename(path) for path in reordered]
        )

    def _refresh_group_icon_async(self) -> None:
        """后台刷新桌面分组卡片图标（顺序变化 → icon_signature 变化 → 重刷）。"""
        folder = self._folder
        apps = list(self._apps)
        if not folder or not apps:
            return

        def _worker() -> None:
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA，shell 图标需要
            except Exception:  # noqa: BLE001
                pass
            try:
                from winfolder.icons import apply_group_icon
                apply_group_icon(folder, apps)
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台刷新分组图标失败: %s", exc)
            finally:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(
            target=_worker, name="wf-popup-order-icon", daemon=True
        ).start()

    def _show_with_fade(self) -> None:
        # 打开面板本身不能立刻被失焦逻辑误关
        self._suppress_auto_hide_until = time.monotonic() + 0.35
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        animation = QPropertyAnimation(self, b"windowOpacity", self)
        animation.setDuration(FADE_MS)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(self._on_fade_finished)
        self._fade_anim = animation
        animation.start()

    def _on_fade_finished(self) -> None:
        if self.isVisible():
            self.setWindowOpacity(1.0)

    # ==================================================================
    # 事件
    # ==================================================================
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            overlay.setGeometry(self.rect())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            if self._drag_active:
                self._cancel_drag()
            self.hide()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        # 点在窗口里但落在面板阴影区（视觉上在面板外）也视为“点外面”
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._card_rect().contains(event.position().toPoint()):
                event.accept()
                if self._drag_active:
                    self._cancel_drag()
                self.hide()
                return
        super().mousePressEvent(event)

    def hideEvent(self, event) -> None:
        self._auto_hide_timer.stop()
        if self._drag_active or self._drag_tile is not None:
            self._cancel_drag()
        super().hideEvent(event)
        self.closed.emit()

    def _dialog_parent(self) -> Optional[QWidget]:
        """提示/错误对话框的父窗口：面板可见时以面板为父，保证显示在面板上方。"""
        return self if self.isVisible() else None

    def _show_message(self, title: str, text: str, kind: str = "error") -> None:
        """主题化提示框；对话框打开期间不允许失焦自动隐藏。"""
        with self._auto_hide_guard():
            dialogs.show_message(self._dialog_parent(), title, text, kind=kind)
            self._restore_popup_activation()

    # ==================================================================
    # 启动 / 右键菜单
    # ==================================================================
    def _launch(self, path: str) -> None:
        if not path:
            return
        if not os.path.exists(path):
            self._show_message("无法启动", f"目标不存在：\n{path}", kind="error")
            self._after_content_mutation()
            return
        now = time.monotonic()
        if now - self._last_launch.get(path, 0.0) < LAUNCH_GUARD_S:
            return  # 双击保护：同一 tile 600ms 内只启动一次
        self._last_launch[path] = now
        try:
            _startfile(path)
        except OSError as exc:
            logger.warning("启动失败 %s: %s", path, exc)
            self._show_message(
                "无法启动",
                f"{_display_name(path)}\n\n{exc}",
                kind="error",
            )
            return
        self.hide()

    def _open_folder(self) -> None:
        folder = self._folder
        if not folder or not os.path.isdir(folder):
            return
        try:
            _startfile(folder)
        except OSError as exc:
            logger.warning("打开文件夹失败 %s: %s", folder, exc)
            self._show_message("无法打开文件夹", f"{folder}\n\n{exc}", kind="error")
            return
        self.hide()

    def _build_tile_menu(self, path: str) -> Optional[QMenu]:
        if not self._folder:
            return None
        menu = _make_menu(self)
        menu.wf_actions = (  # type: ignore[attr-defined]
            menu.addAction("打开"),
            menu.addAction("打开文件所在位置"),
        )
        menu.addSeparator()
        menu.wf_actions += (  # type: ignore[attr-defined]
            menu.addAction("从分组移出到桌面"),
            menu.addAction("移入回收站"),
        )
        return menu

    def _show_tile_menu(self, path: str, global_pos: QPoint) -> None:
        menu = self._build_tile_menu(path)
        if menu is None:
            return
        with self._auto_hide_guard():
            chosen = menu.exec(global_pos)
            self._restore_popup_activation()
        action_open, action_reveal, action_move, action_delete = menu.wf_actions  # type: ignore[attr-defined]
        if chosen is action_open:
            self._launch(path)
        elif chosen is action_reveal:
            _reveal_in_explorer(path)
        elif chosen is action_move:
            self._move_tile_to_desktop(path)
        elif chosen is action_delete:
            self._delete_tile(path)

    def _build_blank_menu(self) -> Optional[QMenu]:
        if not self._folder:
            return None
        menu = _make_menu(self)
        menu.wf_actions = (  # type: ignore[attr-defined]
            menu.addAction("打开文件夹"),
            menu.addAction("刷新图标"),
        )
        return menu

    def _on_blank_context_menu(self, global_pos: QPoint) -> None:
        menu = self._build_blank_menu()
        if menu is None:
            return
        with self._auto_hide_guard():
            chosen = menu.exec(global_pos)
            self._restore_popup_activation()
        action_open, action_refresh = menu.wf_actions  # type: ignore[attr-defined]
        if chosen is action_open:
            self._open_folder()
        elif chosen is action_refresh:
            self._refresh_group_icon()

    def _move_tile_to_desktop(self, path: str) -> None:
        source_dir = os.path.dirname(os.path.abspath(path))
        if os.path.normcase(source_dir) == os.path.normcase(os.path.abspath(_desktop_dir())):
            self._show_message("无需移动", "该快捷方式已经在桌面上了。", kind="info")
            return
        try:
            destination = _move_to_desktop(path)
        except OSError as exc:
            logger.warning("移回桌面失败 %s: %s", path, exc)
            self._show_message("移动失败", f"{path}\n\n{exc}", kind="error")
            return
        logger.info("已移回桌面: %s -> %s", path, destination)
        self._after_content_mutation()

    def _delete_tile(self, path: str) -> None:
        if not _recycle_path(path):
            self._show_message(
                "删除失败",
                f"无法把该项目移入回收站：\n{path}",
                kind="error",
            )
            return
        logger.info("已移入回收站: %s", path)
        self._after_content_mutation()

    def _refresh_group_icon(self) -> None:
        try:
            from winfolder.icons import apply_group_icon  # 并行开发，可能暂不存在
        except Exception:
            self._show_message("刷新图标", "图标模块（winfolder.icons）当前不可用。", kind="info")
            return
        error: Optional[Exception] = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._refresh_content(preserve_scroll=True)
            if not apply_group_icon(self._folder, list(self._apps)):
                error = OSError("无法更新分组图标，请检查文件夹是否可写。")
        except Exception as exc:  # 图标失败不应让面板崩溃
            logger.warning("刷新分组图标失败: %s", self._folder, exc_info=True)
            error = exc
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            self._show_message("刷新图标失败", str(error), kind="error")
            return

    def _after_content_mutation(self) -> None:
        """移动 / 删除快捷方式后刷新面板内容（保留滚动位置）。"""
        self._refresh_content(preserve_scroll=True)


# ---------------------------------------------------------------------------
# 全局单例 API
# ---------------------------------------------------------------------------
_popup: Optional[FolderPopup] = None


def get_popup() -> FolderPopup:
    """返回全局唯一的弹出面板（懒创建）。调用前需已有 QApplication。"""
    global _popup
    if _popup is None:
        if QApplication.instance() is None:
            raise RuntimeError("FolderPopup 需要先创建 QApplication")
        _popup = FolderPopup()
    return _popup


def hide_popup() -> None:
    """隐藏全局面板（若尚未创建则什么都不做）。"""
    if _popup is not None and _popup.isVisible():
        _popup.hide()


# ---------------------------------------------------------------------------
# 独立运行 demo
# ---------------------------------------------------------------------------
def _demo(argv: Sequence[str]) -> int:
    app = QApplication(list(argv))
    app.setApplicationName("WinFolder 弹出面板 Demo")
    theme.apply(app)
    target = argv[1] if len(argv) > 1 else os.path.join(
        os.environ.get("USERPROFILE", ""), "Desktop"
    )
    if not os.path.isdir(target):
        print(f"文件夹不存在：{target}")
        return 2

    popup = get_popup()
    popup.closed.connect(app.quit)  # demo：Esc / 点击外部退出
    group_name = os.path.basename(target.rstrip("\\/")) or target
    from PySide6.QtCore import QTimer

    QTimer.singleShot(0, lambda: popup.open_group(group_name, target))
    print(f"Demo：显示分组 {group_name!r}（{target}）")
    print("按 Esc 或点击面板外退出。")
    return app.exec()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    sys.exit(_demo(sys.argv))
