from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

from github_repository import GitHubRepositoryError, GitHubClient, RepositoryRequest, detect_iar_projects


class GitHubRepositoryDialog(QDialog):
    """Create-only/first-upload dialog; external mutation requires confirmation."""

    def __init__(self, current_root: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GitHub Repository 생성 및 IAR 업로드")
        self.resize(620, 360)
        self.dashboard = QLineEdit("https://github.com/orgs/Esol-Lab")
        self.name = QLineEdit()
        self.description = QLineEdit()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.root = QLineEdit(current_root)
        browse = QPushButton("폴더 선택…")
        browse.clicked.connect(self._choose_root)
        root_row = QHBoxLayout(); root_row.addWidget(self.root); root_row.addWidget(browse)
        self.readme = QCheckBox("README 생성")
        self.readme.setChecked(True)
        self.mode = QLabel("Repository만 생성")
        self.root.textChanged.connect(self._update_mode)
        form = QFormLayout()
        form.addRow("GitHub Dashboard", self.dashboard)
        form.addRow("Repository name", self.name)
        form.addRow("Description", self.description)
        form.addRow("GitHub Token", self.token)
        form.addRow("로컬 IAR 코드 폴더", root_row)
        form.addRow("생성 모드", self.mode)
        form.addRow("옵션", self.readme)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Repository 생성")
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addWidget(QLabel("Repository 생성 후, 폴더가 지정된 경우 최초 코드 업로드를 선택할 수 있습니다.")); layout.addLayout(form); layout.addWidget(buttons)

    def _choose_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "IAR 프로젝트 폴더 선택", self.root.text())
        if path: self.root.setText(path)

    def _update_mode(self, value: str) -> None:
        if not value: self.mode.setText("Repository만 생성")
        elif detect_iar_projects(value): self.mode.setText("IAR 프로젝트 포함 생성")
        else: self.mode.setText("폴더 지정, IAR 프로젝트 없음")

    def _create(self) -> None:
        try:
            request = RepositoryRequest(self.dashboard.text(), self.name.text(), self.description.text(), include_readme=self.readme.isChecked(), local_path=self.root.text())
            request.validate()
            if not self.token.text().strip(): raise GitHubRepositoryError("GitHub Token을 입력하십시오.")
            answer = QMessageBox.question(self, "최종 확인", f"Private Repository '{request.owner}/{request.name}'를 생성하시겠습니까?\n모드: {request.mode}")
            if answer != QMessageBox.Yes: return
            result = GitHubClient(self.token.text()).create_repository(request)
            if request.local_path and QMessageBox.question(self, "최초 업로드", "생성된 Repository에 현재 폴더의 파일을 최초 업로드하시겠습니까?") == QMessageBox.Yes:
                GitHubClient(self.token.text()).upload_project(request)
            QMessageBox.information(self, "완료", f"Repository 생성 완료\n{result.get('html_url', '')}")
            self.accept()
        except GitHubRepositoryError as error:
            QMessageBox.warning(self, "Repository 생성 실패", str(error))
