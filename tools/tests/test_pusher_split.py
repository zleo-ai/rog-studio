"""图片模式分屏一轮推什么：左边主区、右边窄条各放各的，屏幕自带读数按 config 来。
演练模式（假传输层、不调 adb），出图换成假的，不开浏览器。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pusher  # noqa: E402


def one_round(monkeypatch, tmp_path, conf):
    png = tmp_path / "push-strip.png"
    png.write_bytes(b"png")
    monkeypatch.setattr(pusher, "render", lambda theme, view: png)
    monkeypatch.setattr(pusher, "latest_video", lambda theme, kind: None)
    monkeypatch.setattr(pusher, "host_tz", lambda: "GMT+08:00")
    said = []
    monkeypatch.setattr(pusher, "say", said.append)
    scr = pusher.Screen(live=False)
    pushed, last = set(), {}
    c = {**pusher.DEFAULTS, "renderer": "png", "layout": "split", **conf}
    pusher.push_round(scr, c, 0, pushed, last)
    return scr.dev.screen, pushed, last, said


def test_split_without_video_puts_theme_background_on_the_left(monkeypatch, tmp_path):
    screen, pushed, last, said = one_round(monkeypatch, tmp_path, {"theme": "starship"})
    left, right = screen.zones[0].media, screen.zones[1].media
    assert left == ["lp-main-starship.jpg"]
    assert right == [last["name"]] and right[0].startswith("lp-panel-")
    assert left != right  # 不能拿窄条的图拉伸铺满左边
    assert "lp-main-starship.jpg" in pushed
    pushes = [s for s in said if "push" in s]
    assert any("themes" in s and "main.jpg" in s and "lp-main-starship.jpg" in s for s in pushes)


def test_split_background_pushed_once_per_theme(monkeypatch, tmp_path):
    png = tmp_path / "push-strip.png"
    png.write_bytes(b"png")
    monkeypatch.setattr(pusher, "render", lambda theme, view: png)
    monkeypatch.setattr(pusher, "latest_video", lambda theme, kind: None)
    monkeypatch.setattr(pusher, "host_tz", lambda: "GMT+08:00")
    said = []
    monkeypatch.setattr(pusher, "say", said.append)
    scr = pusher.Screen(live=False)
    pushed, last = set(), {}
    c = {**pusher.DEFAULTS, "renderer": "png", "layout": "split", "theme": "starship"}
    pusher.push_round(scr, c, 0, pushed, last)
    pusher.push_round(scr, c, 1, pushed, last)
    assert sum("lp-main-starship.jpg" in s for s in said if "push" in s) == 1


def test_split_slots_follow_config(monkeypatch, tmp_path):
    screen, *_ = one_round(monkeypatch, tmp_path, {"theme": "starship", "split_slots": []})
    assert not any(screen.slots)
    screen, *_ = one_round(monkeypatch, tmp_path, {"theme": "starship"})
    assert "Date&Time" in screen.slots and "CPU Temperature" in screen.slots
