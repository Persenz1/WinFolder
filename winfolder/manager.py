# -*- coding: utf-8 -*-
"""The little management window (tray -> 管理分组)。深浅色主题跟随系统。"""
from __future__ import annotations

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QFileIconProvider, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from . import __version__, theme


class ManagerWindow(QWidget):
    """Non-modal window listing all groups with management actions."""

    def __init__(self, controller):
        super().__init__(None)
        self.controller = controller
        self._theme = theme.current_theme()
        self.setObjectName("manager")
        self.setWindowTitle("WinFolder 桌面文件夹")
        self.setWindowIcon(controller.app_icon())
        self.resize(700, 580)
        self.apply_theme(self._theme)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("桌面文件夹", self)
        title.setObjectName("title")
        header.addWidget(title)
        version = QLabel(f"v{__version__}", self)
        version.setObjectName("version")
        header.addWidget(version, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        root.addLayout(header)

        subtitle = QLabel(
            "① 点「新建分组」创建文件夹　② 把桌面上的快捷方式拖到这个文件夹图标上　"
            "③ 单击文件夹图标展开面板，点面板外面或按 Esc 收起；双击仍是打开真实文件夹。",
            self,
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.list = QListWidget(self)
        self.list.setObjectName("groupList")
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addWidget(self.list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.btn_new = QPushButton("新建分组", self)
        self.btn_new.setObjectName("primary")
        self.btn_new.clicked.connect(lambda: self.controller.create_group_dialog(self))
        actions.addWidget(self.btn_new)
        self.btn_refresh = QPushButton("刷新全部图标", self)
        self.btn_refresh.clicked.connect(self.controller.refresh_all_icons)
        actions.addWidget(self.btn_refresh)
        actions.addStretch(1)
        self.chk_single = QCheckBox("单击弹出面板", self)
        self.chk_single.setChecked(self.controller.single_click_enabled())
        self.chk_single.toggled.connect(self.controller.set_single_click)
        actions.addWidget(self.chk_single)
        self.chk_start = QCheckBox("开机自启", self)
        self.chk_start.setChecked(self.controller.autostart_enabled())
        self.chk_start.toggled.connect(self.controller.set_autostart)
        actions.addWidget(self.chk_start)
        root.addLayout(actions)

        footer = QHBoxLayout()
        btn_data = QPushButton("打开数据目录", self)
        btn_data.clicked.connect(lambda: self.controller.open_data_dir())
        footer.addWidget(btn_data)
        btn_help = QPushButton("使用说明", self)
        btn_help.clicked.connect(self.controller.show_help)
        footer.addWidget(btn_help)
        footer.addStretch(1)
        self.lbl_status = QLabel("", self)
        self.lbl_status.setObjectName("meta")
        footer.addWidget(self.lbl_status)
        root.addLayout(footer)

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def apply_theme(self, theme_name: str | None = None) -> None:
        """应用浅色/深色样式（主题切换时由 app.py 调用）。"""
        self._theme = theme.resolve(theme_name)
        self.setStyleSheet(theme.manager_qss(self._theme))
        self.setFont(QFont(theme.FONT_FAMILY, 9))
        theme.set_titlebar(self, theme.is_dark(self._theme))
        list_widget = getattr(self, "list", None)
        if list_widget is not None and list_widget.count() == 0:
            self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self.list.clear()
        groups = self.controller.groups_list()
        provider = QFileIconProvider()
        colors = theme.colors(self._theme)
        if not groups:
            item = QListWidgetItem("还没有分组。点左下角「新建分组」开始。")
            item.setSizeHint(QSize(0, 48))
            item.setForeground(QColor(theme.to_qcolor(colors["text_muted"])))
            self.list.addItem(item)
        for group in groups:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 62))
            self.list.addItem(item)

            row = QWidget()
            row.setObjectName("row")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(10, 6, 10, 6)
            layout.setSpacing(12)

            icon = provider.icon(QFileInfo(group.path))
            icon_label = QLabel(row)
            icon_label.setPixmap(icon.pixmap(QSize(36, 36)))
            icon_label.setFixedSize(40, 40)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(icon_label)

            texts = QVBoxLayout()
            texts.setSpacing(2)
            name = QLabel(group.name, row)
            name.setObjectName("name")
            meta = QLabel(f"{group.count} 个应用 · {group.path}", row)
            meta.setObjectName("meta")
            meta.setToolTip(group.path)
            texts.addWidget(name)
            texts.addWidget(meta)
            layout.addLayout(texts, 1)

            btn_open = QPushButton("打开", row)
            btn_open.clicked.connect(
                lambda _=False, g=group: self.controller.open_group(g)
            )
            layout.addWidget(btn_open)
            btn_rename = QPushButton("重命名", row)
            btn_rename.clicked.connect(lambda _=False, g=group: self._rename(g))
            layout.addWidget(btn_rename)
            btn_del = QPushButton("删除", row)
            btn_del.setObjectName("danger")
            btn_del.clicked.connect(lambda _=False, g=group: self._delete(g))
            layout.addWidget(btn_del)

            self.list.setItemWidget(item, row)

        self.chk_single.setChecked(self.controller.single_click_enabled())
        self.chk_start.setChecked(self.controller.autostart_enabled())
        self.lbl_status.setText(f"共 {len(groups)} 个分组")

    # ------------------------------------------------------------------
    def _rename(self, group) -> None:
        self.controller.rename_group_dialog(group, self)
        self.refresh()

    def _delete(self, group) -> None:
        self.controller.delete_group_dialog(group, self)
        self.refresh()

    # ------------------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt naming
        theme.set_titlebar(self, theme.is_dark(self._theme))
        self.refresh()
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent):  # noqa: N802 - Qt naming
        event.ignore()
        self.hide()
