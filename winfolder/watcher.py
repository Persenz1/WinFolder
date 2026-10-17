# -*- coding: utf-8 -*-
"""WinFolder 桌面单击监听模块。

设计目标
--------
WinFolder 把桌面上的真实文件夹当作"安卓文件夹"。用户单击桌面上的分组文件夹
图标时，``ClickWatcher`` 发出 :attr:`ClickWatcher.group_clicked`，由 app.py 弹出
Android 风格的面板；点桌面空白处/非分组图标时发出
:attr:`ClickWatcher.desktop_clicked`，可用于关闭已打开的面板。

实现要点
--------
* 纯 Qt 主线程实现：``QTimer`` 每 ~16ms 轮询 ``GetAsyncKeyState(VK_LBUTTON)``
  与 ``GetCursorPos``（进程 DPI-aware，坐标均为**物理像素**），不新开线程。
* 按下时用 ``WindowFromPoint`` + ``GetAncestor(GA_ROOT)`` 确认起点在桌面
  （``Progman`` / ``WorkerW`` / 已知透明覆盖层），否则整段手势作废——点其他
  程序的最小化按钮后窗口消失、抬起落在桌面分组图标上的情况不会误触发。
* 在鼠标抬起、判定为"单击"后再做一次 UI Automation
  ``ControlFromPoint(x, y)``（实测 ~2-3ms），避免每次轮询都走 COM。
* 只有 UIA 顶层控件是桌面（``Program Manager`` / ``Progman``）时才可能发信号，
  Explorer 窗口里的同名文件夹、其他应用窗口一律忽略。
* exclude rect provider 用于屏蔽面板自身区域；双击/连点在 350ms 内同位置去抖。
* uiautomation 导入失败或任何 UIA 调用异常都只记 warning，绝不向 Qt 事件循环
  抛异常；``available`` 属性表示 UIA 是否可用。

线程约束：所有方法（start/stop/set_exclude_rects_provider）以及 timer 回调都必须在
创建该对象的 Qt 线程（主线程）里调用。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

__all__ = ["ClickWatcher"]

# --- Win32 常量 -------------------------------------------------------------
VK_LBUTTON = 0x01
POLL_INTERVAL_MS = 16          # ~60Hz，任务要求 16-25ms
CLICK_MAX_MOVE_PX = 6          # 按下到抬起的最大位移（欧氏距离），超过视为拖动
CLICK_MAX_HOLD_SEC = 0.9       # 按住超过该时长不视为单击（长按）
GROUP_DEBOUNCE_SEC = 0.35      # 双击/连点去抖窗口
GROUP_DEBOUNCE_DIST_PX = 8     # "同一位置"的判定半径
DESKTOP_TOP_CLASS = "Progman"
DESKTOP_TOP_NAME = "Program Manager"
DESKTOP_ICON_CONTROL_TYPE = "ListItemControl"

# --- Win32 绑定 -------------------------------------------------------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int

GA_ROOT = 2
# 按下起点必须落在这些顶层窗口上，手势才可能被当作桌面单击：
# Progman = 桌面；WorkerW = 部分场景下桌面图标列表的宿主；覆盖层见 _OVERLAY_TOP_CLASSES。
DESKTOP_PRESS_ROOT_CLASSES = frozenset({"Progman", "WorkerW"})


def _root_window_class_at(pos: tuple[int, int]) -> str:
    """物理坐标 pos 处顶层窗口的类名；取不到返回空串。"""
    pt = wintypes.POINT(pos[0], pos[1])
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return ""
    root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    buf = ctypes.create_unicode_buffer(256)
    if _user32.GetClassNameW(root, buf, len(buf)) == 0:
        return ""
    return buf.value


def _get_cursor_pos() -> Optional[tuple[int, int]]:
    """当前光标位置（物理像素）；失败返回 None。"""
    pt = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def _ensure_physical_coordinates() -> None:
    """确保本进程把 ``GetCursorPos`` 返回物理像素（与 UIA 的 rect 同一坐标系）。

    Qt6 在 QGuiApplication 初始化时已经设置 Per-Monitor V2 DPI 感知，此时直接返回；
    纯 headless（无 QGuiApplication）或 Qt 还没初始化时，由本函数先按 Qt6 默认的
    Per-Monitor V2 设置。若进程已经设置过 DPI 感知，再调用只会返回失败，
    不抛异常、也不改变已有设置。
    """
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is not None:
            return
    except Exception:  # pragma: no cover - QtGui 一定存在，这里只是防御
        pass
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2（Win8.1+；Win7 上走下面的老 API）
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass


# 在导入 uiautomation **之前**先设置 DPI 感知：
# uiautomation 模块在 import 时会无条件调用 SetProcessDpiAwareness(PerMonitor V1)，
# 若让它先执行，Qt6 之后设置 Per-Monitor V2 会失败并只能退回 V1。这里先按 Qt6
# 默认级别设置好（若 QGuiApplication 已存在则 Qt 已设置，直接跳过）。
_ensure_physical_coordinates()


# --- 从本机选定的 Python 环境导入 uiautomation ---------------------------
auto = None  # type: ignore[assignment]
_import_error = ""
try:
    import uiautomation as auto  # noqa: E402 - 先设置 DPI 感知
except Exception as exc:  # pragma: no cover - 本机可用，仅防御
    auto = None
    _import_error = f"{type(exc).__name__}: {exc}"
    logger.warning("WinFolder: 无法导入 uiautomation，点击监听不可用（%s）", _import_error)


class ClickWatcher(QObject):
    """桌面单击监听器（Qt 主线程、QTimer 轮询、UIA 判定）。

    Signals
    -------
    group_clicked(str, int, int)
        单击了桌面上的分组文件夹图标；参数为 ``(分组文件夹显示名, 物理 x, 物理 y)``。
    desktop_clicked(int, int)
        单击了桌面空白处或非分组桌面图标；参数为 ``(物理 x, 物理 y)``。
        起点在其他窗口（如点最小化按钮后抬起落在桌面）、落在 exclude rect 内、
        拖动/长按都不会发任何信号。
    """

    group_clicked = Signal(str, int, int)
    desktop_clicked = Signal(int, int)

    def __init__(
        self,
        group_names_provider: Callable[[], Sequence[str]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._group_names_provider = group_names_provider
        self._exclude_rects_provider: Optional[
            Callable[[], Sequence[tuple[int, int, int, int]]]
        ] = None

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)

        # 鼠标状态机
        self._button_down = False
        self._press_pos: Optional[tuple[int, int]] = None
        self._press_time = 0.0
        self._press_on_desktop = False

        # group_clicked 去抖
        self._last_group_pos: Optional[tuple[int, int]] = None
        self._last_group_time = 0.0

        # 异常日志限流
        self._error_count = 0
        self._last_error_log = 0.0

        #: uiautomation 是否可用（导入成功即 True；运行期异常不会翻转它）
        self.available: bool = auto is not None
        if not self.available:
            logger.warning(
                "ClickWatcher: uiautomation 不可用，start() 将不会启动监听"
                "（%s）",
                _import_error,
            )

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        """启动监听（幂等；不可用时只记 warning 并返回）。"""
        if self._timer.isActive():
            return
        if not self.available or auto is None:
            logger.warning("ClickWatcher.start(): uiautomation 不可用，未启动")
            return
        _ensure_physical_coordinates()
        self._reset_click_state()
        self._timer.start()
        logger.info(
            "ClickWatcher started (interval=%dms)", POLL_INTERVAL_MS
        )

    def stop(self) -> None:
        """停止监听（幂等）。"""
        if self._timer.isActive():
            self._timer.stop()
            logger.info("ClickWatcher stopped")
        self._reset_click_state()

    def set_exclude_rects_provider(
        self,
        provider: Optional[Callable[[], Sequence[tuple[int, int, int, int]]]],
    ) -> None:
        """设置 exclude rect provider。

        provider 返回物理坐标矩形 ``(left, top, right, bottom)`` 序列，落在其中的
        左键单击不产生任何信号（例如已打开的面板区域）。传 ``None`` 取消。
        """
        self._exclude_rects_provider = provider

    # ------------------------------------------------------------- timer 逻辑
    def _on_timer(self) -> None:
        try:
            self._poll()
        except Exception:  # 绝不让异常冒泡回 Qt 事件循环
            self._log_error("ClickWatcher 轮询异常")

    def _poll(self) -> None:
        try:
            state = _user32.GetAsyncKeyState(VK_LBUTTON)
        except Exception:
            self._log_error("GetAsyncKeyState 失败")
            return
        is_down = bool(state & 0x8000)
        pos = _get_cursor_pos()
        if pos is None:
            return

        if is_down:
            if not self._button_down:
                # 按下：记录起点，并确认起点在桌面上。松开时才做 UIA 判定。
                self._button_down = True
                self._press_pos = pos
                self._press_time = time.perf_counter()
                self._press_on_desktop = self._press_started_on_desktop(pos)
            return

        if not self._button_down:
            return

        # 抬起
        self._button_down = False
        press_pos = self._press_pos
        press_on_desktop = self._press_on_desktop
        self._press_pos = None
        self._press_on_desktop = False
        try:
            held = time.perf_counter() - self._press_time
        except Exception:
            held = 0.0
        self._handle_release(press_pos, pos, held, press_on_desktop)

    def _press_started_on_desktop(self, pos: tuple[int, int]) -> bool:
        """按下起点是否在桌面（或已知透明覆盖层）上。

        失败时放行（保持旧行为）：WindowFromPoint 这类 API 实际上不会失败，
        若真失败说明环境异常，此时仍走抬起时的 UIA 判定兜底。
        """
        try:
            cls = _root_window_class_at(pos)
        except Exception:
            self._log_error("WindowFromPoint 判定按下起点失败")
            return True
        return cls in DESKTOP_PRESS_ROOT_CLASSES or cls in self._OVERLAY_TOP_CLASSES

    def _handle_release(
        self,
        press_pos: Optional[tuple[int, int]],
        release_pos: tuple[int, int],
        held_seconds: float,
        press_on_desktop: bool,
    ) -> None:
        if press_pos is None:
            return
        if not press_on_desktop:
            return  # 起点在其他窗口（如最小化按钮），抬起后落在桌面也不算单击桌面
        if held_seconds > CLICK_MAX_HOLD_SEC:
            return  # 长按
        dx = release_pos[0] - press_pos[0]
        dy = release_pos[1] - press_pos[1]
        if dx * dx + dy * dy > CLICK_MAX_MOVE_PX * CLICK_MAX_MOVE_PX:
            return  # 拖动
        if self._in_excluded_rect(release_pos):
            return  # 面板自身区域
        self._dispatch_click(release_pos)

    # ------------------------------------------------------------- UIA 判定
    def _dispatch_click(self, pos: tuple[int, int]) -> None:
        if auto is None:
            return
        x, y = pos
        t0 = time.perf_counter()
        try:
            ctrl = auto.ControlFromPoint(x, y)
        except Exception:
            self._log_error(f"ControlFromPoint({x},{y}) 失败")
            return
        if ctrl is None:
            return
        try:
            ctrl_type = ctrl.ControlTypeName
            name = (ctrl.Name or "").strip()
            top = ctrl.GetTopLevelControl()
            top_class = (top.ClassName or "") if top is not None else ""
            top_name = (top.Name or "") if top is not None else ""
        except Exception:
            self._log_error("读取 UIA 控件属性失败")
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms > 40.0:
            logger.debug("UIA 判定耗时 %.1fms（%s）", elapsed_ms, ctrl_type)

        on_desktop = top_class == DESKTOP_TOP_CLASS or top_name == DESKTOP_TOP_NAME
        if not on_desktop:
            # 其他窗口（浏览器/资源管理器/面板/Qt 窗口……）不产生任何信号。
            # 例外：透明常驻覆盖层（如 NVIDIA Overlay 的 CEF-OSC-WIDGET）会拦在
            # UIA 命中测试前面——鼠标消息能穿透到桌面，ControlFromPoint 却返回
            # 覆盖层。对这类已知覆盖层做兜底：按坐标在桌面图标列表里几何命中。
            if not self._is_known_overlay(top_class, top_name):
                return
            hit = self._desktop_icon_at(x, y)
            if hit is not None and self._emit_group_click(hit, pos):
                return
            self.desktop_clicked.emit(x, y)
            return

        if ctrl_type == DESKTOP_ICON_CONTROL_TYPE and name:
            if self._emit_group_click(name, pos):
                return

        # 桌面空白区域，或点击了非分组的桌面图标
        logger.debug("desktop_clicked @ (%d,%d) (%s %r)", x, y, ctrl_type, name)
        self.desktop_clicked.emit(x, y)

    # 已知透明覆盖层：穿透鼠标消息，却会挡住 UIA 命中测试
    _OVERLAY_TOP_CLASSES = frozenset({"CEF-OSC-WIDGET"})  # NVIDIA GeForce Overlay
    _OVERLAY_TOP_NAME_KEYWORDS = ("nvidia geforce overlay",)

    def _is_known_overlay(self, top_class: str, top_name: str) -> bool:
        if top_class in self._OVERLAY_TOP_CLASSES:
            return True
        lowered = top_name.lower()
        return any(keyword in lowered for keyword in self._OVERLAY_TOP_NAME_KEYWORDS)

    def _emit_group_click(self, name: str, pos: tuple[int, int]) -> bool:
        """name 属于分组时去抖并发出 group_clicked；返回是否命中分组。"""
        if name not in self._group_names():
            return False
        x, y = pos
        if self._is_group_debounced(pos):
            logger.debug("group_clicked 去抖：%r @ (%d,%d)", name, x, y)
            return True
        self._last_group_pos = pos
        self._last_group_time = time.perf_counter()
        logger.info("group_clicked: %r @ (%d,%d)", name, x, y)
        self.group_clicked.emit(name, x, y)
        return True

    def _desktop_icon_at(self, x: int, y: int) -> Optional[str]:
        """覆盖层兜底：枚举桌面图标列表做几何命中，返回图标名；未命中返回 None。"""
        try:
            progman = auto.Control(ClassName=DESKTOP_TOP_CLASS, searchDepth=1)
            if not progman.Exists(1):
                return None
            listview = progman.Control(ClassName="SysListView32")
            if not listview.Exists(1):
                return None
            for item in listview.GetChildren():
                rect = item.BoundingRectangle
                if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
                    return (item.Name or "").strip() or None
        except Exception:
            self._log_error("桌面图标几何命中失败")
        return None

    def _group_names(self) -> set[str]:
        """调用 provider 取分组名集合（strip 后），异常时返回空集。"""
        provider = self._group_names_provider
        if provider is None:
            return set()
        try:
            raw = provider() or ()
        except Exception:
            self._log_error("group_names_provider 调用失败")
            return set()
        names: set[str] = set()
        try:
            for item in raw:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    names.add(text)
        except Exception:
            self._log_error("遍历 group_names_provider 结果失败")
        return names

    def _is_group_debounced(self, pos: tuple[int, int]) -> bool:
        last_pos = self._last_group_pos
        if last_pos is None:
            return False
        if time.perf_counter() - self._last_group_time >= GROUP_DEBOUNCE_SEC:
            return False
        dx = pos[0] - last_pos[0]
        dy = pos[1] - last_pos[1]
        return dx * dx + dy * dy <= GROUP_DEBOUNCE_DIST_PX * GROUP_DEBOUNCE_DIST_PX

    # ------------------------------------------------------------- exclude
    def _in_excluded_rect(self, pos: tuple[int, int]) -> bool:
        provider = self._exclude_rects_provider
        if provider is None:
            return False
        try:
            rects = provider() or ()
        except Exception:
            self._log_error("exclude_rects_provider 调用失败")
            return False
        try:
            x, y = pos
            for rect in rects:
                try:
                    if len(rect) != 4:
                        continue
                    left, top, right, bottom = (int(v) for v in rect)
                except Exception:
                    continue
                if left > right:
                    left, right = right, left
                if top > bottom:
                    top, bottom = bottom, top
                if left <= x < right and top <= y < bottom:
                    return True
        except Exception:
            self._log_error("遍历 exclude rects 失败")
        return False

    # ------------------------------------------------------------- 工具
    def _reset_click_state(self) -> None:
        self._button_down = False
        self._press_pos = None
        self._press_time = 0.0
        self._press_on_desktop = False

    def _log_error(self, message: str) -> None:
        """异常限流日志：每 5s 最多一条，带当前异常栈。"""
        now = time.perf_counter()
        self._error_count += 1
        if now - self._last_error_log < 5.0:
            return
        self._last_error_log = now
        logger.warning("%s（累计 %d 次）", message, self._error_count, exc_info=True)
