"""额度面板放哪几家：config.json 里的 quota_slots、读不到额度时如实标「缺」而不编数。
不问 magpie（--stale-minutes 会跳过它），快照用临时文件，不碰真实的额度数据。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import snapshot  # noqa: E402

ROOT = HERE.parent.parent
HOUR = 3600 * 1000


def test_default_slots_without_config(tmp_path):
    assert snapshot.slots(tmp_path / "none.json") == ["claude", "codex", "grok", "cursor"]


def test_slots_from_config(tmp_path):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"theme": "starship", "quota_slots": ["claude", "agy", "grok", "cursor"]}), encoding="utf-8")
    assert snapshot.slots(conf) == ["claude", "agy", "grok", "cursor"]


@pytest.mark.parametrize("bad", ["agy", [], ["claude", "nope"], ["claude", "claude"], ["claude", "codex", "grok", "cursor", "agy"], [1]])
def test_bad_slots_fall_back_to_default(tmp_path, bad):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"quota_slots": bad}), encoding="utf-8")
    assert snapshot.slots(conf) == snapshot.DEFAULT_SLOTS


def test_broken_config_falls_back(tmp_path):
    conf = tmp_path / "config.json"
    conf.write_text("[1, 2", encoding="utf-8")
    assert snapshot.slots(conf) == snapshot.DEFAULT_SLOTS
    conf.write_text("[1, 2]", encoding="utf-8")
    assert snapshot.slots(conf) == snapshot.DEFAULT_SLOTS


def test_every_default_and_agy_is_known():
    assert set(snapshot.DEFAULT_SLOTS) <= set(snapshot.PROVIDERS)
    assert "agy" in snapshot.PROVIDERS
    # agy-cli 的窗口还没接：magpie 里出现 agy-cli 也不能被当成某个窗口的数
    assert not [k for k in snapshot.MAGPIE_MAP if k[0].startswith("agy")]


def test_agy_without_windows_ignored_from_magpie(monkeypatch):
    accounts = [{"agent": "agy-cli", "active": True, "readAt": "2026-11-25T22:20:23+08:00",
                 "windows": [{"name": "7 days", "used": 55, "resetsAt": "2026-12-01T00:00:00+08:00"}]}]
    monkeypatch.setattr(snapshot, "magpie_accounts", lambda: accounts)
    assert snapshot.magpie_windows() == {}


def run_snapshot(tmp_path, slots_conf):
    home = tmp_path / "home"
    (home / ".config" / "ryuo-studio").mkdir(parents=True)
    if slots_conf is not None:
        (home / ".config" / "ryuo-studio" / "config.json").write_text(json.dumps({"quota_slots": slots_conf}), encoding="utf-8")
    now = int(time.time() * 1000)
    snap = {"generated_at_ms": now, "providers": [{"windows": [
        {"id": "anthropic:7d", "used_pct": 40, "resets_at_ms": now + 48 * HOUR, "duration_ms": 168 * HOUR},
        {"id": "openai-codex:primary", "used_pct": 30, "resets_at_ms": now + 72 * HOUR},
        {"id": "xai-oauth:credits:1w", "used_pct": 60, "resets_at_ms": now + 24 * HOUR, "duration_ms": 168 * HOUR},
    ]}]}
    sp = tmp_path / "snap.json"
    sp.write_text(json.dumps(snap), encoding="utf-8")
    out_js, out_json = tmp_path / "data.js", tmp_path / "data.json"
    env = {**os.environ, "RYUO_QUOTA_SNAPSHOT": str(sp), "HOME": str(home), "USERPROFILE": str(home)}
    subprocess.run([sys.executable, "-X", "utf8", "-I", str(ROOT / "tools" / "snapshot.py"), "--stale-minutes", "0",
                    "--out", str(out_js), "--json", str(out_json)], check=True, capture_output=True, env=env)
    return json.loads(out_json.read_text(encoding="utf-8"))


def test_agy_takes_codex_slot_and_says_missing(tmp_path):
    data = run_snapshot(tmp_path, ["claude", "agy", "grok", "cursor"])
    rows = data["rows"]
    assert [r["key"] for r in rows] == ["claude", "agy", "grok", "cursor"]
    agy = rows[1]
    assert agy["missing"] is True and agy["name"] == "Gemini"
    assert "used" not in agy and "resetMs" not in agy  # 不编数
    assert rows[0]["used"] == 40 and rows[2]["used"] == 60
    assert rows[3]["missing"] is True  # Cursor 这份快照里也没有
    assert data["next"]["name"] == "Grok"


def test_default_still_shows_codex(tmp_path):
    data = run_snapshot(tmp_path, None)
    assert [r["key"] for r in data["rows"]] == ["claude", "codex", "grok", "cursor"]
    assert data["rows"][1]["used"] == 30
