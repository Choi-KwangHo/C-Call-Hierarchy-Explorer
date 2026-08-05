from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from window_state import apply_dark_title_bar


GLOBAL_DARK_STYLE = """
QWidget { color:#DCE6EC; }
QDialog, QMessageBox, QFileDialog { background:#151A1F; color:#DCE6EC; }
QLabel { color:#DCE6EC; background:transparent; }
QLabel:disabled { color:#77848D; }
QPushButton { background:#293640; color:#F2F6F8; border:1px solid #4A5B67; border-radius:3px; padding:6px 12px; }
QPushButton:hover { background:#384955; border-color:#68808F; }
QPushButton:pressed { background:#17658F; }
QPushButton:disabled { background:#222A30; color:#71808A; border-color:#35414A; }
QLineEdit, QSpinBox, QComboBox { background:#0F151A; color:#E8EEF2; border:1px solid #465762; border-radius:3px; padding:5px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color:#1683C5; }
QComboBox QAbstractItemView { background:#11181E; color:#E8EEF2; selection-background-color:#1B6C9D; selection-color:#FFFFFF; border:1px solid #465762; }
QPlainTextEdit, QTextEdit { background:#0D1318; color:#DCE6EC; border:1px solid #34414A; selection-background-color:#1D668F; selection-color:#FFFFFF; }
QTreeWidget, QTableWidget, QListWidget { background:#10161B; alternate-background-color:#161E24; color:#DCE6EC; border:1px solid #34414A; outline:0; }
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected { background:rgba(24,126,185,135); color:#FFFFFF; }
QTreeWidget::item:hover, QTableWidget::item:hover, QListWidget::item:hover { background:#20303A; color:#FFFFFF; }
QHeaderView::section { background:#232D35; color:#EDF3F6; border:0; border-right:1px solid #3B4852; border-bottom:1px solid #3B4852; padding:6px; }
QCheckBox, QRadioButton { color:#E4EBEF; spacing:7px; }
QCheckBox:disabled, QRadioButton:disabled { color:#77848D; }
QTabWidget::pane { background:#11171C; border:1px solid #34414A; }
QTabBar::tab { background:#222C34; color:#BCC8D0; padding:7px 14px; }
QTabBar::tab:selected { background:#176C9D; color:#FFFFFF; }
QDialogButtonBox { background:transparent; }
QToolTip { background:#222D35; color:#F4F7F9; border:1px solid #586B77; padding:4px; }
QMenu { background:#171E24; color:#E4EBEF; border:1px solid #3C4953; }
QMenu::item:selected { background:#17648F; color:#FFFFFF; }
QProgressBar { background:#11171C; color:#FFFFFF; border:1px solid #3B4953; text-align:center; min-height:17px; }
QProgressBar::chunk { background:#1683C5; }
QScrollBar:vertical { background:#11171C; width:13px; }
QScrollBar::handle:vertical { background:#485A66; min-height:28px; border-radius:5px; margin:2px; }
QScrollBar:horizontal { background:#11171C; height:13px; }
QScrollBar::handle:horizontal { background:#485A66; min-width:28px; border-radius:5px; margin:2px; }
"""


class _DarkWindowFilter(QObject):
    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.Show, QEvent.Polish) and isinstance(watched, QWidget) and watched.isWindow():
            apply_dark_title_bar(watched)
        return False


def apply_application_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    colors = {
        QPalette.Window: "#151A1F", QPalette.WindowText: "#DCE6EC",
        QPalette.Base: "#0F151A", QPalette.AlternateBase: "#161E24",
        QPalette.ToolTipBase: "#222D35", QPalette.ToolTipText: "#F4F7F9",
        QPalette.Text: "#DCE6EC", QPalette.Button: "#293640",
        QPalette.ButtonText: "#F2F6F8", QPalette.BrightText: "#FFFFFF",
        QPalette.Highlight: "#176C9D", QPalette.HighlightedText: "#FFFFFF",
        QPalette.Link: "#62B7EA", QPalette.LinkVisited: "#A995E8",
        QPalette.PlaceholderText: "#7E8B94",
    }
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#77848D"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#77848D"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#77848D"))
    app.setPalette(palette)
    app.setStyleSheet(GLOBAL_DARK_STYLE)
    window_filter = _DarkWindowFilter(app)
    app.installEventFilter(window_filter)
    app._dark_window_filter = window_filter
