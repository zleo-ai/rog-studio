"""管理器里的灯效一节：走真的 HTTP 接口，写进真的配置文件，再让神光同步按它自己的读法读回来。"""
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "app"))
import aura_sync  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONF_DIR", tmp_path)
    monkeypatch.setattr(server, "CONF", tmp_path / "config.json")
    monkeypatch.setattr(server, "LIGHTS_STATUS", tmp_path / "lights.json")
    monkeypatch.setattr(server, "device_status", lambda conf: {"connected": False, "mode": "演练", "note": ""})
    monkeypatch.setattr(server, "pusher_status", lambda: {"running": False, "autostart": False, "last": ""})
    monkeypatch.setattr(server, "themes", lambda: [])
    monkeypatch.setattr(server.hostsys, "service_autostart", lambda name: False)
    server.LOG.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    yield call, tmp_path
    srv.shutdown()


def synced(tmp_path):
    """神光同步实际会用的值：它自己的读法，读管理器写下的那份文件。"""
    return aura_sync.load_conf(tmp_path / "config.json", aura_sync.DEFAULTS)


def test_settings_reach_the_sync_daemon(mgr):
    call, tmp = mgr
    call("/api/conf", {"theme": "aurora", "layout": "full"})
    call("/api/conf", {"lights_effect": "breathe", "lights_brightness": 40, "lights_devices": ["gpu", "dram"],
                       "lights_argb_leds": 45, "lights_on_exit": "keep", "lights_enabled": False})
    c = synced(tmp)
    assert (c["lights_effect"], c["lights_brightness"], c["lights_devices"]) == ("breathe", 40, ["gpu", "dram"])
    assert (c["lights_argb_leds"], c["lights_on_exit"], c["lights_enabled"]) == (45, "keep", False)
    raw = json.loads((tmp / "config.json").read_text())
    assert (raw["theme"], raw["layout"]) == ("aurora", "full")  # 灯效那几个键不碰别的设置


def test_bad_values_keep_previous(mgr):
    call, tmp = mgr
    call("/api/conf", {"lights_brightness": 40, "lights_effect": "breathe", "lights_devices": ["gpu"]})
    call("/api/conf", {"lights_brightness": 500, "lights_effect": "disco", "lights_devices": ["keyboard"],
                       "lights_enabled": "yes", "lights_argb_leds": 0, "lights_on_exit": None})
    raw = json.loads((tmp / "config.json").read_text())
    assert (raw["lights_brightness"], raw["lights_effect"], raw["lights_devices"]) == (40, "breathe", ["gpu"])
    for k in ("lights_enabled", "lights_argb_leds", "lights_on_exit"):  # 原来没设过的，写坏了就保持没设
        assert raw.get(k, aura_sync.DEFAULTS[k]) == aura_sync.DEFAULTS[k]
    assert synced(tmp)["lights_devices"] == ["gpu"]


def test_lights_only_save_logs_once(mgr):
    call, _ = mgr
    call("/api/conf", {"lights_brightness": 30})
    msgs = [x["msg"] for x in call("/api/state")["log"]]
    assert any(m.startswith("灯效：") for m in msgs)
    assert not any(m.startswith("设置已保存") for m in msgs)


def write_status(tmp, age, **kw):
    (tmp / "lights.json").write_text(json.dumps({"at": int(time.time() - age), **kw}))


DEVS = [{"name": "ENE DRAM", "kind": "dram", "leds": 8}, {"name": "X870", "kind": "motherboard", "leds": 183}]


@pytest.mark.parametrize("age,status,state", [
    (2, {"running": True, "enabled": True, "devices": DEVS}, "syncing"),
    (2, {"running": True, "enabled": False, "devices": DEVS}, "idle"),
    (60, {"running": True, "enabled": True, "devices": DEVS}, "stopped"),  # 程序被杀了，状态文件还停在「在跑」
    (10, {"running": False, "error": "Connection refused"}, "no_openrgb"),
    (300, {"running": False, "error": "Connection refused"}, "stopped"),
    (1, {"running": False, "enabled": True}, "stopped"),  # 正常退出
])
def test_status_is_what_the_daemon_wrote(mgr, age, status, state):
    call, tmp = mgr
    write_status(tmp, age, **status)
    L = call("/api/state")["lights"]
    assert L["state"] == state
    assert L["devices"] == (DEVS if state in ("syncing", "idle") else [])
    assert L["error"] == ("Connection refused" if state == "no_openrgb" else "")


@pytest.mark.parametrize("content", [None, "", "{", '{"at": "x", "running": true}', "[]"])
def test_missing_or_broken_status_is_stopped(mgr, content):
    call, tmp = mgr
    if content is not None:
        (tmp / "lights.json").write_text(content)
    L = call("/api/state")["lights"]
    assert (L["state"], L["devices"], L["autostart"]) == ("stopped", [], False)
    assert L["conf"] == {k: aura_sync.DEFAULTS[k] for k in server.LIGHT_KEYS}
