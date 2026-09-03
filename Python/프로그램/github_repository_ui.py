from __future__ import annotations

import base64
import ctypes
import os
from datetime import date

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget
from github_repository import GitHubClient, GitHubRepositoryError, RepositoryRequest, detect_iar_projects

class _Blob(ctypes.Structure):
    _fields_=[("cbData",ctypes.c_uint), ("pbData",ctypes.POINTER(ctypes.c_byte))]
def _protect(value: str) -> str:
    if not value or os.name != "nt": return ""
    raw=value.encode("utf-8"); source=ctypes.create_string_buffer(raw); incoming=_Blob(len(raw),ctypes.cast(source,ctypes.POINTER(ctypes.c_byte))); outgoing=_Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(incoming),"EmbedForge GitHub Token",None,None,None,0,ctypes.byref(outgoing)): return ""
    try: return base64.b64encode(ctypes.string_at(outgoing.pbData,outgoing.cbData)).decode()
    finally: ctypes.windll.kernel32.LocalFree(outgoing.pbData)
def _unprotect(value: str) -> str:
    if not value or os.name != "nt": return ""
    try: raw=base64.b64decode(value.encode())
    except ValueError: return ""
    source=ctypes.create_string_buffer(raw); incoming=_Blob(len(raw),ctypes.cast(source,ctypes.POINTER(ctypes.c_byte))); outgoing=_Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(incoming),None,None,None,None,0,ctypes.byref(outgoing)): return ""
    try: return ctypes.string_at(outgoing.pbData,outgoing.cbData).decode("utf-8")
    finally: ctypes.windll.kernel32.LocalFree(outgoing.pbData)

class _Signals(QObject):
    ready = Signal(object); progress = Signal(int, int, str); error = Signal(str); done = Signal()
class _Worker(QRunnable):
    def __init__(self, action): super().__init__(); self.action=action; self.signals=_Signals()
    @Slot()
    def run(self):
        try: self.signals.ready.emit(self.action(self.signals.progress.emit))
        except Exception as error: self.signals.error.emit(str(error))
        finally: self.signals.done.emit()

class _StageTimeline(QWidget):
    labels=("Repository 생성","파일 검사","커밋 준비","파일 전송","커밋 검증","진행 마무리")
    def __init__(self): super().__init__(); self.stage=0; self.failed=-1; self.setMinimumHeight(72)
    def set_stage(self,stage): self.stage=stage; self.failed=-1; self.update()
    def set_failed(self,stage): self.failed=stage; self.update()
    def paintEvent(self,_):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing); count=len(self.labels); left,right=35,self.width()-35; y=42; step=(right-left)/(count-1)
        for index,label in enumerate(self.labels):
            x=left+step*index; color=QColor("#C0392B") if index==self.failed else QColor("#2E8B57") if index<self.stage else QColor("#238CC5") if index==self.stage else QColor("#52616D")
            if index: painter.setPen(QPen(QColor("#2E8B57") if index<=self.stage else QColor("#52616D"),2)); painter.drawLine(int(x-step+9),y,int(x-9),y)
            painter.setBrush(color); painter.setPen(QPen(QColor("#EAF2F6"),2)); painter.drawEllipse(int(x-8),y-8,16,16)
            painter.setPen(QColor("#F0F5F7")); painter.drawText(int(x-step/2),4,int(step),24,Qt.AlignCenter,label)

class GitHubRepositoryDialog(QDialog):
    """Background GitHub creation and first-upload manager."""
    def __init__(self, current_root: str = "", parent=None, settings: QSettings | None = None):
        super().__init__(parent); self.settings=settings or QSettings(); self.pool=QThreadPool(self); self.pool.setMaxThreadCount(1); self.worker=None; self.selected=None
        self.setWindowTitle("GitHub Repository 생성 및 IAR 업로드"); self.resize(1050, 610)
        outer=QVBoxLayout(self); outer.addWidget(QLabel("Dashboard별 저장소를 조회합니다. ‘초기 업로드 필요’ 저장소만 최초 코드 업로드를 실행할 수 있습니다."))
        self.timeline=_StageTimeline(); outer.addWidget(self.timeline); self._stage(0)
        splitter=QSplitter(Qt.Horizontal); outer.addWidget(splitter, 1); left=QWidget(); form=QFormLayout(left); splitter.addWidget(left)
        self.dashboard=QComboBox(); self.dashboard.setEditable(True); self.dashboard.addItems(self._history("github/dashboardHistory", "https://github.com/Choi-KwangHo")); self.dashboard.currentTextChanged.connect(self._dashboard_changed)
        self.name=QLineEdit(f"IAR-Firmware-{date.today():%Y%m%d}"); self.description=QLineEdit("EmbedForge에서 생성한 IAR 펌웨어 소스 저장소")
        self.token=QLineEdit(); self.token.setEchoMode(QLineEdit.Password); self.token.setPlaceholderText("Dashboard 선택 시 저장 토큰을 마스킹하여 표시")
        self.root=QComboBox(); self.root.setEditable(True); self.root.addItems(self._history("github/localFolderHistory", current_root)); self.root.currentTextChanged.connect(self._update_mode)
        browse=QPushButton("폴더 선택…"); browse.clicked.connect(self._choose_root); row=QHBoxLayout(); row.addWidget(self.root); row.addWidget(browse)
        self.mode=QLabel("Repository만 생성"); self.readme=QCheckBox("README 생성"); self.readme.setChecked(True)
        for label, widget in (("GitHub Dashboard",self.dashboard),("Repository name",self.name),("Description",self.description),("GitHub Token",self.token),("로컬 IAR 코드 폴더",row),("생성 모드",self.mode),("옵션",self.readme)): form.addRow(label,widget)
        right=QWidget(); right_layout=QVBoxLayout(right); splitter.addWidget(right); splitter.setSizes([430,620]); right_layout.addWidget(QLabel("Repository 목록 · 빈 저장소는 ‘초기 업로드 필요’"))
        self.tree=QTreeWidget(); self.tree.setHeaderLabels(["Repository","상태","공개"]); self.tree.itemSelectionChanged.connect(self._selected_changed); self.tree.setContextMenuPolicy(Qt.CustomContextMenu); self.tree.customContextMenuRequested.connect(self._context_menu); right_layout.addWidget(self.tree,1)
        self.status=QLabel("Dashboard를 입력한 뒤 목록 새로고침을 누르십시오."); self.progress=QProgressBar(); self.progress.hide(); right_layout.addWidget(self.status); right_layout.addWidget(self.progress)
        buttons=QHBoxLayout(); self.refresh=QPushButton("목록 새로고침"); self.refresh.clicked.connect(self._refresh); self.create=QPushButton("Repository 생성"); self.create.clicked.connect(self._create); self.upload=QPushButton("선택 저장소에 최초 커밋"); self.upload.setEnabled(False); self.upload.clicked.connect(self._upload); close=QPushButton("닫기"); close.clicked.connect(self.reject)
        for button in (self.refresh,self.create,self.upload): buttons.addWidget(button)
        buttons.addStretch(1); buttons.addWidget(close); outer.addLayout(buttons); self._dashboard_changed(self.dashboard.currentText()); self._update_mode(self.root.currentText())
    def _history(self,key,default):
        value=self.settings.value(key,[]); values=[value] if isinstance(value,str) else list(value or []); return list(dict.fromkeys(([default] if default else [])+[str(x) for x in values if x]))
    def _remember(self,key,value):
        if value: self.settings.setValue(key,([value]+[x for x in self._history(key,"") if x!=value])[:12])
    def _token_key(self): return "github/token/"+base64.urlsafe_b64encode(self.dashboard.currentText().strip().encode()).decode()
    def _dashboard_changed(self,_): self.token.setText(_unprotect(self.settings.value(self._token_key(),"")))
    def _remember_token(self):
        protected=_protect(self.token.text())
        if protected: self.settings.setValue(self._token_key(),protected)
    def _choose_root(self):
        path=QFileDialog.getExistingDirectory(self,"IAR 프로젝트 폴더 선택",self.root.currentText())
        if path: self.root.setEditText(path)
    def _update_mode(self,value): self.mode.setText("Repository만 생성" if not value else "IAR 프로젝트 포함 생성" if detect_iar_projects(value) else "폴더 지정, IAR 프로젝트 없음")
    def _request(self): return RepositoryRequest(self.dashboard.currentText(),self.name.text(),self.description.text(),include_readme=self.readme.isChecked(),local_path=self.root.currentText())
    def _client(self): return GitHubClient(self.token.text())
    def _start(self,work,label,stage=0):
        for button in (self.refresh,self.create,self.upload): button.setEnabled(False)
        self.operation_failed=False; self.active_stage=stage; self._stage(stage); self.progress.setRange(0,0); self.progress.show(); self.status.setText(label); self.worker=_Worker(work); self.worker.signals.ready.connect(self._ready); self.worker.signals.progress.connect(self._progress); self.worker.signals.error.connect(self._failed); self.worker.signals.done.connect(self._done); self.pool.start(self.worker)
    def _stage(self, active): self.active_stage=active; self.timeline.set_stage(active)
    def _failed(self,error):
        self.operation_failed=True
        self.timeline.set_failed(self.active_stage)
        self.status.setText(f"실패 단계: {self.timeline.labels[self.active_stage]} · {error}")
        QMessageBox.warning(self,"GitHub 작업 실패",f"실패 단계: {self.timeline.labels[self.active_stage]}\n\n{error}")
    def _done(self):
        self.refresh.setEnabled(True); self.create.setEnabled(True); self.progress.hide()
        if not self.operation_failed: self._stage(5)
        self._selected_changed()
    def _progress(self,current,total,name):
        self._stage(2 if current == 0 else 3); self.progress.setRange(0,max(1,total)); self.progress.setValue(current); prefix="최초 커밋 준비" if current == 0 else f"파일 전송 {current}/{total}"; self.status.setText(f"{prefix}: {name}")
    def _ready(self,result):
        if isinstance(result,list) and (not result or hasattr(result[0], "is_empty")):
            self.tree.clear()
            for repo in result:
                item=QTreeWidgetItem([repo.name,"초기 업로드 필요" if repo.is_empty else "커밋 있음","Private" if repo.private else "Public"]); item.setData(0,Qt.UserRole,repo); self.tree.addTopLevelItem(item)
            self.status.setText(f"저장소 {len(result)}개 조회 완료")
        else: self._stage(4); self.status.setText("커밋 검증 완료 · 진행 마무리 중")
    def _refresh(self):
        try:
            request=self._request(); request.owner; self._remember("github/dashboardHistory",request.dashboard_url); self._remember_token(); self._start(lambda _:self._client().list_repositories(request),"Repository 목록 조회 중…")
        except GitHubRepositoryError as error: QMessageBox.warning(self,"입력 오류",str(error))
    def _create(self):
        try:
            request=self._request(); request.validate(); self._stage(0)
            if not self.token.text().strip(): raise GitHubRepositoryError("GitHub Token을 입력하십시오.")
            if QMessageBox.question(self,"생성 확인",f"{request.owner}/{request.name} 저장소를 생성합니까?")!=QMessageBox.Yes:return
            self._remember("github/dashboardHistory",request.dashboard_url); self._remember("github/localFolderHistory",request.local_path); self._remember_token(); self._start(lambda _:self._client().create_repository(request),"Repository 생성 중…",0)
        except GitHubRepositoryError as error: QMessageBox.warning(self,"입력 오류",str(error))
    def _selected_changed(self):
        rows=self.tree.selectedItems(); self.selected=rows[0].data(0,Qt.UserRole) if rows else None; self.upload.setEnabled(bool(self.selected and self.selected.is_empty and self.root.currentText().strip()))
    def _context_menu(self, point):
        menu=QMenu(self); create=menu.addAction("새 Repository 생성"); create.triggered.connect(self._new_repository); menu.exec(self.tree.viewport().mapToGlobal(point))
    def _new_repository(self):
        self.selected=None; self.tree.clearSelection(); self.name.setFocus(); self.name.selectAll(); self.status.setText("새 Repository 이름과 설명을 입력한 뒤 생성하십시오.")
    def _upload(self):
        if not self.selected:return
        request=self._request(); request.name=self.selected.name
        if QMessageBox.question(self,"최초 커밋 확인",f"{self.selected.full_name}에 제외 규칙을 통과한 파일을 업로드합니까?")!=QMessageBox.Yes:return
        self._remember("github/localFolderHistory",request.local_path); self._start(lambda progress:self._client().upload_project(request,progress=progress),"파일 검사 중…",1)
