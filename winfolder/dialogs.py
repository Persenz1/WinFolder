# -*- coding: utf-8 -*-
"""主题化的自定义对话框。

为什么要自己写
==============
* ``QInputDialog`` 在 Windows 上使用原生控件，深色 palette 下会出现白底白字；
* ``QMessageBox`` 默认按钮是英文 “OK / Yes / No”，且不跟随主题。

这里提供两套跟随 :mod:`winfolder.theme` 的对话框：

* :class:`TextInputDialog` / :func:`get_text` —— 新建分组、重命名分组；
  支持占位符、确定/取消中文按钮、Enter 确认、Esc 取消、空名禁用确认、
  父窗口居中。
* :class:`MessageDialog` / :func:`show_message` / :func:`confirm` —— 提示、
  警告与删除确认，按钮中文、深浅色都保证文字对比度。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:  # 包内导入
    from . import theme
except ImportError:  # 作为独立脚本 / 单文件运行时的兜底
    import theme  # type: ignore[no-redef]

log = logging.getLogger(__name__)

_OK_TEXT = "确定"
_CANCEL_TEXT = "取消"
_INPUT_MAX_LENGTH = 80


def _apply_dialog_theme(dialog: QDialog) -> str:
    """给对话框套用当前主题样式，并返回主题名。"""
    name = theme.current_theme()
    dialog.setStyleSheet(theme.dialog_qss(name))
    dialog.setFont(QFont(theme.FONT_FAMILY, 9))
    return name


def _center_on_parent(dialog: QDialog, parent: Optional[QWidget]) -> None:
    """把对话框放到父窗口（或主屏）中央。"""
    try:
        dialog.adjustSize()
        if parent is not None and parent.isVisible():
            frame = parent.frameGeometry()
            x = frame.center().x() - dialog.width() // 2
            y = frame.center().y() - dialog.height() // 2
        else:
            screen = dialog.screen() or dialog.windowHandle().screen() if dialog.windowHandle() else None
            if screen is None:
                from PySide6.QtGui import QGuiApplication

                screen = QGuiApplication.primaryScreen()
            available = screen.availableGeometry() if screen else None
            if available is None:
                return
            x = available.center().x() - dialog.width() // 2
            y = available.center().y() - dialog.height() // 2
        dialog.move(x, y)
    except Exception as exc:  # noqa: BLE001 - 居中失败不影响功能
        log.debug("对话框居中失败: %s", exc)


def _button(text: str, object_name: str, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumWidth(84)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def _dialog_titlebar(dialog: QDialog, theme_name: str) -> None:
    try:
        theme.set_titlebar(dialog, theme.is_dark(theme_name))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 文本输入框（替代 QInputDialog.getText）
# ---------------------------------------------------------------------------
class TextInputDialog(QDialog):
    """主题化的单行文本输入对话框。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title: str = "",
        label: str = "",
        placeholder: str = "",
        initial: str = "",
        ok_text: str = _OK_TEXT,
        cancel_text: str = _CANCEL_TEXT,
    ):
        super().__init__(parent)
        self.setObjectName("wfDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(440)
        self._theme = _apply_dialog_theme(self)
        self._window_title = title

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        root.setSpacing(10)

        title_label = QLabel(title, self)
        title_label.setObjectName("wfDialogTitle")
        root.addWidget(title_label)

        if label:
            label_widget = QLabel(label, self)
            label_widget.setObjectName("wfDialogLabel")
            label_widget.setWordWrap(True)
            root.addWidget(label_widget)

        self.edit = QLineEdit(self)
        self.edit.setObjectName("wfDialogInput")
        self.edit.setPlaceholderText(placeholder or "输入名称")
        self.edit.setText(initial or "")
        self.edit.setMaxLength(_INPUT_MAX_LENGTH)
        self.edit.setClearButtonEnabled(True)
        root.addWidget(self.edit)

        root.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.cancel_button = _button(cancel_text, "wfDialogCancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.ok_button = _button(ok_text, "wfDialogOk", self)
        self.ok_button.setDefault(True)
        self.ok_button.setAutoDefault(True)
        self.ok_button.clicked.connect(self._accept_if_valid)
        buttons.addWidget(self.ok_button)
        root.addLayout(buttons)

        self.edit.textChanged.connect(self._update_ok_state)
        self.edit.returnPressed.connect(self._accept_if_valid)
        self._update_ok_state(self.edit.text())
        self.edit.setFocus()
        _center_on_parent(self, parent)

    # -- 值 / 状态 -----------------------------------------------------
    def value(self) -> str:
        return self.edit.text().strip()

    def _update_ok_state(self, text: str) -> None:
        self.ok_button.setEnabled(bool((text or "").strip()))

    def _accept_if_valid(self) -> None:
        if self.value():
            self.accept()

    # -- 生命周期 ------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        _dialog_titlebar(self, self._theme)
        self.edit.setFocus()
        self.edit.selectAll()

    @staticmethod
    def get_text(
        parent: Optional[QWidget] = None,
        title: str = "",
        label: str = "",
        placeholder: str = "",
        initial: str = "",
        ok_text: str = _OK_TEXT,
        cancel_text: str = _CANCEL_TEXT,
    ) -> tuple[str, bool]:
        """替代 ``QInputDialog.getText``，返回 ``(文本, 是否确定)``。"""
        dialog = TextInputDialog(
            parent=parent,
            title=title,
            label=label,
            placeholder=placeholder,
            initial=initial,
            ok_text=ok_text,
            cancel_text=cancel_text,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.value(), accepted


# ---------------------------------------------------------------------------
# 提示 / 确认对话框（替代 QMessageBox）
# ---------------------------------------------------------------------------
_KIND_STYLE = {
    "info": ("ⓘ", "accent"),
    "warning": ("⚠", "warning"),
    "error": ("✕", "danger"),
    "question": ("?", "accent"),
}
_WARNING_COLOR = {"light": "#b26a00", "dark": "#ffc66d"}


class MessageDialog(QDialog):
    """主题化的提示 / 确认对话框（支持富文本，按钮中文）。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title: str = "",
        text: str = "",
        kind: str = "info",
        ok_text: str = "知道了",
        cancel_text: Optional[str] = None,
        danger: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("wfDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(460)
        self._theme = _apply_dialog_theme(self)
        self._window_title = title

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        root.setSpacing(12)

        title_label = QLabel(title, self)
        title_label.setObjectName("wfDialogTitle")
        root.addWidget(title_label)

        body = QHBoxLayout()
        body.setSpacing(14)
        glyph, color_key = _KIND_STYLE.get(kind, _KIND_STYLE["info"])
        icon = QLabel(glyph, self)
        icon.setObjectName("wfDialogIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        icon.setFixedWidth(30)
        icon_font = QFont(theme.FONT_FAMILY, 17)
        icon_font.setBold(True)
        icon.setFont(icon_font)
        colors = theme.colors(self._theme)
        if color_key == "warning":
            color = _WARNING_COLOR["dark" if colors["is_dark"] else "light"]
        else:
            color = colors[color_key]
        icon.setStyleSheet(f"color: {color}; background: transparent;")
        body.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        self.text_label = QLabel(self)
        self.text_label.setObjectName("wfDialogText")
        self.text_label.setWordWrap(True)
        self.text_label.setTextFormat(
            Qt.TextFormat.RichText if "<" in (text or "") else Qt.TextFormat.PlainText
        )
        self.text_label.setText(text or "")
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.text_label.setOpenExternalLinks(False)
        body.addWidget(self.text_label, 1)
        root.addLayout(body)

        root.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.cancel_button: Optional[QPushButton] = None
        if cancel_text is not None:
            self.cancel_button = _button(cancel_text, "wfDialogCancel", self)
            self.cancel_button.clicked.connect(self.reject)
            buttons.addWidget(self.cancel_button)
        self.ok_button = _button(
            ok_text, "wfDialogOkDanger" if danger else "wfDialogOk", self
        )
        self.ok_button.setDefault(True)
        self.ok_button.setAutoDefault(True)
        self.ok_button.clicked.connect(self.accept)
        buttons.addWidget(self.ok_button)
        root.addLayout(buttons)

        self._center_on_parent(parent)

    # -- 生命周期 ------------------------------------------------------
    def _center_on_parent(self, parent: Optional[QWidget]) -> None:
        _center_on_parent(self, parent)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        _dialog_titlebar(self, self._theme)

    @staticmethod
    def show_message(
        parent: Optional[QWidget] = None,
        title: str = "",
        text: str = "",
        kind: str = "info",
        ok_text: str = "知道了",
    ) -> None:
        dialog = MessageDialog(
            parent=parent, title=title, text=text, kind=kind, ok_text=ok_text
        )
        dialog.exec()

    @staticmethod
    def confirm(
        parent: Optional[QWidget] = None,
        title: str = "",
        text: str = "",
        ok_text: str = "确定",
        cancel_text: str = _CANCEL_TEXT,
        danger: bool = False,
    ) -> bool:
        dialog = MessageDialog(
            parent=parent,
            title=title,
            text=text,
            kind="warning" if danger else "question",
            ok_text=ok_text,
            cancel_text=cancel_text,
            danger=danger,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------
def get_text(
    parent: Optional[QWidget] = None,
    title: str = "",
    label: str = "",
    placeholder: str = "",
    initial: str = "",
    ok_text: str = _OK_TEXT,
    cancel_text: str = _CANCEL_TEXT,
) -> tuple[str, bool]:
    """``QInputDialog.getText`` 的主题化替代。"""
    return TextInputDialog.get_text(
        parent=parent,
        title=title,
        label=label,
        placeholder=placeholder,
        initial=initial,
        ok_text=ok_text,
        cancel_text=cancel_text,
    )


def show_message(
    parent: Optional[QWidget] = None,
    title: str = "",
    text: str = "",
    kind: str = "info",
    ok_text: str = "知道了",
) -> None:
    """主题化提示框（替代 ``QMessageBox.information/warning``）。"""
    MessageDialog.show_message(parent=parent, title=title, text=text, kind=kind, ok_text=ok_text)


def confirm(
    parent: Optional[QWidget] = None,
    title: str = "",
    text: str = "",
    ok_text: str = "确定",
    cancel_text: str = _CANCEL_TEXT,
    danger: bool = False,
) -> bool:
    """主题化确认框（替代 ``QMessageBox.question``），返回是否确认。"""
    return MessageDialog.confirm(
        parent=parent,
        title=title,
        text=text,
        ok_text=ok_text,
        cancel_text=cancel_text,
        danger=danger,
    )
