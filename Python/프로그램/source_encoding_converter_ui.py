from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout

from source_encoding_converter import ConversionResult, convert_items, scan_folder, summary


class SourceEncodingConverterDialog(QDialog):
    def __init__(self, initial_root: str = "", parent=None) -> None:
        super().__init__(parent)
        self.items = []
        self.setWindowTitle("소스 인코딩 일괄 변환")
        self.resize(930, 620)
        layout = QVBoxLayout(self)
        notice = QLabel("UTF-8은 엄격 검증하고 BOM·CP949/EUC-KR만 안전하게 변환합니다. 불확실하거나 바이너리인 파일은 자동 변환하지 않습니다.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.root = QLineEdit(initial_root)
        root_button = QPushButton("폴더 선택…")
        root_button.clicked.connect(self._browse_root)
        root_row = QHBoxLayout(); root_row.addWidget(self.root); root_row.addWidget(root_button)
        form.addRow("대상 소스 폴더", root_row)
        self.mode = QComboBox(); self.mode.addItem("원본 위치 변환 + .bak 백업", "backup"); self.mode.addItem("별도 출력 폴더에 변환", "folder")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("변환 방식", self.mode)
        self.output = QLineEdit(); self.output.setPlaceholderText("별도 출력 폴더")
        output_button = QPushButton("출력 폴더…"); output_button.clicked.connect(self._browse_output)
        output_row = QHBoxLayout(); output_row.addWidget(self.output); output_row.addWidget(output_button)
        self.output_row = output_row
        form.addRow("출력 폴더", output_row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        scan = QPushButton("스캔 및 미리보기"); scan.clicked.connect(self._scan)
        self.convert = QPushButton("안전 변환 실행"); self.convert.setEnabled(False); self.convert.clicked.connect(self._convert)
        buttons.addWidget(scan); buttons.addWidget(self.convert); buttons.addStretch(1)
        layout.addLayout(buttons)
        self.result = QLabel("아직 스캔하지 않았습니다."); layout.addWidget(self.result)
        self.table = QTableWidget(0, 5); self.table.setHorizontalHeaderLabels(["파일", "판별 인코딩", "줄바꿈", "처리", "사유"]); self.table.horizontalHeader().setStretchLastSection(True); self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(300); self.log.setFixedHeight(100); layout.addWidget(self.log)
        self._mode_changed()

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "변환할 소스 폴더", self.root.text())
        if path: self.root.setText(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "변환 결과 폴더", self.output.text() or self.root.text())
        if path: self.output.setText(path)

    def _mode_changed(self) -> None:
        enabled = self.mode.currentData() == "folder"
        self.output.setEnabled(enabled)
        self.output_row.itemAt(1).widget().setEnabled(enabled)

    def _show_rows(self, rows) -> None:
        self.table.setRowCount(0)
        for row_data in rows:
            item = getattr(row_data, "item", row_data)
            status = getattr(row_data, "action", item.status)
            detail = getattr(row_data, "detail", item.detail)
            row = self.table.rowCount(); self.table.insertRow(row)
            for column, value in enumerate((str(item.relative_path), item.encoding, item.newline, status, detail)):
                cell = QTableWidgetItem(value); cell.setToolTip(value); self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()

    def _scan(self) -> None:
        try:
            self.items = scan_folder(self.root.text())
            self._show_rows(self.items); self.result.setText(summary(self.items)); self.log.appendPlainText("스캔 완료: 변환 가능 파일만 실행 대상입니다.")
            self.convert.setEnabled(any(item.status == "변환 가능" for item in self.items))
        except ValueError as error:
            QMessageBox.warning(self, "스캔 실패", str(error))

    def _convert(self) -> None:
        mode = self.mode.currentData(); output = self.output.text().strip()
        if mode == "folder" and not output:
            QMessageBox.warning(self, "출력 폴더 필요", "별도 출력 폴더를 지정하십시오."); return
        candidate = sum(item.status == "변환 가능" for item in self.items)
        target = ".bak 백업 후 원본 위치" if mode == "backup" else f"별도 폴더\n{Path(output)}"
        if QMessageBox.question(self, "변환 최종 확인", f"{candidate}개 파일을 UTF-8 무 BOM으로 변환합니다.\n방식: {target}\n불확실한 파일은 건너뜁니다.") != QMessageBox.Yes:
            return
        results = convert_items(self.items, mode, output)
        self._show_rows(results); self.result.setText(summary(results))
        self.log.appendPlainText("변환 완료\n" + "\n".join(f"{result.action}: {result.item.relative_path} {result.detail}" for result in results))
        QMessageBox.information(self, "변환 완료", summary(results))
