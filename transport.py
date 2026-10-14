"""Cancelable HTTP requests in isolated processes; credentials stay in memory.

A thread cannot reliably interrupt requests/DNS/TLS. Each HTTP call therefore
owns one spawned process. Stop terminates that process, never the Qt worker.
"""
import multiprocessing
import time
import requests


class CancelledError(Exception):
    pass


def _http_child(pipe, method, url, headers, trust_env, kwargs):
    try:
        with requests.Session() as session:
            session.trust_env = trust_env
            session.headers.update(headers)
            with session.request(method, url, stream=True, **kwargs) as response:
                chunks = []
                size = 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise ValueError('响应超过 16 MB 上限')
                    chunks.append(chunk)
                pipe.send(('ok', response.status_code, response.url, dict(response.headers), b''.join(chunks), response.encoding))
    except Exception:
        # Do not serialize exceptions containing credential-bearing URLs/headers.
        pipe.send(('error',))
    finally:
        pipe.close()


def request(method, url, *, cancel, headers=None, trust_env=True, deadline_seconds=200, **kwargs):
    if cancel.is_set():
        raise CancelledError('任务已停止')
    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_http_child, args=(send, method, url, headers or {}, trust_env, kwargs), daemon=True)
    started = False
    try:
        process.start()
        started = True
        send.close()
        deadline = time.monotonic() + deadline_seconds
        while True:
            if cancel.is_set():
                raise CancelledError('任务已停止')
            if time.monotonic() >= deadline:
                raise requests.Timeout('请求达到总时限')
            if receive.poll(.05):
                try:
                    payload = receive.recv()
                except EOFError:
                    raise requests.ConnectionError('网络进程提前退出') from None
                if cancel.is_set():
                    raise CancelledError('任务已停止')
                if payload[0] != 'ok':
                    raise requests.ConnectionError('网络请求失败')
                response = requests.Response()
                _, response.status_code, response.url, headers_out, response._content, response.encoding = payload
                response.headers.update(headers_out)
                return response
            if not process.is_alive():
                raise requests.ConnectionError('网络进程提前退出')
    finally:
        receive.close()
        send.close()
        if started:
            process.join(.05)
            if process.is_alive():
                process.terminate()
                process.join(.5)
            if process.is_alive():
                process.kill()
                process.join(.5)
            process.close()


class CancelableSession:
    def __init__(self, session, cancel):
        self.headers = dict(session.headers)
        self.trust_env = session.trust_env
        self.cancel = cancel
        session.close()

    def get(self, url, **kwargs):
        return request('GET', url, cancel=self.cancel, headers=self.headers,
                       trust_env=self.trust_env, deadline_seconds=45, **kwargs)

    def post(self, url, **kwargs):
        return request('POST', url, cancel=self.cancel, headers=self.headers,
                       trust_env=self.trust_env, deadline_seconds=45, **kwargs)


def smoke_check():
    """Check spawned HTTP cancellation inside a frozen executable, offline."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/hang':
                entered.set(); release.wait(10)
            try:
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    cancel = threading.Event()
    outcome = []
    def blocking():
        try:
            request('GET', url + '/hang', cancel=cancel, timeout=180)
            outcome.append('unexpected completion')
        except CancelledError:
            outcome.append('cancelled')
    task = threading.Thread(target=blocking)
    try:
        assert request('GET', url, cancel=cancel, timeout=3).json() == {'ok': True}
        task.start()
        assert entered.wait(8), 'request did not start'
        start = time.monotonic(); cancel.set(); task.join(2)
        assert not task.is_alive(), 'request failed to cancel'
        assert outcome == ['cancelled']
        assert not multiprocessing.active_children(), 'request subprocess leaked'
        print(f'HTTP success and stop verified; cancellation took {time.monotonic()-start:.3f}s')
    finally:
        cancel.set(); release.set()
        if task.ident is not None: task.join(3)
        server.shutdown(); server.server_close(); server_thread.join()
