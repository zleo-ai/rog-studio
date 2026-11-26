#!/usr/bin/env python3
"""把本机额度快照整理成屏幕要显示的四家用量，写成 data.js 给 render/panel.html 用。

用法：
  python3 -I make_data.py                      # 读真实快照，按现在时间算
  python3 -I make_data.py --stale-minutes 47   # 假装快照已经 47 分钟没更新（画「过期」样子用）
  python3 -I make_data.py --json out.json      # 顺带写一份纯 JSON
"""
import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

# 额度快照：一份 {"generated_at_ms", "providers":[{"windows":[{"id","used_pct","resets_at_ms",...}]}]} 的 JSON，
# 作者本机由另一个采样程序每 15 分钟写一次；别的机器可以用 RYUO_QUOTA_SNAPSHOT 指到自己的文件，没有也行（只靠 magpie）
SNAPSHOT = Path(os.environ.get("RYUO_QUOTA_SNAPSHOT", Path.home() / ".local/state/aios/quota-snapshot.json"))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "render" / "data.js"
LOGO_DIR = ROOT / "themes" / "_shared" / "logos"
# 每家用哪个图标；Claude 保留品牌橙，其余跟随主题文字颜色
LOGO = {"claude": "claude-color.svg", "codex": "codex.svg", "grok": "grok.svg", "cursor": "cursor-mono.svg",
        "agy": "gemini-color.svg"}
STALE_AFTER_MIN = 30
WEEK_MS = 7 * 24 * 3600 * 1000

# 认得的几家：名字、主数字用哪个窗口、窗口说明、小字用哪个窗口
PROVIDERS = {
    "claude": ("Claude", "anthropic:7d", "7 天", ("anthropic:5h", "5 小时")),
    "codex": ("Codex", "openai-codex:primary", "7 天", None),
    "grok": ("Grok", "xai-oauth:credits:1w", "7 天", ("xai-oauth:product:grokbuild:1w", "Build")),
    "cursor": ("Cursor", "cursor:usd:individual-auto", "本月", ("cursor:usd:individual-api", "其他模型")),
    # agy 命令行（面板上显示为 Gemini）：优先直调本机 agy -p /usage --output-format json，magpie 的 plugin:agy-cli 作兜底；
    # 读不到时这一格照样占着，写「暂无数据」。
    "agy": ("Gemini", "agy-cli:primary", "7 天", ("agy-cli:5h", "5 小时")),
}
# 面板上四个位置放谁、按什么顺序。想换掉某一家（比如第二格的 Codex 换成 agy），在 config.json 里写
#   "quota_slots": ["claude", "agy", "grok", "cursor"]
# 删掉这个键就回到默认
DEFAULT_SLOTS = ["claude", "codex", "grok", "cursor"]
CONF = Path.home() / ".config" / "ryuo-studio" / "config.json"
WEEKDAY = "一二三四五六日"


def slots(conf_path=CONF):
    """面板上的几家，按顺序。config.json 里的 quota_slots 写坏了（不是列表、有不认得的、有重复、超过 4 家）就用默认。"""
    try:
        want = json.loads(Path(conf_path).read_text(encoding="utf-8")).get("quota_slots")
    except (OSError, ValueError, AttributeError):
        want = None
    ok = (isinstance(want, list) and 0 < len(want) <= 4 and all(isinstance(k, str) and k in PROVIDERS for k in want)
          and len(set(want)) == len(want))
    return list(want) if ok else list(DEFAULT_SLOTS)

# 四家都优先直接问 magpie（它自己就在读各家订阅的额度）；Gemini 另有一条直调 agy CLI 的路径（更稳）；
# 问不到的窗口（比如 Grok Build）再用本机快照（RYUO_QUOTA_SNAPSHOT 那份）。
# 命令行查额度一次要十几秒，所以结果缓存几分钟，推送程序每分钟要数据时不用每次都问。
MAGPIE_CACHE = Path.home() / ".local/share/ryuo-studio/magpie-accounts.json"
MAGPIE_TTL_S = 240
MAGPIE_MAP = {("claude", "5 hours"): "anthropic:5h", ("claude", "7 days"): "anthropic:7d",
              ("codex", "7 days"): "openai-codex:primary", ("grok", "7 days"): "xai-oauth:credits:1w",
              ("cursor", "Cursor Models"): "cursor:usd:individual-auto", ("cursor", "Other Models"): "cursor:usd:individual-api",
              ("plugin:agy-cli", "Gemini · 7 days"): "agy-cli:primary", ("plugin:agy-cli", "Gemini · 5 hours"): "agy-cli:5h"}
AGY_CACHE = Path.home() / ".local/share/ryuo-studio/agy-usage.json"
AGY_TTL_S = 240
AGY_WINDOW_MAP = {"weekly": ("agy-cli:primary", WEEK_MS), "5h": ("agy-cli:5h", 5 * 3600000)}


def window_start_ms(win, reset_ms):
    if win.get("duration_ms"):
        return reset_ms - win["duration_ms"]
    if win["id"].startswith("cursor:"):
        # 按自然月计费：上一个重置点是上个月同一天同一时刻
        t = time.localtime(reset_ms / 1000)
        y, m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
        d = min(t.tm_mday, calendar.monthrange(y, m)[1])
        return int(time.mktime((y, m, d, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1)) * 1000)
    return reset_ms - WEEK_MS  # Codex 等没写时长的周窗口


def iso_ms(v):
    v = re.sub(r"(\.\d{6})\d+", r"\1", v).replace("Z", "+00:00")  # magpie 给到纳秒，Python 只认到微秒
    return int(datetime.fromisoformat(v).timestamp() * 1000)


def magpie_accounts():
    fresh = MAGPIE_CACHE.exists() and time.time() - MAGPIE_CACHE.stat().st_mtime < MAGPIE_TTL_S
    if not fresh:
        try:
            # 后台服务的 PATH 里没有 ~/.local/bin（Windows 上可能是 %LOCALAPPDATA%\Programs\magpie），按完整路径兜底
            exe = shutil.which("magpie")
            if not exe:
                for cand in (Path.home() / "AppData/Local/Programs/magpie/magpie.com",
                             Path.home() / "AppData/Local/Programs/magpie/magpie.exe",
                             Path.home() / ".local/bin/magpie"):
                    if cand.exists():
                        exe = str(cand)
                        break
            if not exe:
                exe = str(Path.home() / ".local/bin/magpie")
            extra = {"creationflags": 0x08000000} if os.name == "nt" else {}
            out = subprocess.run([exe, "accounts", "--json"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60, **extra).stdout
            json.loads(out)
            MAGPIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MAGPIE_CACHE.with_suffix(".tmp")
            tmp.write_text(out, encoding="utf-8")
            tmp.replace(MAGPIE_CACHE)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # 问不到就用上一份缓存（会按各自的读取时间显示过期），再没有就只用快照
    try:
        return json.loads(MAGPIE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def magpie_windows():
    """把 magpie 的额度换成快照里同样的窗口格式，带上各自读到的时间。"""
    wins = {}
    for acc in magpie_accounts():
        if not acc.get("active") or not acc.get("readAt"):
            continue
        seen = iso_ms(acc["readAt"])
        for w in acc.get("windows") or []:
            wid = MAGPIE_MAP.get((acc.get("agent"), w.get("name")))
            if wid and w.get("used") is not None and w.get("resetsAt"):
                # Cursor 按自然月计费，不写时长，起点由 window_start_ms 推
                dur = None if wid.startswith("cursor:") else 5 * 3600000 if "5 hours" in w["name"] else WEEK_MS
                wins[wid] = {"id": wid, "used_pct": float(w["used"]), "resets_at_ms": iso_ms(w["resetsAt"]),
                             "duration_ms": dur, "observed_at_ms": seen}
    return wins


def find_agy():
    """找本机 agy 命令行；后台任务的 PATH 可能不全，找不到时顺带看常见用户目录。"""
    if found := shutil.which("agy"):
        return found
    for cand in (Path.home() / "AppData/Local/agy/bin/agy.exe",
                 Path.home() / ".local/bin/agy.exe",
                 Path.home() / ".local/bin/agy"):
        if cand.exists():
            return str(cand)
    return None


def parse_agy_usage(stdout):
    """按行扫 agy --output-format json 的输出，找 status=SUCCESS 且 command.name=usage 那条。"""
    for line in (stdout or "").splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("status") == "SUCCESS" and (obj.get("command") or {}).get("name") == "usage":
            return obj
    return None


def agy_usage():
    fresh = AGY_CACHE.exists() and time.time() - AGY_CACHE.stat().st_mtime < AGY_TTL_S
    if not fresh:
        exe = find_agy()
        if exe:
            try:
                env = {**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
                extra = {"creationflags": 0x08000000} if os.name == "nt" else {}
                out = subprocess.run([exe, "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     timeout=30, env=env, **extra).stdout
                rec = parse_agy_usage(out)
                if rec is not None:
                    payload = {"observed_at_ms": int(time.time() * 1000), "usage": rec}
                    AGY_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    tmp = AGY_CACHE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(AGY_CACHE)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # 问不到就退回上一份缓存，没有就返回空
    try:
        return json.loads(AGY_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def agy_windows():
    """把 agy /usage 的 Gemini Models 桶转成与快照相同的窗口格式；忽略禁用或 1970 占位重置时间。"""
    cached = agy_usage()
    if not isinstance(cached, dict):
        return {}
    rec = cached.get("usage") if isinstance(cached.get("usage"), dict) else None
    if not rec:
        return {}
    seen = int(cached.get("observed_at_ms") or 0)
    groups = ((rec.get("command") or {}).get("data") or {}).get("groups") or []
    wins = {}
    for g in groups:
        if not isinstance(g, dict) or g.get("name") != "Gemini Models":
            continue
        for b in g.get("buckets") or []:
            if not isinstance(b, dict) or b.get("disabled"):
                continue
            mapped = AGY_WINDOW_MAP.get(b.get("window"))
            if not mapped:
                continue
            rem = b.get("remaining_fraction")
            reset_raw = b.get("reset_time")
            if rem is None or not reset_raw:
                continue
            try:
                reset_ms = iso_ms(str(reset_raw))
                used_pct = round(max(0.0, min(1.0, 1.0 - float(rem))) * 100, 2)
            except (TypeError, ValueError):
                continue
            if reset_ms <= 0:  # 未激活窗口会给 1970-01-01T00:00:00Z 占位，不当成有效重置时间
                continue
            wid, dur = mapped
            wins[wid] = {"id": wid, "used_pct": used_pct, "resets_at_ms": reset_ms,
                         "duration_ms": dur, "observed_at_ms": seen}
    return wins


def fmt_reset(reset_ms, now_ms):
    t = time.localtime(reset_ms / 1000)
    n = time.localtime(now_ms / 1000)
    hm = time.strftime("%H:%M", t)
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        day = "今天"
    elif reset_ms - now_ms < 2 * 86400000 and (t.tm_yday - n.tm_yday) % 365 == 1:
        day = "明天"
    else:
        day = f"{t.tm_mon}/{t.tm_mday} 周{WEEKDAY[t.tm_wday]}"
    return f"{day} {hm}"


def fmt_left(ms):
    h = ms / 3600000
    if h < 1:
        return f"{max(1, round(h * 60))} 分钟"
    if h < 48:
        return f"{round(h)} 小时"
    return f"{int(h // 24)} 天"


def pace_word(used, elapsed):
    if elapsed < 10:
        return "刚重置", "calm"
    r = used / elapsed if elapsed else 0
    if r > 1.25:
        return "偏快", "hot"
    if r < 0.7:
        return "有富余", "calm"
    return "正好", "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-minutes", type=int, default=None)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--json", help="另外把数据写成纯 JSON（屏上原生面板用）")
    a = ap.parse_args()

    try:
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snap = {"generated_at_ms": 0, "providers": []}
    # 快照里只认探针成功的那次时间；单个窗口有自己读取时间的（magpie 合进去的）按它自己的算
    snap_gen = snap.get("generated_at_ms", 0)
    wins = {w["id"]: {**w, "observed_at_ms": w.get("observed_at_ms") or snap_gen}
            for p in snap.get("providers", []) for w in p["windows"]}
    if a.stale_minutes is None:
        wins.update(magpie_windows())
        wins.update(agy_windows())
    picks = [(k, *PROVIDERS[k]) for k in slots()]
    main_ids = [pick[2] for pick in picks if pick[2] in wins]
    seen = [wins[i]["observed_at_ms"] for i in main_ids]
    gen = max(seen) if seen else snap_gen  # 整块面板的「更新时间」看最新的那家，个别过期的在那一行里单独标
    now = gen + a.stale_minutes * 60000 if a.stale_minutes is not None else int(time.time() * 1000)

    rows = []
    for key, name, wid, sub, second in picks:
        w = wins.get(wid)
        if not w or w.get("used_pct") is None:
            rows.append({"key": key, "name": name, "sub": sub, "missing": True})
            continue
        reset = w["resets_at_ms"]
        start = window_start_ms(w, reset)
        elapsed = max(0.0, min(100.0, (now - start) / (reset - start) * 100))
        used = float(w["used_pct"])
        word, tone = pace_word(used, elapsed)
        if (gen - w["observed_at_ms"]) / 60000 > STALE_AFTER_MIN:  # 这一家单独没更新，比如 Cursor 那条探针坏了
            sub = "停在 " + time.strftime("%H:%M", time.localtime(w["observed_at_ms"] / 1000))
        if used >= 85:
            tone = "hot"
        row = {
            "key": key, "name": name, "sub": sub,
            "used": round(used), "usedExact": used,
            "elapsed": round(elapsed, 1),
            "left": round(100 - used),
            "resetAt": fmt_reset(reset, now), "resetIn": fmt_left(reset - now), "resetMs": reset, "startMs": start,
            "pace": word, "tone": tone,
        }
        if second and second[0] in wins:
            row["second"] = {"label": second[1], "used": round(float(wins[second[0]]["used_pct"]))}
        rows.append(row)

    age_min = (now - gen) / 60000
    nxt = min((r for r in rows if not r.get("missing")), key=lambda r: r["resetMs"])
    data = {
        "generatedAtMs": gen,
        "generatedAt": time.strftime("%H:%M", time.localtime(gen / 1000)),
        "generatedDate": time.strftime("%m-%d", time.localtime(gen / 1000)),
        "now": time.strftime("%H:%M", time.localtime(now / 1000)),
        "ageMin": round(age_min),
        "stale": age_min > STALE_AFTER_MIN,
        "next": {"name": nxt["name"], "resetIn": nxt["resetIn"], "left": nxt["left"]},
        "rows": rows,
    }
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    logos = {}
    for key, fname in LOGO.items():
        f = LOGO_DIR / fname
        if f.exists():  # 开源版不带图标，缺了就只显示文字
            logos[key] = f.read_text(encoding="utf-8").replace('height="1em"', "").replace('width="1em"', "")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    themes = {}
    for tj in sorted((ROOT / "themes").glob("*/theme.json")):
        t = json.loads(tj.read_text(encoding="utf-8"))
        themes[t["id"]] = {"layout": t["layout"], "title": t["colors"]["title"], "content": t["colors"]["content"],
                           "labels": t.get("labels"), "series": t.get("series"), "scrim": t.get("scrim", 0.74)}
    Path(a.out).write_text("window.SNAP = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"
                           + "window.LOGOS = " + json.dumps(logos, ensure_ascii=False) + ";\n"
                           + "window.THEMES = " + json.dumps(themes, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
