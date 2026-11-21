"""Windows 安装这一层：任务放在哪个文件夹、服务外壳什么时候放弃重启、屏上实况默认关、安装脚本默认只生成定义。
都不碰屏幕：截屏接口里的 adb 换成假的，安装脚本只在 -Register 没给时跑（只写 XML，不注册）。"""
import json
import os
import shutil
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "app"))
import hostsys  # noqa: E402

ROOT = HERE.parent.parent


class Recorder:
    def __init__(self, stdout="", returncode=0):
        self.calls, self.stdout, self.returncode = [], stdout, returncode

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return types.SimpleNamespace(stdout=self.stdout, returncode=self.returncode)


# ---------------- 任务文件夹 ----------------

def test_task_name_defaults_to_root(monkeypatch, tmp_path):
    monkeypatch.setattr(hostsys, "OUT", tmp_path)
    assert hostsys.task_name("pusher") == "RyuoStudio-pusher"


def test_task_name_follows_installer_file(monkeypatch, tmp_path):
    monkeypatch.setattr(hostsys, "OUT", tmp_path)
    (tmp_path / "windows-tasks.json").write_text(json.dumps({"task_path": "\\Tools\\"}), encoding="utf-8")
    assert hostsys.task_name("manager") == "\\Tools\\RyuoStudio-manager"
    monkeypatch.setattr(hostsys, "IS_WIN", True)
    rec = Recorder()
    monkeypatch.setattr(hostsys.subprocess, "run", rec)
    hostsys.service("start", "pusher")
    assert rec.calls == [["schtasks", "/Run", "/TN", "\\Tools\\RyuoStudio-pusher"]]


@pytest.mark.parametrize("bad", ["Tools", "\\Tools", "\\..\\x\\", 7, None])
def test_task_name_ignores_bad_folder(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(hostsys, "OUT", tmp_path)
    (tmp_path / "windows-tasks.json").write_text(json.dumps({"task_path": bad}), encoding="utf-8")
    assert hostsys.task_name("pusher") == "RyuoStudio-pusher"


# ---------------- 服务外壳 ----------------

def run_shell(tmp_path, script_body):
    """用一个假的 pusher 脚本跑 run_service.py，看它什么时候放弃。"""
    fake = tmp_path / "fake_pusher.py"
    fake.write_text(script_body, encoding="utf-8")
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import sys, time, pathlib\n"
        f"sys.path.insert(0, {str(HERE.parent)!r})\n"
        "import run_service\n"
        f"run_service.OUT = pathlib.Path({str(tmp_path)!r})\n"
        f"run_service.SCRIPTS['pusher'] = pathlib.Path({str(fake)!r})\n"
        "run_service.time.sleep = lambda s: None\n"
        "sys.argv = ['run_service.py', 'pusher']\n"
        "run_service.main()\n", encoding="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", "-I", str(driver)], capture_output=True, text=True, timeout=60)


def test_run_service_gives_up_after_five_quick_exits(tmp_path):
    r = run_shell(tmp_path, "raise RuntimeError('boom')\n")
    assert r.returncode == 3
    log = (tmp_path / "pusher.log").read_text(encoding="utf-8")
    assert log.count("RuntimeError: boom") == 5
    assert "不再重启" in log


class Stuck(BaseException):
    pass


def test_run_service_resets_streak_after_a_long_run(tmp_path, monkeypatch):
    import run_service
    runs = []
    clock = [0.0]

    def fake_run_path(path, run_name):
        runs.append(1)
        if len(runs) > 50:  # 没有放弃逻辑时会一直重来；跑到这里就算失败，不让测试卡死
            raise Stuck("run_service never gave up")
        clock[0] += 120 if len(runs) == 3 else 1  # 第 3 次跑了两分钟，计数清零
    monkeypatch.setattr(run_service, "OUT", tmp_path)
    monkeypatch.setattr(run_service.runpy, "run_path", fake_run_path)
    monkeypatch.setattr(run_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["run_service.py", "pusher"])
    out, err = sys.stdout, sys.stderr
    try:
        with pytest.raises(SystemExit) as e:
            run_service.main()
    finally:
        sys.stdout, sys.stderr = out, err
    assert e.value.code == 3
    assert len(runs) == 3 + 5  # 前两次快退出，第三次长跑清零，之后再 5 次快退出才放弃


# ---------------- 屏上实况默认关 ----------------

@pytest.fixture
def mgr(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "CONF_DIR", tmp_path)
    monkeypatch.setattr(server, "CONF", tmp_path / "config.json")
    shots = []

    def fake_run(cmd, **kw):
        shots.append(cmd)
        return types.SimpleNamespace(stdout=b"\x89PNG", returncode=0)
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def status(path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base + path, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    yield status, tmp_path, shots
    srv.shutdown()


def test_live_preview_off_by_default_even_when_device_enabled(mgr):
    status, tmp_path, shots = mgr
    (tmp_path / "config.json").write_text(json.dumps({"device_enabled": True}), encoding="utf-8")
    assert status("/api/screen") == 503
    assert shots == []  # 一次 adb 都没跑


def test_live_preview_needs_explicit_true(mgr):
    status, tmp_path, shots = mgr
    (tmp_path / "config.json").write_text(json.dumps({"device_enabled": True, "live_preview": "yes"}), encoding="utf-8")
    assert status("/api/screen") == 503 and shots == []
    (tmp_path / "config.json").write_text(json.dumps({"device_enabled": True, "live_preview": True}), encoding="utf-8")
    assert status("/api/screen") == 200
    assert shots and "screencap" in shots[0]


# ---------------- 安装脚本 ----------------

def test_windows_scripts_are_utf8_bom_crlf():
    for rel in ("desktop/install_windows.ps1", "desktop/windows/Run-RyuoService.ps1"):
        raw = (ROOT / rel).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), rel
        assert b"\n" not in raw.replace(b"\r\n", b""), rel
        text = raw.decode("utf-8-sig")
        assert "pip install" not in text and "--user" not in text, rel  # 环境只由 uv 管


def test_runner_never_syncs_at_start():
    text = (ROOT / "desktop/windows/Run-RyuoService.ps1").read_text(encoding="utf-8-sig")
    assert "'--frozen', '--no-sync'" in text
    assert "device_enabled" in text and "Set-Content" not in text  # 只读配置，不改


PWSH = shutil.which("pwsh")


@pytest.mark.skipif(sys.platform != "win32" or not PWSH, reason="要 Windows 和 PowerShell 7")
def test_installer_default_only_writes_definitions(tmp_path):
    """默认（不加 -Register）只写 XML：任务计划里查不到它，开始菜单也没有快捷方式。"""
    env = {**os.environ, "USERPROFILE": str(tmp_path)}
    hook = tmp_path / "hook.ps1"
    hook.write_text("param($Source, $Reason, $LogPath, [switch]$DryRun)\n", encoding="utf-8")
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "desktop" / "install_windows.ps1"),
                        "-SkipSync", "-TaskPath", "\\RyuoStudioTest\\", "-AdbPath", str(adb), "-FailureHook", str(hook)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    tasks = tmp_path / ".local" / "share" / "ryuo-studio" / "tasks"
    for name in ("pusher", "manager"):
        xml = (tasks / f"RyuoStudio-{name}.xml").read_bytes().decode("utf-16")
        assert "<URI>\\RyuoStudioTest\\RyuoStudio-" + name + "</URI>" in xml
        assert "conhost.exe" in xml and "--headless" in xml and "pwsh.exe" in xml
        assert f"-Name {name}" in xml and "-FailureHook" in xml and "-AdbPath" in xml
        assert "<LogonTrigger>" in xml and "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert not (tmp_path / ".local" / "share" / "ryuo-studio" / "windows-tasks.json").exists()
    q = subprocess.run(["schtasks", "/Query", "/TN", "\\RyuoStudioTest\\RyuoStudio-pusher"], capture_output=True)
    assert q.returncode != 0  # 没注册
