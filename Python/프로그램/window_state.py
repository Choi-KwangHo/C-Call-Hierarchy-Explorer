from __future__ import annotations

import json

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication


def screen_topology() -> str:
    """Return a stable signature for monitor count, placement and resolution."""
    screens = []
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        screens.append({
            "name": screen.name(),
            "x": geometry.x(),
            "y": geometry.y(),
            "width": geometry.width(),
            "height": geometry.height(),
            "dpr": round(float(screen.devicePixelRatio()), 3),
        })
    screens.sort(key=lambda value: (value["name"], value["x"], value["y"]))
    return json.dumps(screens, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _center_on_primary(widget, default_size: tuple[int, int]) -> None:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        widget.resize(*default_size)
        return
    available = screen.availableGeometry()
    width = min(default_size[0], available.width())
    height = min(default_size[1], available.height())
    x = available.x() + (available.width() - width) // 2
    y = available.y() + (available.height() - height) // 2
    widget.setGeometry(x, y, width, height)


def restore_window_state(widget, settings, key: str, default_size: tuple[int, int]) -> str:
    """Restore normal geometry only when the monitor topology is unchanged."""
    current_topology = screen_topology()
    stored_topology = str(settings.value(f"{key}/screenTopology", "") or "")
    if stored_topology != current_topology:
        _center_on_primary(widget, default_size)
        return "normal"
    try:
        raw = settings.value(f"{key}/normalGeometry", "")
        values = json.loads(str(raw))
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError
        rect = QRect(*(int(value) for value in values))
        if rect.width() < 320 or rect.height() < 240:
            raise ValueError
        visible = any(
            screen.availableGeometry().intersected(rect).width() >= 80
            and screen.availableGeometry().intersected(rect).height() >= 80
            for screen in QGuiApplication.screens()
        )
        if not visible:
            raise ValueError
        widget.setGeometry(rect)
    except (TypeError, ValueError, json.JSONDecodeError):
        _center_on_primary(widget, default_size)
        return "normal"
    mode = str(settings.value(f"{key}/mode", "normal") or "normal")
    return mode if mode in {"normal", "maximized", "fullscreen"} else "normal"


def save_window_state(widget, settings, key: str) -> None:
    normal = widget.normalGeometry() if (widget.isFullScreen() or widget.isMaximized()) else widget.geometry()
    mode = "fullscreen" if widget.isFullScreen() else "maximized" if widget.isMaximized() else "normal"
    settings.setValue(f"{key}/screenTopology", screen_topology())
    settings.setValue(
        f"{key}/normalGeometry",
        json.dumps([normal.x(), normal.y(), normal.width(), normal.height()]),
    )
    settings.setValue(f"{key}/mode", mode)
    settings.sync()
