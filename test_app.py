import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import threading
import multiprocessing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QTableWidgetItem
from PySide6.QtCore import Qt, QPoint, QPointF, QCoreApplication, QEvent
from PySide6.QtGui import QWheelEvent
from app import Window, STYLE
from core import Engine, History, ai_url, generate
from transport import request, CancelledError
from pta.submitter import ProblemMeta

@contextmanager
def hanging_server():
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set(); release.wait(10)
            try:
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        do_POST = do_GET
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', entered
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join()


class TransportTests(unittest.TestCase):
    def test_actual_waiting_http_is_cancelled_and_child_reaped(self):
        with hanging_server() as (url, entered):
            cancel = threading.Event()
            result = []
            def run():
                try: request('GET', url, cancel=cancel, timeout=(10, 180))
                except CancelledError: result.append('cancelled')
            thread = threading.Thread(target=run); thread.start()
            self.assertTrue(entered.wait(5))
            start = time.monotonic(); cancel.set(); thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic()-start, 2)
            self.assertEqual(result, ['cancelled'])
            self.assertFalse(multiprocessing.active_children())
    def test_success_returns_response(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            response=request('GET',f'http://127.0.0.1:{server.server_port}',cancel=threading.Event(),timeout=3)
            self.assertEqual(response.json(), {'ok':True})
        finally: server.shutdown(); server.server_close(); thread.join()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / 'history.json')
        self.events = []
        self.settings = {'psid':'123', 'cookie':'private-cookie', 'key':'private-key',
                         'base':'https://example.com/v1', 'model':'test-model', 'compiler':'GCC', 'retries':1}
        self.engine = Engine(self.settings, self.history, lambda k,v: self.events.append((k,v)))
        self.meta = ProblemMeta('1','A+B','PROGRAMMING',10,0,'PROBLEM_NO_ANSWER')
    def tearDown(self): self.tmp.cleanup()
    def test_submission_uncertain_never_reposted(self):
        with patch.object(self.engine, 'statement', return_value='sum'), patch('core.generate', return_value='code'), patch.object(self.engine.client, 'submit', side_effect=TimeoutError) as submit:
            self.engine.run('exam', [self.meta], True)
            self.engine.run('exam', [self.meta], True)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(self.history.get('123/exam','1')['state'], 'UNKNOWN')
    def test_resume_queries_same_submission(self):
        self.history.put('123/exam','1',state='SUBMITTED', submission_id='88')
        with patch.object(self.engine, 'poll', return_value={'status':'ACCEPTED','statusText':'通过'}) as poll, patch.object(self.engine.client,'submit') as submit:
            self.engine.run('exam', [self.meta], True)
            poll.assert_called_once_with('exam','88'); submit.assert_not_called()
    def test_retry_after_known_failure(self):
        with patch.object(self.engine,'statement',return_value='sum'), patch('core.generate',side_effect=['first','fixed']) as gen, patch.object(self.engine.client,'submit',return_value={'submissionId':'88'}) as submit, patch.object(self.engine,'poll',side_effect=[{'status':'WRONG_ANSWER','statusText':'答案错误'}, {'status':'ACCEPTED','statusText':'通过'}]):
            self.engine.run('exam',[self.meta],True)
            self.assertEqual(submit.call_count,2); self.assertEqual(gen.call_count,2)
            self.assertEqual(self.history.get('123/exam','1')['state'],'ACCEPTED')
            self.assertNotIn('private-', self.history.path.read_text())
    def test_attempt_limit_advances_to_next_problem(self):
        next_meta = ProblemMeta('2','Next','PROGRAMMING',10,0,'PROBLEM_NO_ANSWER')
        for maximum in (1, 2, 4):
            with self.subTest(maximum=maximum):
                self.settings['retries'] = maximum - 1
                verdicts = [{'status':'WRONG_ANSWER','statusText':'答案错误'}] * maximum + [{'status':'ACCEPTED','statusText':'通过'}]
                with patch.object(self.engine,'statement',return_value='statement'), patch('core.generate',return_value='code'), patch.object(self.engine.client,'submit',return_value={'submissionId':'88'}) as submit, patch.object(self.engine,'poll',side_effect=verdicts):
                    self.engine.run(f'exam{maximum}', [self.meta,next_meta], True)
                    ids = [call.args[1]['details'][0]['problemSetProblemId'] for call in submit.call_args_list]
                    self.assertEqual(ids, ['1'] * maximum + ['2'])
                    first = self.history.get(f'123/exam{maximum}', '1')
                    self.assertTrue(first['exhausted'])
                    self.assertEqual(first['attempts'], maximum)
                    self.assertEqual(self.history.get(f'123/exam{maximum}', '2')['state'], 'ACCEPTED')
    def test_ai_credentials_are_separate(self):
        class Response:
            status_code=200
            def json(self): return {'choices':[{'message':{'content':'```c\nint main(){}\n```'}}]}
        with patch('core.requests.post',return_value=Response()) as post:
            generate(self.settings,'sum','PROGRAMMING')
            kwargs = post.call_args.kwargs
            self.assertNotIn('Cookie',kwargs['headers'])
            self.assertNotIn('private-cookie',str(kwargs))
            self.assertFalse(kwargs['allow_redirects'])
        with self.assertRaises(ValueError): ai_url('http://remote.example/v1')
    def test_stop_before_any_request(self):
        self.engine.stop.set()
        with patch.object(self.engine,'statement') as statement:
            self.engine.run('exam',[self.meta],True); statement.assert_not_called()
    def test_demo_never_calls_network(self):
        engine = Engine(self.settings,self.history,lambda k,v:None,True)
        with patch('core.requests.post') as post, patch.object(engine.client,'submit') as submit:
            engine.run('demo',[self.meta],True)
            post.assert_not_called(); submit.assert_not_called()

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([]); cls.app.setStyleSheet(STYLE)
    def drain(self, window):
        deadline = time.monotonic()+5
        while window.worker and window.worker.isRunning() and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(window.worker.isRunning())
    def test_stop_actual_http_restores_ui_and_allows_refresh(self):
        with tempfile.TemporaryDirectory() as directory, hanging_server() as (url, entered):
            w=Window(directory); w.show()
            w.launch(lambda e: generate(dict(e.settings, base=url + '/v1', model='test'), 'sum', 'PROGRAMMING', cancel=e.stop))
            try:
                deadline=time.monotonic()+5
                while not entered.is_set() and time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.01)
                self.assertTrue(entered.is_set())
                w.pause()
                self.assertFalse(w.worker.engine.resume.is_set())
                start=time.monotonic(); w.stop(); self.drain(w)
                self.assertLess(time.monotonic()-start,2)
                self.assertTrue(w.manual_id.isEnabled())
                self.assertTrue(all(c.isEnabled() for c in w.controls))
                self.assertFalse(w.pause_button.isEnabled())
                w.start_demo(); self.drain(w)
                self.assertEqual(w.chapter_table.rowCount(),1)
                w.open_chapter(0,0); self.drain(w)
                w.select_all(); w.run_batch(False); self.drain(w)
                self.assertEqual(w.progress.value(),3)
            finally:
                w.stop()
                if w.worker.isRunning(): self.drain(w)
                w.close(); w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    def test_cancel_during_submit_preserves_uncertain_state(self):
        with tempfile.TemporaryDirectory() as directory, hanging_server() as (url, entered):
            w=Window(directory); w.show(); w.psid='123'; w.eid='exam'
            meta=ProblemMeta('1','A+B','PROGRAMMING',10,0,'PROBLEM_NO_ANSWER')
            w.catalog={'1':meta}
            def operation(e):
                e.client.cfg.base_url=url
                with patch.object(e,'statement',return_value='sum'), patch('core.generate',return_value='code'):
                    e.run('exam',[meta],True)
            w.launch(operation)
            try:
                deadline=time.monotonic()+5
                while not entered.is_set() and time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.01)
                self.assertTrue(entered.is_set())
                w.stop(); self.drain(w)
                self.assertEqual(w.history.get('123/exam','1')['state'],'UNKNOWN')
                self.assertTrue(w.manual_id.isEnabled())
            finally:
                w.stop()
                if w.worker.isRunning(): self.drain(w)
                w.close(); w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    def test_wheel_scroll_during_run_pause_stop_and_after_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            w = Window(directory); w.show()
            for table in (w.chapter_table, w.table):
                table.setRowCount(100)
                for row in range(100):
                    table.setItem(row, 0, QTableWidgetItem(str(row)))
            self.app.processEvents()
            release = threading.Event()
            w.launch(lambda e: release.wait(5))
            def assert_wheel_works():
                self.app.processEvents()
                for table in (w.chapter_table, w.table):
                    scrollbar = table.verticalScrollBar()
                    self.assertTrue(table.isEnabled())
                    self.assertGreater(scrollbar.maximum(), 0)
                    scrollbar.setValue(0)
                    event = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
                    self.app.sendEvent(table.viewport(), event)
                    self.assertGreater(scrollbar.value(), 0)
            try:
                assert_wheel_works()
                old_psid = w.psid
                w.open_chapter(0,0)
                self.assertEqual(w.psid, old_psid)
                w.pause(); assert_wheel_works()
                w.stop(); assert_wheel_works()
            finally:
                release.set(); self.drain(w)
            assert_wheel_works(); w.close(); w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    def test_demo_select_generate_and_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            w=Window(directory); w.show(); w.start_demo(); self.drain(w)
            self.assertEqual(w.chapter_table.rowCount(),1)
            w.open_chapter(0,0); self.drain(w)
            self.assertEqual(w.table.rowCount(),3)
            w.select_all(); w.run_batch(True); self.drain(w)
            self.assertEqual(w.progress.value(),3)
            self.assertIn('int main()',w.code.toPlainText())
            self.assertTrue(w.table.isEnabled())
            w.grab().save(str(Path(__file__).parent.parent/'界面预览.png'))
            w.clear_context(); self.assertEqual(w.table.rowCount(),0)
            self.assertFalse(w.editor_pid)
            w.close(); w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

if __name__=='__main__': unittest.main()
