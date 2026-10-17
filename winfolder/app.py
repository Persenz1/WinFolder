# -*- coding: utf-8 -*-
"""WinFolder application: tray controller wiring watcher, popup, icons and groups."""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import sys
import threading
import time

from PySide6.QtCore import QLockFile, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import (
    __version__,
    autostart,
    config,
    dialogs,
    groups,
    icons,
    popup,
    theme,
    util,
    watcher,
)
from .manager import ManagerWindow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background icon generation
# ---------------------------------------------------------------------------

class IconWorker(QThread):
    """Generates folder preview icons without blocking the UI thread."""

    icon_ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[str, list[str]]] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stopping = False

    def request_icon(self, folder: str, apps: list[str]) -> None:
        with self._lock:
            self._items = [item for item in self._items if item[0] != folder]
            self._items.append((folder, list(apps)))
        self._event.set()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
        self._event.set()

    def run(self) -> None:  # noqa: D401 - Qt thread entry point
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA
        except Exception:  # noqa: BLE001
            pass
        try:
            while True:
                with self._lock:
                    stopping = self._stopping
                    item = self._items.pop(0) if self._items else None
                if item is None:
                    if stopping:
                        break
                    self._event.wait(0.25)
                    self._event.clear()
                    continue
                folder, apps = item
                try:
                    icons.apply_group_icon(folder, apps)
                    # Pre-warm the icon cache so the popup opens without a
                    # noticeable delay the first time (popup needs all app
                    # icons, not just the four previewed on the folder card).
                    for app_path in apps:
                        try:
                            icons.extract_icon(app_path)
                        except Exception:  # noqa: BLE001
                            pass
                    self.icon_ready.emit(folder)
                except Exception as exc:  # noqa: BLE001
                    log.warning("后台刷新图标失败 %s: %s", folder, exc)
        finally:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class WinFolderApp:
    def __init__(self, qapp: QApplication):
        self.qapp = qapp
        self.settings = config.load()
        self._groups: list[groups.Group] = []
        self._manager: ManagerWindow | None = None
        self._icon_ready_timer: QTimer | None = None
        self._popup_group_name: str | None = None
        self._last_closed_group: str | None = None
        self._popup_closed_at = 0.0
        self._tray_icon = self._make_tray_icon()
        self.qapp.setWindowIcon(self._tray_icon)

        self.popup = popup.get_popup()
        self.popup.closed.connect(self._on_popup_closed)

        self.watcher = watcher.ClickWatcher(self._group_names, None)
        self.watcher.group_clicked.connect(self._on_group_clicked)
        self.watcher.desktop_clicked.connect(self._on_desktop_clicked)
        self.watcher.set_exclude_rects_provider(self._exclude_rects)

        self.worker = IconWorker()
        self.worker.icon_ready.connect(self._on_icon_ready)
        self.worker.start()

        self._build_tray()
        self._scan(initial=True)

        self._theme = theme.current_theme()
        self._theme_watcher = theme.ThemeWatcher(self.qapp, interval_ms=3500)
        self._theme_watcher.theme_changed.connect(self._on_theme_changed)

        self._timer = QTimer()
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.watcher.start()

        QTimer.singleShot(350, self._maybe_show_welcome)

    # ------------------------------------------------------------------
    # Icon / tray helpers
    # ------------------------------------------------------------------
    def _make_tray_icon(self) -> QIcon:
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(250, 250, 252))
        painter.drawRoundedRect(4, 4, size - 8, size - 8, 15, 15)
        colors = [QColor(52, 168, 83), QColor(219, 68, 55),
                  QColor(66, 133, 244), QColor(255, 193, 7)]
        cell = 21
        gap = 3
        origin = 9
        for index, color in enumerate(colors):
            x = origin + (index % 2) * (cell + gap)
            y = origin + (index // 2) * (cell + gap)
            painter.setBrush(color)
            painter.drawRoundedRect(x, y, cell, cell, 5, 5)
        painter.end()
        return QIcon(pixmap)

    def app_icon(self) -> QIcon:
        return self._tray_icon

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._tray_icon)
        self.tray.setToolTip("WinFolder · 桌面文件夹")
        self.tray_menu = QMenu()
        theme.apply_menu_style(self.tray_menu)
        self._populate_tray_menu()
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _populate_tray_menu(self) -> None:
        """构建/重建托盘菜单（主题切换时重新套用样式）。"""
        menu = self.tray_menu
        menu.clear()
        theme.apply_menu_style(menu)
        self.act_manage = menu.addAction("管理分组…", self.show_manager)
        self.act_new = menu.addAction("新建分组…", lambda: self.create_group_dialog())
        self.act_refresh = menu.addAction("刷新所有图标", self.refresh_all_icons)
        menu.addSeparator()
        self.act_single = menu.addAction("单击弹出面板")
        self.act_single.setCheckable(True)
        self.act_single.setChecked(bool(self.settings.get("single_click_enabled", True)))
        self.act_single.toggled.connect(self.set_single_click)
        self.act_auto = menu.addAction("开机自启")
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(autostart.is_enabled())
        self.act_auto.toggled.connect(self.set_autostart)
        menu.addSeparator()
        menu.addAction("打开数据目录", self.open_data_dir)
        menu.addAction("使用说明", self.show_help)
        menu.addSeparator()
        menu.addAction("退出 WinFolder", self.quit)

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_manager()

    # ------------------------------------------------------------------
    # Group discovery
    # ------------------------------------------------------------------
    def _group_names(self) -> list[str]:
        return [group.name for group in self._groups]

    def groups_list(self) -> list[groups.Group]:
        return list(self._groups)

    def _scan(self, initial: bool = False) -> None:
        try:
            found = groups.discover_groups()
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描分组失败: %s", exc)
            return
        changed = len(found) != len(self._groups) or any(
            a.path != b.path for a, b in zip(found, self._groups))
        self._groups = found
        self._update_tooltip()
        for group in found:
            try:
                if group.needs_icon_refresh():
                    self.worker.request_icon(group.path, group.apps)
            except Exception as exc:  # noqa: BLE001
                log.debug("检查图标失败 %s: %s", group.path, exc)
        if changed and not initial and self._manager is not None and self._manager.isVisible():
            self._schedule_manager_refresh()

    def _update_tooltip(self) -> None:
        count = len(self._groups)
        self.tray.setToolTip(f"WinFolder · {count} 个分组\n单击桌面分组图标展开面板")

    def _tick(self) -> None:
        self._scan()

    # ------------------------------------------------------------------
    # Click handling
    # ------------------------------------------------------------------
    def _on_group_clicked(self, name: str, x: int, y: int) -> None:
        if not self.settings.get("single_click_enabled", True):
            return
        now = time.monotonic()
        log.info("group clicked: %r @ (%d,%d) popup_open=%r last_closed=%r dt=%.3f",
                 name, x, y, self.popup.isVisible(), self._last_closed_group,
                 now - self._popup_closed_at)
        if (self._last_closed_group == name and
                now - self._popup_closed_at < 0.45):
            # Qt 在按下时关闭了面板，这次抬起不应立刻重新打开。
            return
        if self.popup.isVisible():
            if self._popup_group_name == name:
                # 同一个分组：再点一次 = 收起（部分环境下 Qt 不会自动关）
                self.popup.hide()
                return
            # 另一个分组：直接切换到新分组（open_group 会重新定位）
        group = groups.group_by_name(name)
        if group is None:
            self._scan()
            group = groups.group_by_name(name)
        if group is None:
            return
        self._popup_group_name = name
        try:
            self.popup.open_group(group.name, group.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("打开面板失败 %s: %s", group.path, exc)

    def _on_popup_closed(self) -> None:
        self._popup_closed_at = time.monotonic()
        self._last_closed_group = self._popup_group_name
        log.info("popup closed (was=%r)", self._popup_group_name)
        self._popup_group_name = None

    def _on_desktop_clicked(self, x: int, y: int) -> None:
        try:
            if self.popup.isVisible():
                self.popup.hide()
        except Exception:  # noqa: BLE001
            pass

    def _exclude_rects(self) -> list[tuple[int, int, int, int]]:
        # Clicks inside the Qt popup window never resolve to a desktop icon, so
        # nothing needs to be excluded here.  Kept for the watcher API.
        return []

    # ------------------------------------------------------------------
    # Icon ready
    # ------------------------------------------------------------------
    def _on_icon_ready(self, folder: str) -> None:
        if self._manager is not None and self._manager.isVisible():
            self._schedule_manager_refresh()

    def _schedule_manager_refresh(self) -> None:
        if self._icon_ready_timer is not None:
            self._icon_ready_timer.stop()
        else:
            self._icon_ready_timer = QTimer()
            self._icon_ready_timer.setSingleShot(True)
            self._icon_ready_timer.setInterval(250)
            self._icon_ready_timer.timeout.connect(
                lambda: self._manager and self._manager.refresh())
        self._icon_ready_timer.start()

    # ------------------------------------------------------------------
    # Manager / dialogs
    # ------------------------------------------------------------------
    def show_manager(self) -> None:
        if self._manager is None:
            self._manager = ManagerWindow(self)
        self._manager.apply_theme(self._theme)
        self._manager.refresh()
        self._manager.show()
        self._manager.raise_()
        self._manager.activateWindow()
        theme.set_titlebar(self._manager, theme.is_dark(self._theme))
        self._force_foreground(self._manager)

    @staticmethod
    def _force_foreground(widget) -> None:
        """把管理窗口提到前台，但**不使用 AttachThreadInput**。

        背景：AttachThreadInput 是后台窗口抢前台的常见 hack，但它会把本进程
        与前台线程的输入队列临时挂在一起；一旦输入法/聊天软件正在该线程里组合
        文字，或者本进程被结束而没来得及 detach，就可能把用户的输入法状态弄卡。
        用户实测反馈过这一点，所以这里改成更温和的降级策略：

        1. 先直接 SetForegroundWindow（点托盘菜单时本进程本来就有前台权限）；
        2. 失败则用 SetWindowPos 置顶一闪（SWP_NOACTIVATE，不抢键盘焦点），
           窗口至少可见地浮到最前面，用户点一下即可。
        """
        try:
            hwnd = int(widget.winId())
            if not hwnd:
                return
            u32 = ctypes.windll.user32
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if u32.GetForegroundWindow() == hwnd:
                return
            if u32.SetForegroundWindow(hwnd):
                return
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
            u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            u32.SetForegroundWindow(hwnd)
        except Exception as exc:  # noqa: BLE001
            log.debug("置前管理窗口失败: %s", exc)

    def _dialog_parent(self):
        if self._manager is not None and self._manager.isVisible():
            return self._manager
        return None

    def create_group_dialog(self, parent=None) -> None:
        parent = parent or self._dialog_parent()
        name, ok = dialogs.get_text(
            parent,
            title="新建分组",
            label="给这个文件夹起个名字（创建后桌面上会出现这个文件夹）：",
            placeholder="例如：设计工具 / EDA / AI 编程 / 游戏",
            ok_text="创建",
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            group = groups.create_group(name)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_message(parent, "新建分组失败", f"{name}：{exc}", kind="error")
            return
        self._scan()
        self._notify("已创建分组", f"{group.name}\n把桌面快捷方式拖到文件夹图标上即可收纳。")
        if self._manager is not None and self._manager.isVisible():
            self._manager.refresh()

    def rename_group_dialog(self, group: groups.Group, parent=None) -> None:
        parent = parent or self._dialog_parent()
        name, ok = dialogs.get_text(
            parent,
            title="重命名分组",
            label=f"当前名称：{group.name}\n新的分组名称：",
            placeholder="例如：设计工具 / EDA / AI 编程 / 游戏",
            initial=group.name,
            ok_text="重命名",
        )
        name = (name or "").strip()
        if not ok or not name or name == group.name:
            return
        try:
            groups.rename_group(group, name)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_message(parent, "重命名失败", str(exc), kind="error")
        self._scan()

    def delete_group_dialog(self, group: groups.Group, parent=None) -> None:
        parent = parent or self._dialog_parent()
        confirmed = dialogs.confirm(
            parent,
            "删除分组",
            f"确定删除分组「{group.name}」吗？\n\n"
            "文件夹内的文件和子文件夹（包括隐藏项）会移回桌面。\n"
            "任何文件移动失败都会停止删除，并保留剩余内容。",
            ok_text="删除",
            cancel_text="取消",
            danger=True,
        )
        if not confirmed:
            return
        try:
            groups.delete_group(group, restore_apps=True)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_message(parent, "删除失败", str(exc), kind="error")
        self._scan()

    def open_group(self, group: groups.Group) -> None:
        try:
            util.open_in_explorer(group.path)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_message(
                self._dialog_parent(), "打开失败", str(exc), kind="error"
            )

    def refresh_all_icons(self) -> None:
        self._scan()
        for group in self._groups:
            self.worker.request_icon(group.path, group.apps)
        self._notify("WinFolder", "正在刷新分组图标…")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def single_click_enabled(self) -> bool:
        return bool(self.settings.get("single_click_enabled", True))

    def set_single_click(self, enabled: bool) -> None:
        self.settings["single_click_enabled"] = bool(enabled)
        config.save(self.settings)
        if self.act_single.isChecked() != bool(enabled):
            self.act_single.setChecked(bool(enabled))
        if not enabled:
            try:
                self.popup.hide()
            except Exception:  # noqa: BLE001
                pass

    def autostart_enabled(self) -> bool:
        return autostart.is_enabled()

    def set_autostart(self, enabled: bool) -> None:
        ok = autostart.enable() if enabled else autostart.disable()
        self.settings["autostart"] = bool(enabled)
        config.save(self.settings)
        if not ok:
            self._notify("WinFolder", "设置开机自启失败，请查看日志。")
        if self.act_auto.isChecked() != bool(enabled):
            self.act_auto.setChecked(bool(enabled))

    def open_data_dir(self) -> None:
        try:
            util.open_in_explorer(config.DATA_DIR)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_message(
                self._dialog_parent(), "打开失败", str(exc), kind="error"
            )

    def show_help(self) -> None:
        dialogs.show_message(
            self._dialog_parent(),
            "WinFolder 使用说明",
            "<b>三步用起来</b><br>"
            "① 托盘右键 →「新建分组」（或本窗口左下角）创建文件夹；<br>"
            "② 把桌面上的快捷方式拖到这个文件夹图标上，图标里的 2×2 预览会自动更新；<br>"
            "③ 单击桌面上的文件夹图标 → 弹出面板；点图标启动程序，点面板外面或按 Esc 收起。<br><br>"
            "<b>细节</b><br>"
            "• 双击文件夹图标 = 打开真实文件夹（批量拖入/整理时用）<br>"
            "• 分组就是普通文件夹：F2 可重命名，Ctrl+Z 可撤销误拖<br>"
            "• 删除分组：本窗口点「删除」，里面的快捷方式会自动移回桌面<br>"
            "• 托盘菜单可以暂停「单击弹出面板」、开机自启、退出<br>"
            "• 退出 WinFolder 后桌面会变回原来的样子，不残留任何东西",
            kind="info",
        )

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def _on_theme_changed(self, theme_name: str) -> None:
        """系统深浅色变化：重应用 palette/QSS，并刷新已打开的窗口与菜单。"""
        self._theme = theme.apply(self.qapp, theme_name)
        log.info("系统主题切换 -> %s", self._theme)
        try:
            theme.apply_menu_style(self.tray_menu)
        except Exception as exc:  # noqa: BLE001
            log.debug("托盘菜单应用主题失败: %s", exc)
        if self._manager is not None:
            self._manager.apply_theme(self._theme)
            theme.set_titlebar(self._manager, theme.is_dark(self._theme))
        try:
            self.popup.apply_theme(self._theme)
        except Exception as exc:  # noqa: BLE001
            log.debug("弹出面板应用主题失败: %s", exc)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _notify(self, title: str, message: str) -> None:
        try:
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 3500)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_show_welcome(self) -> None:
        if self.settings.get("welcome_shown"):
            return
        self.settings["welcome_shown"] = True
        config.save(self.settings)
        self._notify("WinFolder 已启动",
                     "托盘图标 → 新建分组\n把桌面快捷方式拖进去，单击图标即可展开。")
        self.show_manager()

    def quit(self) -> None:
        log.info("WinFolder 退出")
        try:
            self._theme_watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.worker.stop()
            self.worker.wait(2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        self.qapp.quit()


# ---------------------------------------------------------------------------
# Entry point helpers
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    config.ensure_dirs()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(
            config.LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except Exception:  # noqa: BLE001
        pass
    if sys.executable.lower().endswith("python.exe"):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)


def main() -> int:
    setup_logging()
    util.sanitize_child_environment()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("WinFolder")
    theme_name = theme.apply(app)
    log.info("启动主题: %s（系统 %s）", theme_name, theme.system_theme())

    lock = QLockFile(config.LOCK_PATH)
    lock.setStaleLockTime(5000)
    if not lock.tryLock(150):
        dialogs.show_message(
            None, "WinFolder", "WinFolder 已经在运行了（托盘图标里找一下）。"
        )
        return 0

    controller = WinFolderApp(app)
    app.winfolder = controller  # type: ignore[attr-defined]
    app.aboutToQuit.connect(lambda: None)
    log.info("WinFolder %s 启动", __version__)
    try:
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
