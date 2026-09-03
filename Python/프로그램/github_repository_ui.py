from __future__ import annotations

import base64
import ctypes
import os
import threading
from datetime import date

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget
from github_repository import GitHubClient, GitHubRepositoryError, GitHubUploadCancelled, RepositoryRequest, detect_iar_projects

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
    ready = Signal(object); progress = Signal(int, int, str); error = Signal(str); cancelled = Signal(str); done = Signal()
class _Worker(QRunnable):
    def __init__(self, action, cancel_event): super().__init__(); self.action=action; self.cancel_event=cancel_event; self.signals=_Signals()
    @Slot()
    def run(self):
        try: self.signals.ready.emit(self.action(self.signals.progress.emit, self.cancel_event))
        except GitHubUploadCancelled as error: self.signals.cancelled.emit(str(error))
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
            font=QFont(painter.font()); font.setPointSize(9); painter.setFont(font)
            label_width=max(96,int(step)-8); label_left=max(0,min(self.width()-label_width,int(x-label_width/2)))
            painter.setPen(QColor("#F0F5F7")); painter.drawText(label_left,4,label_width,24,Qt.AlignCenter,label)

class GitHubRepositoryDialog(QDialog):
    """Background GitHub creation and first-upload manager."""
    def __init__(self, current_root: str = "", parent=None, settings: QSettings | None = None):
        super().__init__(parent); self.settings=settings or QSettings(); self.pool=QThreadPool(self); self.pool.setMaxThreadCount(1); self.worker=None; self.selected=None; self.cancel_event=None; self.close_when_stopped=False; self.cancelled_operation=False
        self.setWindowTitle("GitHub Repository 생성 및 IAR 업로드"); self.resize(1280, 660); self.setMinimumWidth(1160)
        outer=QVBoxLayout(self); outer.addWidget(QLabel("Dashboard별 저장소를 조회합니다. ‘초기 업로드 필요’ 저장소만 최초 코드 업로드를 실행할 수 있습니다."))
        self.timeline=_StageTimeline(); outer.addWidget(self.timeline); self._stage(0)
        splitter=QSplitter(Qt.Horizontal); outer.addWidget(splitter, 1); left=QWidget(); left.setMinimumWidth(500); form=QFormLayout(left); form.setLabelAlignment(Qt.AlignRight); form.setHorizontalSpacing(12); splitter.addWidget(left)
        self.dashboard=QComboBox(); self.dashboard.setEditable(True); self.dashboard.setMinimumWidth(355); self.dashboard.addItems(self._history("github/dashboardHistory", "https://github.com/Choi-KwangHo")); self.dashboard.currentTextChanged.connect(self._dashboard_changed); self.dashboard.activated.connect(lambda _index:self._auto_refresh())
        self.name=QLineEdit(f"IAR-Firmware-{date.today():%Y%m%d}"); self.description=QLineEdit("EmbedForge에서 생성한 IAR 펌웨어 소스 저장소")
        self.token=QLineEdit(); self.token.setEchoMode(QLineEdit.Password); self.token.setPlaceholderText("Dashboard 선택 시 저장 토큰을 마스킹하여 표시")
        self.root=QComboBox(); self.root.setEditable(True); self.root.setMinimumWidth(355); self.root.addItems(self._history("github/localFolderHistory", current_root)); self.root.currentTextChanged.connect(self._update_mode)
        browse=QPushButton("폴더 선택…"); browse.clicked.connect(self._choose_root); row=QHBoxLayout(); row.addWidget(self.root); row.addWidget(browse)
        self.mode=QLabel("Repository만 생성"); self.readme=QCheckBox("README 생성"); self.readme.setChecked(True)
        for label, widget in (("GitHub Dashboard",self.dashboard),("Repository name",self.name),("Description",self.description),("GitHub Token",self.token),("로컬 IAR 코드 폴더",row),("생성 모드",self.mode),("옵션",self.readme)): form.addRow(label,widget)
        right=QWidget(); right.setMinimumWidth(610); right_layout=QVBoxLayout(right); splitter.addWidget(right); splitter.setSizes([520,760]); right_layout.addWidget(QLabel("Repository 목록 · 빈 저장소는 ‘초기 업로드 필요’"))
        self.tree=QTreeWidget(); self.tree.setHeaderLabels(["Repository","상태","공개"]); self.tree.setColumnWidth(0,330); self.tree.setColumnWidth(1,190); self.tree.itemSelectionChanged.connect(self._selected_changed); self.tree.setContextMenuPolicy(Qt.CustomContextMenu); self.tree.customContextMenuRequested.connect(self._context_menu); right_layout.addWidget(self.tree,1)
        self.status=QLabel("Dashboard를 입력한 뒤 목록 새로고침을 누르십시오."); self.progress=QProgressBar(); self.progress.hide(); right_layout.addWidget(self.status); right_layout.addWidget(self.progress)
        buttons=QHBoxLayout(); self.refresh=QPushButton("목록 새로고침"); self.refresh.clicked.connect(self._refresh); self.new_mode=QPushButton("새 Repository 모드"); self.new_mode.clicked.connect(self._new_repository); self.create=QPushButton("Repository 생성"); self.create.clicked.connect(self._create); self.upload=QPushButton("선택 저장소에 코드 업로드 / 복구"); self.upload.setEnabled(False); self.upload.clicked.connect(self._upload); close=QPushButton("닫기"); close.clicked.connect(self.close)
        for button in (self.refresh,self.new_mode,self.create,self.upload): buttons.addWidget(button)
        buttons.addStretch(1); buttons.addWidget(close); outer.addLayout(buttons); self._dashboard_changed(self.dashboard.currentText()); self._update_mode(self.root.currentText())
    def _history(self,key,default):
        value=self.settings.value(key,[]); values=[value] if isinstance(value,str) else list(value or []); return list(dict.fromkeys(([default] if default else [])+[str(x) for x in values if x]))
    def _remember(self,key,value):
        if value: self.settings.setValue(key,([value]+[x for x in self._history(key,"") if x!=value])[:12])
    def _token_key(self): return "github/token/"+base64.urlsafe_b64encode(self.dashboard.currentText().strip().encode()).decode()
    def _dashboard_changed(self,_): self.token.setText(_unprotect(self.settings.value(self._token_key(),"")))
    def _auto_refresh(self):
        if self.token.text().strip() and self.worker is None: self._refresh()
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
        self.operation_failed=False; self.cancelled_operation=False; self.active_stage=stage; self._stage(stage); self.progress.setFormat("%p%"); self.progress.setRange(0,0); self.progress.show(); self.status.setText(label); self.cancel_event=threading.Event(); self.worker=_Worker(work,self.cancel_event); self.worker.signals.ready.connect(self._ready); self.worker.signals.progress.connect(self._progress); self.worker.signals.error.connect(self._failed); self.worker.signals.cancelled.connect(self._cancelled); self.worker.signals.done.connect(self._done); self.pool.start(self.worker)
    def _stage(self, active): self.active_stage=active; self.timeline.set_stage(active)
    def _failed(self,error):
        self.operation_failed=True
        self.timeline.set_failed(self.active_stage)
        self.status.setText(f"실패 단계: {self.timeline.labels[self.active_stage]} · {error}")
        QMessageBox.warning(self,"GitHub 작업 실패",f"실패 단계: {self.timeline.labels[self.active_stage]}\n\n{error}")
    def _cancelled(self, message):
        self.cancelled_operation=True
        self.status.setText(f"중단 완료 · {message}")
    def _done(self):
        self.worker=None; self.cancel_event=None; self.refresh.setEnabled(True); self.progress.hide()
        if not self.operation_failed and not self.cancelled_operation: self._stage(5)
        self._selected_changed()
        if self.close_when_stopped:
            self.close_when_stopped=False; self.close()
    def _progress(self,current,total,name):
        if current < 0:
            stage=min(5, abs(current)); self._stage(stage)
            self.progress.setRange(0, 6); self.progress.setValue(stage)
            self.status.setText(name.replace(f"[STAGE:{stage}] ", ""))
            return
        self._stage(3); self.progress.setRange(0,max(1,total)); self.progress.setValue(current); self.status.setText(f"파일 전송 {current}/{total}: {name}")
    def _ready(self,result):
        if isinstance(result,list) and (not result or hasattr(result[0], "is_empty")):
            self.tree.clear()
            for repo in result:
                item=QTreeWidgetItem([repo.name,"초기 업로드 필요" if repo.is_empty else "커밋 있음","Private" if repo.private else "Public"]); item.setData(0,Qt.UserRole,repo); self.tree.addTopLevelItem(item)
            self.status.setText(f"저장소 {len(result)}개 조회 완료")
        else:
            self._stage(4)
            if result.get("uploaded_files", 0) == 0:
                self.status.setText("동일 파일 확인 완료 · 원격 저장소는 이미 최신 상태입니다.")
            else: self.status.setText(f"커밋 검증 완료 · {result['uploaded_files']:,}개 파일 반영 · 진행 마무리 중")
    def _refresh(self):
        try:
            request=self._request(); request.owner; self._remember("github/dashboardHistory",request.dashboard_url); self._remember_token(); self._start(lambda _progress,_cancel:self._client().list_repositories(request),"Repository 목록 조회 중…")
        except GitHubRepositoryError as error: QMessageBox.warning(self,"입력 오류",str(error))
    def _create(self):
        try:
            request=self._request(); request.validate(); self._stage(0)
            if not self.token.text().strip(): raise GitHubRepositoryError("GitHub Token을 입력하십시오.")
            if QMessageBox.question(self,"생성 확인",f"{request.owner}/{request.name} 저장소를 생성합니까?")!=QMessageBox.Yes:return
            self._remember("github/dashboardHistory",request.dashboard_url); self._remember("github/localFolderHistory",request.local_path); self._remember_token(); self._start(lambda _progress,_cancel:self._client().create_repository(request),"Repository 생성 중…",0)
        except GitHubRepositoryError as error: QMessageBox.warning(self,"입력 오류",str(error))
    def _selected_changed(self):
        rows=self.tree.selectedItems(); self.selected=rows[0].data(0,Qt.UserRole) if rows else None
        upload_mode=bool(self.selected)
        for control in (self.name,self.description,self.readme): control.setEnabled(not upload_mode)
        self.create.setEnabled(not upload_mode)
        self.upload.setEnabled(bool(upload_mode and self.root.currentText().strip()))
        if upload_mode:
            self.mode.setText(f"선택 저장소 업로드 / 자동 복구 · {self.selected.full_name}")
            self.status.setText("동일 파일은 자동 건너뛰고 누락 파일만 하나의 Git 커밋으로 복구합니다. 다른 파일은 덮어쓰지 않습니다.")
    def _context_menu(self, point):
        menu=QMenu(self); create=menu.addAction("새 Repository 생성"); create.triggered.connect(self._new_repository); menu.exec(self.tree.viewport().mapToGlobal(point))
    def _new_repository(self):
        self.selected=None; self.tree.clearSelection()
        for control in (self.name,self.description,self.readme): control.setEnabled(True)
        self.create.setEnabled(True); self.upload.setEnabled(False); self.mode.setText("Repository만 생성")
        self.name.setFocus(); self.name.selectAll(); self.status.setText("새 Repository 이름과 설명을 입력한 뒤 생성하십시오.")
    def _upload(self):
        if not self.selected:return
        request=self._request(); request.name=self.selected.name
        if QMessageBox.question(self,"코드 업로드 확인",f"{self.selected.full_name}에 파일을 업로드/복구합니까?\n\n동일 파일은 건너뛰며 다른 원격 파일은 덮어쓰지 않습니다.")!=QMessageBox.Yes:return
        self._remember("github/localFolderHistory",request.local_path); self._start(lambda progress,cancel:self._client().upload_project(request,progress=progress,cancel_event=cancel),"파일 검사 중…",1)
    def closeEvent(self, event):
        if self.worker is not None:
            self.close_when_stopped=True
            if self.cancel_event: self.cancel_event.set()
            self.status.setText("중단 중… 안전하게 Git 작업을 종료한 뒤 창을 닫습니다.")
            self.progress.setFormat("중단 처리 중… %v/%m"); self.progress.show()
            event.ignore()
            return
        event.accept()
