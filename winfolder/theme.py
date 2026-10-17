# -*- coding: utf-8 -*-
"""WinFolder 主题系统：跟随 Windows 深浅色，提供统一调色板 / QSS / 颜色常量。

功能
====
* :func:`system_theme` —— 读注册表 ``AppsUseLightTheme`` / ``SystemUsesLightTheme``
  判断系统主题；不可用时回退到 Qt ``styleHints().colorScheme()``；默认浅色。
* :func:`current_theme` —— 在系统主题基础上支持环境变量
  ``WINFOLDER_THEME=light|dark`` 覆盖（仅用于自动截图/测试）。
* :func:`apply` —— 用 Fusion 风格 + QPalette + 全局 QSS 让整个应用（含原生控件、
  QMessageBox、QMenu、QInputDialog 等）跟随主题。
* QSS 构造器：:func:`app_qss` / :func:`menu_qss` / :func:`manager_qss` /
  :func:`dialog_qss` / :func:`popup_qss`。
* :func:`set_titlebar` —— 按主题设置 DWM 深色/浅色标题栏（浅色下不再强制深色）。
* :class:`ThemeWatcher` —— QTimer 轮询系统主题变化，``theme_changed(str)`` 信号，
  app.py 用它实时重应用主题。

两套主题的所有颜色都集中在 :data:`THEMES` 里；面板/弹出菜单等自绘控件通过
``colors()`` 取 ``(r, g, b, a)`` 元组或 QSS 颜色字符串。
"""

from __future__ import annotations

import ctypes
import logging
import os

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QWidget

log = logging.getLogger(__name__)

FONT_FAMILY = "Microsoft YaHei UI"
_THEME_ENV = "WINFOLDER_THEME"
_VALID_THEMES = ("light", "dark")

# 注册表位置：HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize
_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

# ---------------------------------------------------------------------------
# 颜色常量（QSS 字符串 + 自绘用的 RGBA 元组）
# ---------------------------------------------------------------------------
LIGHT = {
    "name": "light",
    "is_dark": False,
    "titlebar_dark": False,
    # 基础界面
    "window": "#f3f5f9",
    "surface": "#ffffff",
    "surface_alt": "#eef1f6",
    "border": "rgba(0, 0, 0, 26)",
    "border_strong": "rgba(0, 0, 0, 42)",
    "text": "#1b1f27",
    "text_muted": "rgba(27, 31, 39, 160)",
    "text_faint": "rgba(27, 31, 39, 105)",
    "text_disabled": "rgba(27, 31, 39, 85)",
    "accent": "#2f6fed",
    "accent_hover": "#4a86f7",
    "accent_pressed": "#2559c9",
    "accent_text": "#ffffff",
    "hover": "rgba(0, 0, 0, 14)",
    "pressed": "rgba(0, 0, 0, 26)",
    "selection": "rgba(47, 111, 237, 40)",
    "danger": "#c0392b",
    "danger_bg": "rgba(192, 57, 43, 24)",
    # 工具提示 / 输入框 / 滚动条
    "tooltip_bg": "#ffffff",
    "tooltip_text": "#1b1f27",
    "tooltip_border": "rgba(0, 0, 0, 34)",
    "input_bg": "#ffffff",
    "input_border": "rgba(0, 0, 0, 46)",
    "scroll_handle": "rgba(0, 0, 0, 62)",
    "scroll_handle_hover": "rgba(0, 0, 0, 100)",
    # 菜单（托盘 / 右键）
    "menu_bg": "rgba(253, 254, 255, 251)",
    "menu_border": "rgba(0, 0, 0, 30)",
    "menu_text": "#1b1f27",
    "menu_hover": "rgba(0, 0, 0, 20)",
    "menu_separator": "rgba(0, 0, 0, 26)",
    "menu_disabled": "rgba(27, 31, 39, 90)",
    # 管理窗口
    "list_bg": "#ffffff",
    "list_item_hover": "rgba(47, 111, 237, 18)",
    "btn_bg": "#ffffff",
    "btn_hover": "#f0f3f9",
    "btn_pressed": "#e4e9f2",
    "btn_border": "rgba(0, 0, 0, 34)",
    "btn_text": "#1b1f27",
    # 弹出面板（浅色 = 半透明白玻璃 + 深色文字）
    "panel_top": (255, 255, 255, 242),
    "panel_mid": (248, 250, 253, 240),
    "panel_bottom": (240, 244, 249, 245),
    "panel_border": (0, 0, 0, 30),
    "shadow_color": (15, 23, 42, 255),
    "shadow_peak": 6,
    "tile_text": (24, 30, 40, 255),
    "tile_hover": (0, 0, 0, 16),
    "tile_pressed": (0, 0, 0, 28),
    "panel_title": "#14181f",
    "panel_subtitle": "rgba(27, 31, 39, 155)",
    "panel_footer": "rgba(27, 31, 39, 110)",
    "panel_glyph": "rgba(27, 31, 39, 75)",
    "panel_hint": "rgba(27, 31, 39, 200)",
    "panel_btn_bg": "rgba(0, 0, 0, 16)",
    "panel_btn_border": "rgba(0, 0, 0, 28)",
    "panel_btn_text": "#1b2430",
    "panel_btn_hover": "rgba(0, 0, 0, 26)",
    "panel_btn_pressed": "rgba(0, 0, 0, 38)",
    "panel_scroll_handle": "rgba(0, 0, 0, 70)",
    "panel_scroll_hover": "rgba(0, 0, 0, 115)",
}

DARK = {
    "name": "dark",
    "is_dark": True,
    "titlebar_dark": True,
    # 基础界面
    "window": "#1b1d23",
    "surface": "#23262e",
    "surface_alt": "#262a33",
    "border": "rgba(255, 255, 255, 20)",
    "border_strong": "rgba(255, 255, 255, 40)",
    "text": "#e8ecf4",
    "text_muted": "rgba(235, 240, 250, 160)",
    "text_faint": "rgba(235, 240, 250, 105)",
    "text_disabled": "rgba(235, 240, 250, 80)",
    "accent": "#3b82f6",
    "accent_hover": "#4b8df8",
    "accent_pressed": "#2f6fd0",
    "accent_text": "#ffffff",
    "hover": "rgba(255, 255, 255, 18)",
    "pressed": "rgba(255, 255, 255, 30)",
    "selection": "rgba(59, 130, 246, 70)",
    "danger": "#ff8f8f",
    "danger_bg": "rgba(255, 143, 143, 26)",
    # 工具提示 / 输入框 / 滚动条
    "tooltip_bg": "#262a33",
    "tooltip_text": "#f2f4f8",
    "tooltip_border": "rgba(255, 255, 255, 30)",
    "input_bg": "#23262e",
    "input_border": "rgba(255, 255, 255, 36)",
    "scroll_handle": "rgba(255, 255, 255, 58)",
    "scroll_handle_hover": "rgba(255, 255, 255, 96)",
    # 菜单
    "menu_bg": "rgba(30, 33, 40, 252)",
    "menu_border": "rgba(255, 255, 255, 42)",
    "menu_text": "#f2f4f8",
    "menu_hover": "rgba(255, 255, 255, 32)",
    "menu_separator": "rgba(255, 255, 255, 28)",
    "menu_disabled": "rgba(235, 240, 250, 90)",
    # 管理窗口
    "list_bg": "#23262e",
    "list_item_hover": "rgba(255, 255, 255, 10)",
    "btn_bg": "#2e323c",
    "btn_hover": "#3a3f4b",
    "btn_pressed": "#262a33",
    "btn_border": "rgba(255, 255, 255, 26)",
    "btn_text": "#e8ecf4",
    # 弹出面板（深色玻璃，保持原有观感）
    "panel_top": (38, 41, 50, 246),
    "panel_mid": (28, 30, 37, 244),
    "panel_bottom": (21, 23, 29, 247),
    "panel_border": (255, 255, 255, 34),
    "shadow_color": (0, 0, 0, 255),
    "shadow_peak": 9,
    "tile_text": (242, 244, 248, 255),
    "tile_hover": (255, 255, 255, 26),
    "tile_pressed": (255, 255, 255, 42),
    "panel_title": "#f5f7fa",
    "panel_subtitle": "rgba(235, 240, 250, 140)",
    "panel_footer": "rgba(235, 240, 250, 105)",
    "panel_glyph": "rgba(235, 240, 250, 55)",
    "panel_hint": "rgba(235, 240, 250, 205)",
    "panel_btn_bg": "rgba(255, 255, 255, 16)",
    "panel_btn_border": "rgba(255, 255, 255, 28)",
    "panel_btn_text": "rgba(235, 240, 250, 220)",
    "panel_btn_hover": "rgba(255, 255, 255, 32)",
    "panel_btn_pressed": "rgba(255, 255, 255, 48)",
    "panel_scroll_handle": "rgba(255, 255, 255, 58)",
    "panel_scroll_hover": "rgba(255, 255, 255, 95)",
}

THEMES = {"light": LIGHT, "dark": DARK}


# ---------------------------------------------------------------------------
# 主题检测
# ---------------------------------------------------------------------------
def _read_registry_theme() -> str | None:
    try:
        import winreg
    except ImportError:  # 非 Windows，理论上不会发生
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
            values = {}
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                try:
                    raw, _kind = winreg.QueryValueEx(key, name)
                    values[name] = int(raw)
                except OSError:
                    continue
        if "AppsUseLightTheme" in values:
            return "light" if values["AppsUseLightTheme"] else "dark"
        if "SystemUsesLightTheme" in values:
            return "light" if values["SystemUsesLightTheme"] else "dark"
    except OSError as exc:
        log.debug("读取系统主题注册表失败: %s", exc)
    return None


def _qt_color_scheme_theme() -> str | None:
    try:
        app = QGuiApplication.instance()
        if app is None:
            return None
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            return "light"
    except Exception as exc:  # noqa: BLE001
        log.debug("Qt colorScheme 检测失败: %s", exc)
    return None


def system_theme() -> str:
    """当前系统主题（不读取环境变量覆盖）。"""
    return _read_registry_theme() or _qt_color_scheme_theme() or "light"


def env_override() -> str | None:
    value = os.environ.get(_THEME_ENV, "").strip().lower()
    return value if value in _VALID_THEMES else None


def current_theme() -> str:
    """实际使用的主题：环境变量覆盖优先，其次系统主题。"""
    return env_override() or system_theme()


def resolve(name: str | None = None) -> str:
    if name in _VALID_THEMES:
        return str(name)
    return current_theme()


def colors(name: str | None = None) -> dict:
    return THEMES[resolve(name)]


def is_dark(name: str | None = None) -> bool:
    return bool(colors(name)["is_dark"])


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------
def to_qcolor(value) -> QColor:
    """把颜色常量（QColor / RGBA 元组 / #hex / rgba(...)）转成 QColor。"""
    if isinstance(value, QColor):
        return QColor(value)
    if isinstance(value, tuple):
        return QColor(*value)
    text = str(value).strip()
    if text.lower().startswith("rgba("):
        inner = text[text.index("(") + 1: text.rindex(")")]
        parts = [part.strip() for part in inner.split(",")]
        if len(parts) == 4:
            red, green, blue = (int(float(part)) for part in parts[:3])
            alpha_raw = parts[3]
            alpha = float(alpha_raw)
            color = QColor(red, green, blue)
            color.setAlphaF(alpha) if 0.0 <= alpha <= 1.0 else color.setAlpha(int(alpha))
            return color
    color = QColor(text)
    return color


def rgba_tuple(value, alpha: int | None = None) -> tuple[int, int, int, int]:
    color = to_qcolor(value)
    return (color.red(), color.green(), color.blue(),
            color.alpha() if alpha is None else alpha)


# ---------------------------------------------------------------------------
# QPalette
# ---------------------------------------------------------------------------
def qpalette(name: str | None = None) -> QPalette:
    c = colors(name)
    palette = QPalette()
    window = to_qcolor(c["window"])
    surface = to_qcolor(c["surface"])
    text = to_qcolor(c["text"])
    muted = to_qcolor(c["text_muted"])
    disabled = to_qcolor(c["text_disabled"])
    accent = to_qcolor(c["accent"])
    accent_text = to_qcolor(c["accent_text"])
    border = to_qcolor(c["border_strong"])

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, surface)
    palette.setColor(QPalette.ColorRole.AlternateBase, to_qcolor(c["surface_alt"]))
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, to_qcolor(c["btn_bg"]))
    palette.setColor(QPalette.ColorRole.ButtonText, to_qcolor(c["btn_text"]))
    palette.setColor(QPalette.ColorRole.BrightText, to_qcolor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, to_qcolor(c["tooltip_bg"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, to_qcolor(c["tooltip_text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, accent_text)
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.LinkVisited, accent)
    palette.setColor(QPalette.ColorRole.Mid, border)
    palette.setColor(QPalette.ColorRole.Dark, border)

    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base,
                     to_qcolor(c["surface_alt"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button,
                     to_qcolor(c["surface_alt"]))
    return palette


# ---------------------------------------------------------------------------
# QSS
# ---------------------------------------------------------------------------
def menu_qss(name: str | None = None) -> str:
    c = colors(name)
    return f"""
QMenu {{
    background-color: {c['menu_bg']};
    color: {c['menu_text']};
    border: 1px solid {c['menu_border']};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 26px 6px 12px;
    border-radius: 6px;
    color: {c['menu_text']};
    background: transparent;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QMenu::item:selected {{
    background: {c['menu_hover']};
    color: {c['menu_text']};
}}
QMenu::item:disabled {{
    color: {c['menu_disabled']};
}}
QMenu::separator {{
    height: 1px;
    background: {c['menu_separator']};
    margin: 4px 8px;
}}
QMenu::indicator {{
    width: 15px;
    height: 15px;
}}
"""


def app_qss(name: str | None = None) -> str:
    c = colors(name)
    return menu_qss(name) + f"""
QToolTip {{
    color: {c['tooltip_text']};
    background-color: {c['tooltip_bg']};
    border: 1px solid {c['tooltip_border']};
    border-radius: 6px;
    padding: 4px 7px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QMessageBox {{
    background-color: {c['window']};
}}
QMessageBox QLabel {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 9.5pt;
}}
QMessageBox QPushButton {{
    background-color: {c['btn_bg']};
    color: {c['btn_text']};
    border: 1px solid {c['btn_border']};
    border-radius: 8px;
    padding: 6px 16px;
    min-width: 64px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QMessageBox QPushButton:hover {{
    background-color: {c['btn_hover']};
}}
QMessageBox QPushButton:pressed {{
    background-color: {c['btn_pressed']};
}}
QDialog {{
    background-color: {c['window']};
}}
QLineEdit {{
    background-color: {c['input_bg']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {c['accent']};
    selection-color: {c['accent_text']};
    font-family: "{FONT_FAMILY}";
    font-size: 9.5pt;
}}
QLineEdit:focus {{
    border: 1px solid {c['accent']};
}}
"""


def dialog_qss(name: str | None = None) -> str:
    c = colors(name)
    return f"""
QDialog#wfDialog {{
    background-color: {c['window']};
}}
QLabel#wfDialogTitle {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 12pt;
    font-weight: 600;
}}
QLabel#wfDialogLabel {{
    color: {c['text_muted']};
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QLabel#wfDialogText {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 9.5pt;
}}
QLineEdit#wfDialogInput {{
    background-color: {c['input_bg']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 9px;
    padding: 8px 11px;
    selection-background-color: {c['accent']};
    selection-color: {c['accent_text']};
    font-family: "{FONT_FAMILY}";
    font-size: 10pt;
}}
QLineEdit#wfDialogInput:focus {{
    border: 1px solid {c['accent']};
}}
QPushButton#wfDialogOk {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: 1px solid {c['accent']};
    border-radius: 8px;
    padding: 7px 22px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
    font-weight: 600;
}}
QPushButton#wfDialogOk:hover {{
    background-color: {c['accent_hover']};
    border-color: {c['accent_hover']};
}}
QPushButton#wfDialogOk:pressed {{
    background-color: {c['accent_pressed']};
}}
QPushButton#wfDialogOk:disabled {{
    background-color: {c['surface_alt']};
    color: {c['text_disabled']};
    border-color: {c['border']};
}}
QPushButton#wfDialogOkDanger {{
    background-color: {c['danger']};
    color: {c['accent_text']};
    border: 1px solid {c['danger']};
    border-radius: 8px;
    padding: 7px 22px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
    font-weight: 600;
}}
QPushButton#wfDialogOkDanger:hover {{
    background-color: {c['danger']};
}}
QPushButton#wfDialogCancel {{
    background-color: {c['btn_bg']};
    color: {c['btn_text']};
    border: 1px solid {c['btn_border']};
    border-radius: 8px;
    padding: 7px 20px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QPushButton#wfDialogCancel:hover {{
    background-color: {c['btn_hover']};
}}
QPushButton#wfDialogCancel:pressed {{
    background-color: {c['btn_pressed']};
}}
"""


def manager_qss(name: str | None = None) -> str:
    c = colors(name)
    return f"""
QWidget#manager {{
    background-color: {c['window']};
}}
QWidget#row {{
    background: transparent;
}}
QLabel {{
    background: transparent;
}}
QLabel#title {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 15pt;
    font-weight: 600;
}}
QLabel#subtitle {{
    color: {c['text_muted']};
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QLabel#version {{
    color: {c['text_faint']};
    font-family: "{FONT_FAMILY}";
    font-size: 8pt;
}}
QLabel#name {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 10pt;
    font-weight: 600;
}}
QLabel#meta {{
    color: {c['text_muted']};
    font-family: "{FONT_FAMILY}";
    font-size: 8.5pt;
}}
QListWidget#groupList {{
    background-color: {c['list_bg']};
    border: 1px solid {c['border']};
    border-radius: 12px;
    padding: 6px;
    outline: none;
    color: {c['text']};
}}
QListWidget#groupList::item {{
    border-radius: 10px;
    padding: 2px;
}}
QListWidget#groupList::item:hover {{
    background-color: {c['list_item_hover']};
}}
QPushButton {{
    background-color: {c['btn_bg']};
    color: {c['btn_text']};
    border: 1px solid {c['btn_border']};
    border-radius: 8px;
    padding: 6px 14px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QPushButton:hover {{
    background-color: {c['btn_hover']};
}}
QPushButton:pressed {{
    background-color: {c['btn_pressed']};
}}
QPushButton#primary {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: 1px solid {c['accent']};
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background-color: {c['accent_hover']};
    border-color: {c['accent_hover']};
}}
QPushButton#primary:pressed {{
    background-color: {c['accent_pressed']};
}}
QPushButton#danger {{
    color: {c['danger']};
}}
QPushButton#danger:hover {{
    background-color: {c['danger_bg']};
}}
QCheckBox {{
    color: {c['text']};
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
    spacing: 8px;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['scroll_handle']};
    border-radius: 4px;
    min-height: 26px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['scroll_handle_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: transparent;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


def popup_qss(name: str | None = None) -> str:
    c = colors(name)
    return f"""
QWidget#wfContent, QWidget#wfGrid, QWidget#wfScrollBody, QWidget#wfEmpty {{
    background: transparent;
}}
QLabel {{
    background: transparent;
}}
QLabel#wfTitle {{
    color: {c['panel_title']};
    font-family: "{FONT_FAMILY}";
    font-size: 10pt;
    font-weight: 600;
}}
QLabel#wfCount {{
    color: {c['panel_subtitle']};
    font-family: "{FONT_FAMILY}";
    font-size: 8pt;
}}
QLabel#wfFooter {{
    color: {c['panel_footer']};
    font-family: "{FONT_FAMILY}";
    font-size: 8pt;
}}
QLabel#wfEmptyGlyph {{
    color: {c['panel_glyph']};
    font-family: "{FONT_FAMILY}";
    font-size: 20pt;
}}
QLabel#wfEmptyHint {{
    color: {c['panel_hint']};
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QPushButton#wfHeaderButton {{
    color: {c['panel_btn_text']};
    background-color: {c['panel_btn_bg']};
    border: 1px solid {c['panel_btn_border']};
    border-radius: 8px;
    padding: 3px 10px;
    font-family: "{FONT_FAMILY}";
    font-size: 8pt;
}}
QPushButton#wfHeaderButton:hover {{
    background-color: {c['panel_btn_hover']};
}}
QPushButton#wfHeaderButton:pressed {{
    background-color: {c['panel_btn_pressed']};
}}
QPushButton#wfEmptyButton {{
    color: {c['panel_btn_text']};
    background-color: {c['panel_btn_bg']};
    border: 1px solid {c['panel_btn_border']};
    border-radius: 9px;
    padding: 5px 20px;
    font-family: "{FONT_FAMILY}";
    font-size: 9pt;
}}
QPushButton#wfEmptyButton:hover {{
    background-color: {c['panel_btn_hover']};
}}
QPushButton#wfEmptyButton:pressed {{
    background-color: {c['panel_btn_pressed']};
}}
QScrollArea#wfScroll {{
    background: transparent;
    border: none;
}}
QScrollArea#wfScroll > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {c['panel_scroll_handle']};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['panel_scroll_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: transparent;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


# ---------------------------------------------------------------------------
# 应用 / 菜单 / 标题栏
# ---------------------------------------------------------------------------
def apply(app: QApplication | None = None, name: str | None = None) -> str:
    """把主题应用到 QApplication（Fusion + palette + 全局 QSS），返回主题名。"""
    target = app or QApplication.instance()
    theme_name = resolve(name)
    if target is not None:
        try:
            if str(target.style().objectName()).lower() != "fusion":
                target.setStyle("Fusion")
        except Exception as exc:  # noqa: BLE001
            log.debug("设置 Fusion 风格失败: %s", exc)
        target.setPalette(qpalette(theme_name))
        target.setStyleSheet(app_qss(theme_name))
        target.setFont(QFont(FONT_FAMILY, 9))
    return theme_name


def apply_menu_style(menu: QMenu, name: str | None = None) -> None:
    """托盘 / 右键菜单样式（圆角、hover、勾选项跟随主题）。"""
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    menu.setStyleSheet(menu_qss(name))
    menu.setFont(QFont(FONT_FAMILY, 9))


def set_titlebar(window: QWidget | int, dark: bool) -> None:
    """按主题设置 DWM 标题栏深浅（Windows 10 20H1+ / Windows 11）。"""
    try:
        hwnd = int(window) if isinstance(window, int) else int(window.winId())
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = ctypes.WinDLL("dwmapi")
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("设置标题栏主题失败: %s", exc)


# ---------------------------------------------------------------------------
# 主题变化轮询
# ---------------------------------------------------------------------------
class ThemeWatcher(QObject):
    """每 interval_ms 轮询一次系统主题，变化时发出 ``theme_changed``。"""

    theme_changed = Signal(str)

    def __init__(self, parent: QObject | None = None, interval_ms: int = 3500):
        super().__init__(parent)
        self._theme = current_theme()
        self._timer = QTimer(self)
        self._timer.setInterval(max(1000, int(interval_ms)))
        self._timer.timeout.connect(self.check_now)
        self._timer.start()

    def current(self) -> str:
        return self._theme

    def check_now(self) -> None:
        new_theme = current_theme()
        if new_theme and new_theme != self._theme:
            self._theme = new_theme
            log.info("系统主题变化: -> %s", new_theme)
            self.theme_changed.emit(new_theme)

    def stop(self) -> None:
        self._timer.stop()


# ---------------------------------------------------------------------------
# 简单演示（python winfolder/theme.py）
# ---------------------------------------------------------------------------
def _demo() -> int:  # pragma: no cover - 手动演示用
    import sys

    from PySide6.QtWidgets import (
        QCheckBox, QDialog, QLabel, QPushButton, QVBoxLayout,
    )

    app = QApplication(sys.argv)
    name = apply(app)
    dialog = QDialog()
    dialog.setWindowTitle(f"WinFolder 主题演示 · {name}")
    dialog.resize(420, 220)
    dialog.setStyleSheet(dialog_qss(name))
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(f"当前主题：{name}", dialog))
    layout.addWidget(QLabel("浅色/深色两套样式。设置 WINFOLDER_THEME=dark 可强制深色。", dialog))
    layout.addWidget(QCheckBox("示例勾选项", dialog))
    button = QPushButton("关闭", dialog)
    button.clicked.connect(dialog.accept)
    layout.addWidget(button)
    theme = apply(app)  # noqa: F841
    set_titlebar(dialog, is_dark(name))
    dialog.exec()
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(_demo())
