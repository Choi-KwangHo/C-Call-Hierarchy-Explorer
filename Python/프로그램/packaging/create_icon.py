from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication


def main() -> None:
    QApplication.instance() or QApplication([])
    target = Path(__file__).resolve().parent.parent / "assets" / "CallHierarchyExplorer.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    image = QImage(256, 256, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#173A56"))
    painter.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)

    painter.setPen(QPen(QColor("#5CC8FF"), 12, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawLine(150, 72, 150, 186)
    painter.drawLine(150, 100, 210, 100)
    painter.drawLine(150, 156, 210, 156)
    painter.setBrush(QColor("#5CC8FF"))
    for x, y in ((150, 72), (210, 100), (210, 156), (150, 186)):
        painter.drawEllipse(x - 13, y - 13, 26, 26)

    font = QFont("Segoe UI", 104, QFont.Bold)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(QRectF(22, 48, 122, 150), Qt.AlignCenter, "C")
    painter.end()

    if not image.save(str(target), "ICO"):
        raise RuntimeError(f"Could not create icon: {target}")
    print(target)


if __name__ == "__main__":
    main()
