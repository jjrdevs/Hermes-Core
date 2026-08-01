from __future__ import annotations

from pathlib import Path

from engine.prometheus_server import PrometheusServerManager
from engine.prometheus_sink import PrometheusMetricsSink
from engine.runtime import RuntimeKernel
from engine.runtime_service import RuntimeService


class FakeServer:
    def __init__(self, addr: str, port: int, app):
        self.addr = addr
        self.port = port
        self.app = app
        self.served = False
        self.shutdown_called = False
        self.server_close_called = False

    def serve_forever(self) -> None:
        self.served = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.server_close_called = True


class FakeCounter:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def inc(self, amount: int = 1) -> None:
        self.calls.append(amount)


class FakeGauge:
    def __init__(self, *args, **kwargs):
        self.values = []

    def set(self, value: float) -> None:
        self.values.append(value)


def test_prometheus_server_manager_lifecycle_refcount(monkeypatch):
    PrometheusServerManager._instance = None
    manager = PrometheusServerManager.get()

    server_instances = []

    def fake_make_server(addr, port, app, server_class=None):
        server = FakeServer(addr, port, app)
        server_instances.append(server)
        return server

    monkeypatch.setattr("engine.prometheus_server.make_wsgi_app", lambda: object())
    monkeypatch.setattr("engine.prometheus_server.make_server", fake_make_server)

    manager.acquire(port=9000, addr="127.0.0.1")
    assert manager.refcount() == 1
    assert len(server_instances) == 1

    manager.acquire(port=9000, addr="127.0.0.1")
    assert manager.refcount() == 2

    manager.release()
    assert manager.refcount() == 1
    manager.release()
    assert manager.refcount() == 0
    assert server_instances[0].shutdown_called is True
    assert server_instances[0].server_close_called is True


def test_prometheus_sink_lazily_starts_and_stops(monkeypatch):
    started = []
    stopped = []

    def fake_acquire(port=8000, addr="0.0.0.0"):
        started.append((port, addr))

    def fake_release():
        stopped.append(True)

    monkeypatch.setattr("engine.prometheus_sink.acquire_prometheus_server", fake_acquire)
    monkeypatch.setattr("engine.prometheus_sink.release_prometheus_server", fake_release)
    monkeypatch.setattr("engine.prometheus_sink.Counter", FakeCounter)
    monkeypatch.setattr("engine.prometheus_sink.Gauge", FakeGauge)
    monkeypatch.setattr("engine.prometheus_sink.CollectorRegistry", lambda: object())

    sink = PrometheusMetricsSink(port=9100, addr="127.0.0.1", auto_start=True)
    sink.incr("test_counter")
    assert started == [(9100, "127.0.0.1")]

    sink.close()
    assert stopped == [True]


def test_runtime_shutdown_closes_attached_metrics_sink(tmp_path):
    event_db = tmp_path / "events.db"
    artifact_db = tmp_path / "artifacts.db"
    workflow_definitions_db = tmp_path / "workflow_definitions.db"

    kernel = RuntimeKernel(event_db, artifact_db, workflow_definitions_db)

    class FakeSink:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    sink = FakeSink()
    kernel.metrics = sink

    kernel.shutdown()

    assert sink.closed is True


def test_runtime_service_shutdown_closes_attached_metrics_sink(tmp_path):
    service = RuntimeService(str(tmp_path))

    class FakeSink:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    sink = FakeSink()
    service.kernel.metrics = sink

    service.shutdown()

    assert sink.closed is True
