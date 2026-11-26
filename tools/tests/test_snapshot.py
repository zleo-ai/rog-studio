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
    assert snapshot.PROVIDERS["agy"] == ("Gemini", "agy-cli:primary", "7 天", ("agy-cli:5h", "5 小时"))


def test_magpie_plugin_agy_cli_windows(monkeypatch):
    accounts = [
        {"agent": "agy-cli", "active": True, "readAt": "2026-11-25T22:20:23+08:00",
         "windows": [{"name": "7 days", "used": 55, "resetsAt": "2026-12-01T00:00:00+08:00"}]},
        {"agent": "plugin:agy-cli", "active": True, "readAt": "2026-11-26T22:45:22+08:00",
         "windows": [
             {"name": "Gemini · 7 days", "used": 13.4, "resetsAt": "2026-12-03T07:07:30Z"},
             {"name": "Gemini · 5 hours", "used": 15.8, "resetsAt": "2026-11-26T17:07:30Z"},
         ]},
    ]
    monkeypatch.setattr(snapshot, "magpie_accounts", lambda: accounts)
    wins = snapshot.magpie_windows()
    assert wins["agy-cli:primary"]["used_pct"] == 13.4
    assert wins["agy-cli:primary"]["duration_ms"] == snapshot.WEEK_MS
    assert wins["agy-cli:5h"]["used_pct"] == 15.8
    assert wins["agy-cli:5h"]["duration_ms"] == 5 * 3600000


def test_agy_windows_parses_usage_json_and_filters_placeholder(monkeypatch, tmp_path):
    cache = tmp_path / "agy-usage.json"
    monkeypatch.setattr(snapshot, "AGY_CACHE", cache)
    monkeypatch.setattr(snapshot, "find_agy", lambda: "/fake/agy")
    usage_line = json.dumps({
        "status": "SUCCESS",
        "num_turns": 0,
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.86,
                             "reset_time": "2026-12-03T07:07:30Z"},
                            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.83,
                             "reset_time": "2026-11-26T17:07:30Z"},
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.5,
                             "reset_time": "2026-12-02T14:23:23Z"},
                        ],
                    },
                ],
            },
        },
    })
    seen_env = {}

    def fake_run(cmd, **kw):
        seen_env.update(kw.get("env") or {})
        assert cmd == ["/fake/agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"]
        return type("R", (), {"stdout": "log line\n" + usage_line + "\n"})()

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
    wins = snapshot.agy_windows()
    assert seen_env.get("AGY_CLI_DISABLE_AUTO_UPDATE") == "true"
    assert wins["agy-cli:primary"]["used_pct"] == 14.0
    assert wins["agy-cli:primary"]["duration_ms"] == snapshot.WEEK_MS
    assert wins["agy-cli:5h"]["used_pct"] == 17.0
    assert wins["agy-cli:5h"]["duration_ms"] == 5 * 3600000
    assert cache.exists()


def test_agy_windows_skips_epoch_and_disabled(monkeypatch, tmp_path):
    cache = tmp_path / "agy-usage.json"
    payload = {
        "observed_at_ms": int(time.time() * 1000),
        "usage": {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [{
                        "name": "Gemini Models",
                        "buckets": [
                            {"window": "weekly", "remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                            {"window": "5h", "remaining_fraction": 0.5, "reset_time": "2026-11-26T17:07:30Z", "disabled": True},
                        ],
                    }],
                },
            },
        },
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(snapshot, "AGY_CACHE", cache)
    assert snapshot.agy_windows() == {}


def test_agy_windows_missing_binary_or_timeout_falls_back(monkeypatch, tmp_path):
    cache = tmp_path / "agy-usage.json"
    monkeypatch.setattr(snapshot, "AGY_CACHE", cache)
    monkeypatch.setattr(snapshot, "find_agy", lambda: None)
    assert snapshot.agy_windows() == {}

    # 已有旧缓存但超时：退回旧缓存而不是崩
    old = {
        "observed_at_ms": int(time.time() * 1000) - 600000,
        "usage": {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [{"name": "Gemini Models", "buckets": [
                {"window": "weekly", "remaining_fraction": 0.75, "reset_time": "2026-12-03T07:07:30Z"}
            ]}]}},
        },
    }
    cache.write_text(json.dumps(old), encoding="utf-8")
    os.utime(cache, (time.time() - 1000, time.time() - 1000))
    monkeypatch.setattr(snapshot, "find_agy", lambda: "/fake/agy")

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="agy", timeout=30)

    monkeypatch.setattr(snapshot.subprocess, "run", boom)
    wins = snapshot.agy_windows()
    assert wins["agy-cli:primary"]["used_pct"] == 25.0


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
