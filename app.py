import json
import os
import sys
import multiprocessing
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QStandardPaths
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QSplitter, QTabWidget, QProgressBar, QDialog, QFormLayout, QComboBox, QSpinBox,
    QCheckBox, QDialogButtonBox, QMessageBox, QFileDialog, QHeaderView)
from core import Engine, History, safe_error, ai_url
from transport import CancelledError
from pta.submitter import status_text

STYLE = '''
QWidget {background:#101827;color:#e4ecf7;font-size:14px;font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;}
QMainWindow,QDialog {background:#101827;} QLabel#title {font-size:27px;font-weight:700;}
QLabel#muted {color:#9aabc3;} QPushButton {background:#22324a;border:1px solid #34455f;padding:10px 16px;border-radius:7px;}
QPushButton:hover {background:#314766;} QPushButton:disabled {color:#64758e;}
QPushButton#primary {background:#3b72ed;border:none;color:white;}
QLineEdit,QPlainTextEdit,QComboBox,QSpinBox {background:#172338;border:1px solid #34455f;border-radius:5px;padding:7px;}
QTableWidget {background:#172338;border:1px solid #2d3f57;gridline-color:#2d3f57;selection-background-color:#254f8d;}
QHeaderView::section {background:#22324a;padding:9px;border:none;color:#9fb2cb;}
QTabWidget::pane {border:1px solid #34455f;} QTabBar::tab {background:#22324a;padding:9px 20px;}
QTabBar::tab:selected {background:#315687;} QProgressBar {border:1px solid #34455f;border-radius:5px;text-align:center;height:22px;}
QProgressBar::chunk {background:#3b72ed;} QCheckBox {spacing:8px;}
'''

class Worker(QThread):
    event = Signal(str, object)
    def __init__(self, engine, operation, parent=None):
        super().__init__(parent)
        self.engine, self.operation = engine, operation
    def run(self):
        try:
            self.operation(self.engine)
        except CancelledError:
            self.event.emit('log', '任务已停止，可以重新运行或刷新章节。')
        except Exception as exc:
            self.event.emit('log', safe_error(exc, self.engine.settings))
        finally:
            self.event.emit('done', None)
            self.engine.emit = lambda *args: None
            self.operation = None

class Settings(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('连接与模型设置')
        self.resize(610, 520)
        layout = QVBoxLayout(self)
        note = QLabel('Cookie 仅发送给 PTA；AI 服务仅接收题面、代码和判题反馈。\n接口兼容 Chat Completions，地址通常以 /v1 结尾。')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.fields = {}
        for name, label in [('cookie','PTA Cookie'),('base','AI 接口地址'),('key','API Key'),('model','模型名称')]:
            entry = QLineEdit(parent.settings.get(name, ''))
            if name in ('cookie','key'):
                entry.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[name] = entry
            form.addRow(label, entry)
        self.compiler = QComboBox()
        self.compiler.setEditable(True)
        self.compiler.addItems(['GCC','GXX','PYTHON3','JAVAC'])
        self.compiler.setCurrentText(parent.settings.get('compiler', 'GCC'))
        form.addRow('默认编译器', self.compiler)
        self.retry = QSpinBox()
        self.retry.setRange(1, 10)
        self.retry.setValue(parent.settings.get('retries', 1) + 1)
        form.addRow('每题最多尝试次数（含首次）', self.retry)
        self.active = QCheckBox('只读取尚未结束的题集')
        self.active.setChecked(parent.settings.get('active', True))
        form.addRow(self.active)
        self.remember = QCheckBox('将 Cookie 与密钥保存到系统凭据库')
        self.remember.setChecked(parent.settings.get('remember', False))
        form.addRow(self.remember)
        layout.addLayout(form)
        help_text = QLabel('获取 Cookie：在浏览器登录 PTA → F12 → Network → 刷新题集页面\n→ 选择 pintia.cn 的请求 → 复制 Request Headers 的 Cookie 值。\n不要把 Cookie 发到聊天或分享给别人。凭据库不可用时只在本次运行中保留。')
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    def values(self):
        data = {name: field.text().strip() for name, field in self.fields.items()}
        data.update(compiler=self.compiler.currentText(), retries=self.retry.value() - 1,
                    active=self.active.isChecked(), remember=self.remember.isChecked())
        return data

class Window(QMainWindow):
    def __init__(self, data_dir=None):
        super().__init__()
        self.setWindowTitle('PTA 练习助手 v1.2')
        self.resize(1250, 850)
        self.data_dir = Path(data_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / 'settings.json'
        self.settings = dict(base='https://api.example.com/v1', model='', compiler='GCC', retries=1, active=True)
        if self.config_path.exists():
            try:
                self.settings.update(json.loads(self.config_path.read_text('utf-8')))
            except (OSError, ValueError):
                pass
        if self.settings.get('remember'):
            try:
                import keyring
                for key in ('cookie','key'):
                    self.settings[key] = keyring.get_password('PTAPracticeAssistant', key) or ''
            except Exception:
                pass
        self.history = History(self.data_dir / 'history.json')
        self.demo, self.worker, self.catalog, self.eid = False, None, {}, ''
        self.psid = ''
        self.controls = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(26, 22, 26, 22)
        header = QHBoxLayout()
        title = QLabel('PTA 练习助手'); title.setObjectName('title')
        header.addWidget(title); header.addStretch()
        self.mode_label = QLabel('本地运行 · 凭据独立管理'); self.mode_label.setObjectName('muted')
        header.addWidget(self.mode_label)
        header.addWidget(self.button('连接设置', self.configure))
        layout.addLayout(header)
        subtitle = QLabel('选择章节，查看题目，生成代码并跟踪 PTA 判题结果。'); subtitle.setObjectName('muted')
        layout.addWidget(subtitle)
        bar = QHBoxLayout()
        bar.addWidget(self.button('读取我的章节', self.chapters, True))
        self.manual_id = QLineEdit(); self.manual_id.setPlaceholderText('也可输入题集 ID 或 PTA 题集链接')
        self.controls.append(self.manual_id)
        bar.addWidget(self.manual_id)
        bar.addWidget(self.button('打开题集', self.open_manual))
        bar.addWidget(self.button('离线演示', self.start_demo))
        layout.addLayout(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.chapter_table = QTableWidget(0, 2)
        self.chapter_table.setHorizontalHeaderLabels(['章节 / 题集', '截止时间'])
        self.chapter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.chapter_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.chapter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.chapter_table.cellDoubleClicked.connect(self.open_chapter)
        split.addWidget(self.chapter_table)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.chapter_label = QLabel('双击左侧章节进入题目列表')
        right_layout.addWidget(self.chapter_label)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['选择', '题目', '题型', '状态'])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55); self.table.setColumnWidth(2, 145); self.table.setColumnWidth(3, 215)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.preview)
        right_layout.addWidget(self.table)
        actions = QHBoxLayout()
        actions.addWidget(self.button('全选未通过', self.select_all))
        actions.addWidget(self.button('生成所选代码', lambda: self.run_batch(False), True))
        actions.addWidget(self.button('生成并自动提交', lambda: self.run_batch(True)))
        self.pause_button = QPushButton('暂停'); self.pause_button.setEnabled(False); self.pause_button.clicked.connect(self.pause)
        actions.addWidget(self.pause_button)
        self.stop_button = QPushButton('停止'); self.stop_button.setEnabled(False); self.stop_button.clicked.connect(self.stop)
        actions.addWidget(self.stop_button)
        right_layout.addLayout(actions)
        split.addWidget(right); split.setSizes([300, 850]); layout.addWidget(split, 3)
        tabs = QTabWidget()
        self.statement_view = QPlainTextEdit(); self.statement_view.setReadOnly(True)
        self.statement_view.setPlaceholderText('选择一道题查看题面。')
        self.code = QPlainTextEdit(); self.code.setPlaceholderText('生成的代码会显示在这里，可以编辑后提交。')
        self.code.setFont(QFont('monospace', 11))
        self.editor_pid = ''
        self.editor_psid = ''
        self.code_dirty = False
        self.code.textChanged.connect(self.mark_dirty)
        code_page = QWidget(); cl = QVBoxLayout(code_page); cl.addWidget(self.code)
        cb = QHBoxLayout(); cb.addWidget(self.button('保存代码到文件', self.export)); cb.addWidget(self.button('提交当前编辑的代码', self.submit_manual)); cb.addWidget(self.button('已核对未提交，解除阻塞', self.clear_uncertain)); cb.addStretch(); cl.addLayout(cb)
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True)
        tabs.addTab(self.statement_view, '题面'); tabs.addTab(code_page, '代码编辑'); tabs.addTab(self.logs, '运行记录')
        layout.addWidget(tabs, 2)
        self.progress = QProgressBar(); self.progress.setValue(0); layout.addWidget(self.progress)
        self.footer = QLabel('就绪 · 自动提交仅用于课程与平台允许的练习。支持编程题、程序填空题。')
        self.footer.setObjectName('muted'); layout.addWidget(self.footer)
        self.logs.appendPlainText(f'任务与代码保存位置：{self.data_dir}')
    def button(self, text, action, primary=False):
        b = QPushButton(text); b.clicked.connect(action)
        if primary: b.setObjectName('primary')
        self.controls.append(b)
        return b
    def mark_dirty(self):
        self.code_dirty = True
    def persist_editor(self):
        if self.code_dirty and self.editor_pid and self.editor_psid:
            self.history.put(self.editor_psid, self.editor_pid, code=self.code.toPlainText())
            self.code_dirty = False
    def configure(self):
        dlg = Settings(self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        self.settings.update(dlg.values())
        saved = {k:v for k,v in self.settings.items() if k not in ('cookie','key','psid')}
        try:
            import keyring
            for key in ('cookie','key'):
                if saved['remember']: keyring.set_password('PTAPracticeAssistant', key, self.settings.get(key, ''))
                else:
                    try: keyring.delete_password('PTAPracticeAssistant', key)
                    except keyring.errors.PasswordDeleteError: pass
        except Exception:
            saved['remember'] = False
            self.settings['remember'] = False
            self.logs.appendPlainText('系统凭据库不可用：Cookie 和密钥只保留在内存中。')
        self.config_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), 'utf-8')
    def launch(self, operation):
        if self.worker and self.worker.isRunning(): return
        self.persist_editor()
        snapshot = dict(self.settings, psid=self.psid)
        engine = Engine(snapshot, self.history, None, self.demo)
        if self.worker is not None:
            self.worker.wait()
            self.worker.deleteLater()
        self.worker = Worker(engine, operation, self)
        engine.emit = self.worker.event.emit
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.finished)
        for control in self.controls: control.setEnabled(False)
        # Keep both lists enabled so their viewports and scrollbars receive wheel events.
        # Task targets are a snapshot; chapter changes and previews are guarded while busy.
        self.code.setReadOnly(True)
        self.pause_button.setEnabled(True); self.stop_button.setEnabled(True)
        self.footer.setText('正在运行 · 暂停会等待当前请求，停止会取消请求')
        self.worker.start()
    def finished(self):
        if self.sender() is not None and self.sender() is not self.worker: return
        stopped = self.worker.engine.stop.is_set()
        for control in self.controls: control.setEnabled(True)
        self.table.setEnabled(True); self.code.setReadOnly(False)
        self.pause_button.setEnabled(False); self.stop_button.setEnabled(False)
        self.pause_button.setText('暂停')
        self.footer.setText('已停止 · 可以重新运行或刷新章节；已发出的提交仍需查询结果' if stopped else '任务结束 · 通过情况以 PTA 判题结果为准；详细信息见运行记录')
    def handle_event(self, kind, value):
        if self.sender() is not None and self.sender() is not self.worker: return
        if kind == 'log': self.logs.appendPlainText(value)
        elif kind == 'chapters':
            self.chapter_table.setRowCount(len(value))
            for row, chapter in enumerate(value):
                item = QTableWidgetItem(chapter.get('name') or chapter.get('title') or str(chapter['id']))
                item.setData(Qt.ItemDataRole.UserRole, str(chapter['id']))
                self.chapter_table.setItem(row, 0, item)
                self.chapter_table.setItem(row, 1, QTableWidgetItem(str(chapter.get('endAt') or '长期开放')))
            self.logs.appendPlainText(f'已读取 {len(value)} 个章节 / 题集。双击进入。')
        elif kind == 'loaded':
            self.eid, self.catalog, compilers = value
            self.chapter_label.setText(f'当前题集：{self.psid} · {len(self.catalog)} 道题')
            self.compilers = compilers
            self.table.setRowCount(len(self.catalog))
            for row, meta in enumerate(self.catalog.values()):
                check = QTableWidgetItem(); check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Unchecked); check.setData(Qt.ItemDataRole.UserRole, meta.pspid)
                self.table.setItem(row, 0, check)
                for col, text in enumerate([meta.title, meta.ptype, status_text(meta.status)], 1):
                    self.table.setItem(row, col, QTableWidgetItem(text))
            self.logs.appendPlainText('题目状态已同步；可用编译器：' + ', '.join(compilers))
        elif kind == 'status':
            pid, text = value
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == pid:
                    self.table.item(row, 3).setText(text)
            self.logs.appendPlainText(f'{pid}：{text}')
            if text == '通过': self.catalog[pid].status = 'PROBLEM_ACCEPTED'
        elif kind in ('statement', 'code'):
            pid, text = value
            self.editor_pid, self.editor_psid = pid, self.psid + '/' + str(self.eid)
            if kind == 'statement': self.statement_view.setPlainText(text)
            else:
                self.code.setPlainText(text); self.code_dirty = False
        elif kind == 'progress':
            done, total = value
            self.progress.setMaximum(total); self.progress.setValue(done)
            self.progress.setFormat(f'已处理 {done} / {total} 题（处理数不等于通过数）')
    def clear_context(self):
        self.persist_editor()
        self.catalog = {}; self.eid = ''; self.psid = ''; self.editor_pid = ''; self.editor_psid = ''
        self.table.setRowCount(0); self.chapter_table.setRowCount(0); self.statement_view.clear(); self.code.clear(); self.code_dirty = False
        self.chapter_label.setText('双击左侧章节进入题目列表')
    def chapters(self):
        self.clear_context()
        self.demo = False; self.mode_label.setText('真实连接 · PTA')
        self.launch(lambda e: e.emit('chapters', e.chapters()))
    def start_demo(self):
        self.clear_context()
        self.demo = True; self.mode_label.setText('离线演示 · 不连接 PTA / AI')
        self.launch(lambda e: e.emit('chapters', e.chapters()))
    def open_manual(self):
        import re
        text = self.manual_id.text().strip()
        match = re.search(r'problem-sets/(\d+)', text)
        psid = match.group(1) if match else text
        if not psid.isascii() or not psid.isdigit():
            QMessageBox.information(self, '题集地址', '请输入数字题集 ID 或包含 problem-sets/ID 的链接。'); return
        self.demo = False; self.mode_label.setText('真实连接 · PTA'); self.open_set(psid)
    def open_chapter(self, row, col):
        if self.worker and self.worker.isRunning(): return
        self.open_set(self.chapter_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
    def open_set(self, psid):
        self.persist_editor(); self.psid = psid; self.editor_pid = ''; self.editor_psid = ''
        self.catalog = {}; self.eid = ''; self.table.setRowCount(0)
        self.statement_view.clear(); self.code.clear(); self.code_dirty = False
        self.chapter_label.setText('正在读取题集：' + psid)
        self.launch(lambda e: e.emit('loaded', e.load()))
    def current_pid(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else ''
    def preview(self):
        if self.worker and self.worker.isRunning(): return
        pid = self.current_pid()
        if not pid: return
        self.persist_editor()
        self.editor_pid, self.editor_psid = pid, self.psid + '/' + str(self.eid)
        self.code.setPlainText(self.history.get(self.psid + '/' + str(self.eid), pid).get('code', '')); self.code_dirty = False
        self.launch(lambda e: e.emit('statement', (pid, e.statement(pid))))
    def select_all(self):
        for row in range(self.table.rowCount()):
            pid = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            eligible = self.catalog[pid].status != 'PROBLEM_ACCEPTED' and self.catalog[pid].ptype in ('PROGRAMMING','CODE_COMPLETION')
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if eligible else Qt.CheckState.Unchecked)
    def validate(self, auto):
        if not self.demo:
            if auto and getattr(self, 'compilers', []) and self.settings['compiler'] not in self.compilers:
                QMessageBox.information(self,'编译器','请在设置中选择题集支持的编译器：' + ', '.join(self.compilers)); return False
            try: ai_url(self.settings['base'])
            except ValueError as exc:
                QMessageBox.information(self,'接口设置',str(exc)); return False
        return True
    def run_batch(self, auto):
        targets = [self.catalog[self.table.item(row,0).data(Qt.ItemDataRole.UserRole)] for row in range(self.table.rowCount())
                   if self.table.item(row,0).checkState() == Qt.CheckState.Checked]
        if not targets:
            QMessageBox.information(self,'选择题目','请先勾选要处理的题目。'); return
        if not self.validate(auto): return
        self.progress.setMaximum(len(targets)); self.progress.setValue(0)
        self.launch(lambda e: e.run(self.eid, targets, auto))
    def submit_manual(self):
        pid = self.editor_pid
        if not pid or not self.code.toPlainText().strip():
            QMessageBox.information(self,'提交代码','请先选择题目并填写代码。'); return
        if not self.validate(True): return
        code = self.code.toPlainText()
        self.launch(lambda e: e.run(self.eid, [self.catalog[pid]], True, code))
    def pause(self):
        if not self.worker or not self.worker.isRunning() or self.worker.engine.stop.is_set(): return
        if self.worker.engine.resume.is_set():
            self.worker.engine.resume.clear(); self.pause_button.setText('继续')
            self.footer.setText('已请求暂停 · 当前请求结束后暂停；点击继续可恢复')
        else:
            self.worker.engine.resume.set(); self.pause_button.setText('暂停')
            self.footer.setText('正在运行')
    def stop(self):
        if not self.worker or not self.worker.isRunning(): return
        self.worker.engine.stop.set(); self.worker.engine.resume.set()
        self.pause_button.setEnabled(False); self.stop_button.setEnabled(False)
        self.footer.setText('正在取消当前请求 · 随后可重新运行或刷新；已发出的提交不能撤回')
    def clear_uncertain(self):
        pid = self.editor_pid
        if not pid: return
        scope = self.psid + '/' + str(self.eid)
        saved = self.history.get(scope, pid)
        if saved.get('state') not in ('UNKNOWN','SUBMITTING','SUBMITTED'):
            QMessageBox.information(self, '任务记录', '当前题目没有待核对的提交。'); return
        answer = QMessageBox.question(self, '核对提交记录', '请先在 PTA 网页检查此题的提交记录。\n只有确认上次提交没有成功到达 PTA，才解除阻塞。\n你已确认未提交成功吗？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.history.put(scope, pid, state='GENERATED', submission_id=None)
            self.logs.appendPlainText(pid + '：用户核对后解除提交阻塞。')
    def export(self):
        filename, _ = QFileDialog.getSaveFileName(self,'保存代码',f'{self.editor_pid or "answer"}.txt','源代码 (*.*)')
        if filename: Path(filename).write_text(self.code.toPlainText(), 'utf-8')
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.stop()
            self.footer.setText('正在停止任务，请在任务结束后关闭窗口。')
            event.ignore()
        else:
            if self.worker is not None: self.worker.wait()
            self.persist_editor(); event.accept()

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if '--transport-check' in sys.argv:
        from transport import smoke_check
        smoke_check()
        sys.exit(0)
    app = QApplication(sys.argv); app.setApplicationName('PTAPracticeAssistant'); app.setStyleSheet(STYLE)
    window = Window(); window.show()
    if '--screenshot' in sys.argv:
        def capture():
            window.grab().save(sys.argv[sys.argv.index('--screenshot') + 1]); app.quit()
        QTimer.singleShot(700, capture)
    sys.exit(app.exec())
