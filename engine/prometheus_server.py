from __future__ import annotations

import threading
from contextlib import suppress
from typing import Optional

try:
    from prometheus_client import make_wsgi_app
    from wsgiref.simple_server import make_server, WSGIServer
    from socketserver import ThreadingMixIn

    class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):  # type: ignore[misc]
        daemon_threads = True
except Exception:  # pragma: no cover - optional dependency
    make_wsgi_app = None  # type: ignore
    make_server = None  # type: ignore

    class _ThreadingWSGIServer(object):  # type: ignore[no-redef]
        daemon_threads = True


class PrometheusServerManager:
    """Singleton manager to start/stop a Prometheus exposition server with reference counting.

    Use `acquire(port, addr)` to indicate a component needs the server and `release()` when it's
    no longer needed. The server is started lazily on the first acquire and stopped when the
    last release occurs.
    """

    _lock = threading.Lock()
    _instance: Optional["PrometheusServerManager"] = None

    def __init__(self) -> None:
        self._refcount = 0
        self._server_thread: Optional[threading.Thread] = None
        self._httpd = None
        self._addr = None
        self._port = None

    @classmethod
    def get(cls) -> "PrometheusServerManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = PrometheusServerManager()
            return cls._instance

    def acquire(self, port: int = 8000, addr: str = "0.0.0.0") -> None:
        if make_wsgi_app is None:
            raise RuntimeError("prometheus_client is required to start the exposition server")

        with self._lock:
            self._refcount += 1
            if self._refcount == 1:
                # start server
                app = make_wsgi_app()
                self._addr = addr
                self._port = port
                self._httpd = make_server(addr, port, app, server_class=_ThreadingWSGIServer)
                def run() -> None:
                    try:
                        self._httpd.serve_forever()
                    except Exception:
                        pass

                self._server_thread = threading.Thread(target=run, name="prometheus-http", daemon=True)
                self._server_thread.start()

    def release(self) -> None:
        with self._lock:
            if self._refcount <= 0:
                return
            self._refcount -= 1
            if self._refcount == 0 and self._httpd is not None:
                # shutdown server
                try:
                    self._httpd.shutdown()
                except Exception:
                    pass
                with suppress(Exception):
                    self._httpd.server_close()
                self._httpd = None
                self._server_thread = None
                self._addr = None
                self._port = None

    def refcount(self) -> int:
        with self._lock:
            return int(self._refcount)


def acquire_prometheus_server(port: int = 8000, addr: str = "0.0.0.0") -> None:
    PrometheusServerManager.get().acquire(port=port, addr=addr)


def release_prometheus_server() -> None:
    PrometheusServerManager.get().release()


def current_refcount() -> int:
    return PrometheusServerManager.get().refcount()
