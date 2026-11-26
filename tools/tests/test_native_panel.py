"""原生面板（tools/native_panel.py、overlay/build.py、overlay/src/RyuoPanel.java）与原生分屏推送的单元测试。
全部在离线演练模式下跑，不连屏幕、不装工具链。"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import native_panel  # noqa: E402
import pusher  # noqa: E402


def load_overlay_build():
    spec = importlib.util.spec_from_file_location("overlay_build", ROOT / "overlay" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_theme_spec_includes_agy_gemini_logo():
    assert native_panel.LOGOS["agy"] == ("gemini-color.svg", False)
    spec = native_panel.theme_spec("starship")
    assert spec["logos"]["agy"] == {"file": "logos/agy.png", "mono": False}
    assert spec["logos"]["claude"] == {"file": "logos/claude.png", "mono": False}
    assert spec["logos"]["codex"]["mono"] is True


def test_digest_is_stable_and_ignores_bundle_sha(tmp_path):
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "a.ttf").write_bytes(b"font")
    (tmp_path / "panel.dex").write_bytes(b"dex")
    d1 = native_panel.digest(tmp_path)
    (tmp_path / "bundle.sha").write_text("anything", encoding="utf-8")
    assert native_panel.digest(tmp_path) == d1
    (tmp_path / "panel.dex").write_bytes(b"dex2")
    assert native_panel.digest(tmp_path) != d1


def test_overlay_build_finds_jdk_and_prefers_d8_jar(monkeypatch, tmp_path):
    ob = load_overlay_build()
    win = sys.platform == "win32"
    javac_name = "javac.exe" if win else "javac"
    java_name = "java.exe" if win else "java"

    # 1) 优先找 toolchain 里的 jdk-17*
    jdk = tmp_path / "jdk-17.0.15"
    (jdk / "bin").mkdir(parents=True)
    (jdk / "bin" / javac_name).write_bytes(b"")
    (jdk / "bin" / java_name).write_bytes(b"")
    assert ob.find_jdk(tmp_path) == jdk

    # 2) toolchain 里没有时退到 JAVA_HOME
    empty_tc = tmp_path / "empty"
    empty_tc.mkdir()
    monkeypatch.setenv("JAVA_HOME", str(jdk))
    assert ob.find_jdk(empty_tc) == jdk

    # 3) d8.jar 存在时直接用 java -cp d8.jar，两边平台一致
    d8_jar = tmp_path / "android-14" / "lib" / "d8.jar"
    d8_jar.parent.mkdir(parents=True)
    d8_jar.write_bytes(b"")
    cmd = ob.d8_command(tmp_path, jdk, tmp_path / "android.jar", tmp_path / "out", ["A.class"])
    assert cmd[:4] == [str(jdk / "bin" / java_name), "-cp", str(d8_jar), "com.android.tools.r8.D8"]
    assert "--min-api" in cmd and "33" in cmd and cmd[-1] == "A.class"

    # 4) 没有 d8.jar 时退回 d8 / d8.bat
    d8_jar.unlink()
    d8_script = tmp_path / "android-14" / ("d8.bat" if win else "d8")
    d8_script.write_text("", encoding="utf-8")
    cmd2 = ob.d8_command(tmp_path, jdk, tmp_path / "android.jar", tmp_path / "out", ["A.class"])
    assert cmd2[0] == str(d8_script)


def test_native_glass_split_pushes_full_video_without_rendering_png(monkeypatch, tmp_path):
    vid = tmp_path / "starship-ring-full.mp4"
    vid.write_bytes(b"mp4")
    rendered = []
    monkeypatch.setattr(pusher, "render", lambda *a: rendered.append(a))
    monkeypatch.setattr(pusher, "latest_video", lambda theme, kind: vid if (theme, kind) == ("starship", "full") else None)
    monkeypatch.setattr(pusher, "host_tz", lambda: "GMT+08:00")
    said = []
    monkeypatch.setattr(pusher, "say", said.append)
    scr = pusher.Screen(live=False)
    pushed, last = set(), {}
    c = {**pusher.DEFAULTS, "renderer": "native", "layout": "split", "panel_glass": True, "hud": True, "theme": "starship"}
    pusher.push_round(scr, c, 0, pushed, last, native=True)
    assert rendered == []  # 玻璃分屏只放循环视频，不出静态面板 PNG
    assert len(scr.dev.screen.zones) == 1
    assert scr.dev.screen.zones[0].media == ["starship-ring-full.mp4"]
    assert not any(scr.dev.screen.slots)  # 开了 HUD 就关掉屏幕自带读数，免得叠在一起
    assert "starship-ring-full.mp4" in pushed
    assert last["name"] is None


def test_native_glass_split_without_hud_keeps_split_slots(monkeypatch, tmp_path):
    vid = tmp_path / "starship-ring-full.mp4"
    vid.write_bytes(b"mp4")
    monkeypatch.setattr(pusher, "latest_video", lambda theme, kind: vid if kind == "full" else None)
    monkeypatch.setattr(pusher, "host_tz", lambda: "GMT+08:00")
    monkeypatch.setattr(pusher, "say", lambda *a: None)
    scr = pusher.Screen(live=False)
    c = {**pusher.DEFAULTS, "renderer": "native", "layout": "split", "panel_glass": True, "hud": False, "theme": "starship"}
    pusher.push_round(scr, c, 0, set(), {}, native=True)
    assert "Date&Time" in scr.dev.screen.slots and "CPU Temperature" in scr.dev.screen.slots


def test_ryuo_panel_java_retains_missing_rows():
    src = (ROOT / "overlay" / "src" / "RyuoPanel.java").read_text(encoding="utf-8")
    assert 'if (o.optBoolean("missing")) continue;' not in src
    assert 'static final String NA = "暂无数据";' in src
    assert 'if (!r.missing) soon.add(r);' in src
    assert 'text("—"' in src


def test_data_fetcher_loads_cache_and_updates_in_background(tmp_path):
    import json
    import threading
    import time

    cache = tmp_path / "data.json"
    cache.write_text(json.dumps({"generatedAtMs": 1000, "rows": []}), encoding="utf-8")
    gate = threading.Event()

    def slow_fetch():
        gate.wait(timeout=5)
        return {"generatedAtMs": 2000, "rows": [], "tz": "GMT+08:00"}

    f = pusher.DataFetcher(slow_fetch, cache)
    d0, v0, e0 = f.snapshot()
    assert d0["generatedAtMs"] == 1000 and v0 == 1 and e0 is None
    assert f.trigger() is True
    assert f.trigger() is False  # 进行中不会重复拉起
    gate.set()
    for _ in range(50):
        d1, v1, e1 = f.snapshot()
        if v1 > v0:
            break
        time.sleep(0.02)
    assert d1["generatedAtMs"] == 2000 and v1 == 2 and e1 is None

