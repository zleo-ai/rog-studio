"""HidTransport 和 hidapi 之间的约定，用假的 hid 模块测，不碰硬件。

hidapi 的 read(n, 0) 在阻塞模式下会一直等；心跳每秒以 timeout 0 收回包，屏不回包时就卡死（Windows 真机上见过）。
所以打开设备后必须切到非阻塞。"""
import sys
import types

from ryuo.transport import HidTransport


class FakeHidDevice:
    def __init__(self):
        self.nonblocking = 0
        self.opened = None
        self.reads = []

    def open_path(self, path):
        self.opened = path

    def set_nonblocking(self, v):
        self.nonblocking = int(v)
        return 0

    def read(self, n, timeout_ms=0):
        self.reads.append((n, timeout_ms))
        if timeout_ms == 0 and not self.nonblocking:
            raise AssertionError("blocking read with no timeout would hang forever")
        return []


def make(monkeypatch):
    dev = FakeHidDevice()
    monkeypatch.setitem(sys.modules, "hid", types.SimpleNamespace(device=lambda: dev, enumerate=lambda *a: []))
    return HidTransport(path=b"fake-path"), dev


def test_opens_nonblocking(monkeypatch):
    t, dev = make(monkeypatch)
    assert dev.opened == b"fake-path"
    assert dev.nonblocking == 1


def test_zero_timeout_read_returns_immediately(monkeypatch):
    t, dev = make(monkeypatch)
    assert t.read_report(0) == b""
    assert t.read_report(250) == b""
    assert dev.reads == [(1024, 0), (1024, 250)]


def test_negative_timeout_is_treated_as_no_wait(monkeypatch):
    t, dev = make(monkeypatch)
    t.read_report(-5)
    assert dev.reads == [(1024, 0)]
