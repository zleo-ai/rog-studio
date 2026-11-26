#!/usr/bin/env python3
"""常驻推送：每秒给屏发心跳，同时把额度面板送上屏。

面板有两种送法（config 里的 renderer）：
  native（默认）：屏上跑一个原生绘制的常驻程序（tools/native_panel.py），
      电脑每分钟送一次数据、每 5 秒保活，倒计时在屏上按秒走；换主题或布局时才重发一次 config。
      分屏：面板盖在右边窄条上，底下垫一张同主题的静态图兜底。
      整屏：屏只循环放视频，面板每隔 full_hide_seconds 秒淡入盖在压暗的视频上 full_show_seconds 秒。
  png：主机画好图，按 push_minutes 推上屏（做法甲）。

  python3 -I tools/pusher.py            # 读 ~/.config/ryuo-studio/config.json
  python3 -I tools/pusher.py --once     # 只推一轮就退出（调试用）

config 里 device_enabled=false 时只演练：照样出图、照样组帧，但不调 adb、不开 HID，
只把要做的事打印出来。

文件名不用 ryuo 开头：屏会先去官方预设目录里按「包含」匹配找同名文件。
为什么每轮换一个新文件名：1.0.7 实测，屏上的图片加载器按路径缓存，同名覆盖不刷新，
连「先删再放同名文件」都会显示最早那次的旧图。所以每轮用带时间的新名字，推完重发一次
config，再删掉上一张。重发 config 时左边视频会从头播，图片半秒内换上，没有黑屏。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "driver"))
sys.path.insert(0, str(ROOT / "tools"))
import hostsys  # noqa: E402
import render as render_page  # noqa: E402
from ryuo.device import RyuoDevice  # noqa: E402
from ryuo.transport import FakeTransport  # noqa: E402

CONF = Path.home() / ".config" / "ryuo-studio" / "config.json"
OUT = Path.home() / ".local" / "share" / "ryuo-studio"
SCREEN_DIR = "/sdcard/pcMedia/"
DEFAULTS = {"theme": "starship", "layout": "split", "push_minutes": 15, "device_enabled": False, "renderer": "native",
            "full_show_seconds": 15, "full_hide_seconds": 45, "panel_width": 634, "panel_glass": True,
            "hud": True, "hud_items": ["cpu", "gpu", "mem"]}
NATIVE_TICK_S, NATIVE_DATA_S = 5, 60


_LOG_LOCK = threading.Lock()


def say(msg):
    """一行日志：墙钟 + 开机以来的单调时钟。写完立刻 fsync，电脑硬卡死时还没落盘的内容会丢，卡前最后几行最要紧。"""
    now = time.time()
    line = f"{time.strftime('%m-%d %H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d} [{time.monotonic():.3f}] {msg}\n"
    with _LOG_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()
        try:
            os.fsync(sys.stdout.fileno())
        except (OSError, ValueError):  # 终端、管道上 fsync 不了，不要紧
            pass


@contextmanager
def step(name):
    """记一步的开始、结束和耗时；失败也记下来再往外抛。"""
    say(f"开始：{name}")
    t0 = time.monotonic()
    try:
        yield
    except BaseException as e:
        say(f"失败：{name}，用时 {(time.monotonic() - t0) * 1000:.0f} ms，{type(e).__name__}: {e}")
        raise
    say(f"完成：{name}，用时 {(time.monotonic() - t0) * 1000:.0f} ms")


def timed(obj, attr, name):
    """把对象上的一个方法换成带 step 记录的版本。"""
    fn = getattr(obj, attr)

    def wrapper(*a, **kw):
        with step(name):
            return fn(*a, **kw)
    setattr(obj, attr, wrapper)


def memory():
    """推送进程自己的常驻内存，加上整个服务（含它拉起的 adb、Chrome、编译器等子进程）当前和历史最高占用。"""
    parts = []
    try:
        st = dict(x.split(":", 1) for x in Path("/proc/self/status").read_text().splitlines() if ":" in x)
        parts.append(f"本进程 {int(st['VmRSS'].split()[0]) // 1024}M（最高 {int(st['VmHWM'].split()[0]) // 1024}M）")
        cg = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
        cur, peak = (int((cg / f).read_text()) // 2 ** 20 for f in ("memory.current", "memory.peak"))
        parts.append(f"整个服务 {cur}M（最高 {peak}M）")
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return "，".join(parts) or "内存读不到"


HB_SLOW_MS, HB_SUMMARY_EVERY = 200, 10


class TimedTransport:
    """包在 HID 传输层外面：心跳线程每次写都计时，慢的单独记一行，其余每 10 次汇总一行。"""

    def __init__(self, inner):
        self.inner = inner
        self.times = []

    def write_report(self, report):
        t0 = time.monotonic()
        try:
            self.inner.write_report(report)
        finally:
            ms = (time.monotonic() - t0) * 1000
            if threading.current_thread().name == "ryuo-heartbeat":
                if ms > HB_SLOW_MS:
                    say(f"心跳写入慢：{ms:.0f} ms")
                self.times.append(ms)
                if len(self.times) >= HB_SUMMARY_EVERY:
                    say(f"心跳写入 {len(self.times)} 次：平均 {sum(self.times) / len(self.times):.1f} ms，最长 {max(self.times):.1f} ms")
                    self.times = []
            elif ms > HB_SLOW_MS:
                say(f"HID 写入慢：{ms:.0f} ms")

    def __getattr__(self, name):
        return getattr(self.inner, name)


ALIVE_S = 60  # 每隔这么久记一行「还活着」和内存，电脑卡死后翻日志能看到最后正常的时刻


def watch_heartbeat(hb, seen):
    """心跳在后台线程里跑，出错只记在 hb.errors 里；这里把新错误和定期的存活行写进日志。"""
    err = hb.errors[-1] if hb.errors else None
    if err is not None and err is not seen.get("err"):
        say(f"心跳出错：{type(err).__name__}: {err}（已累计心跳 {hb.ticks} 次）")
        seen["err"] = err
    if time.time() - seen.get("alive", 0) >= ALIVE_S:
        say(f"还活着：心跳累计 {hb.ticks} 次，{memory()}")
        seen["alive"] = time.time()


def conf():
    try:
        return {**DEFAULTS, **json.loads(CONF.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


host_tz = hostsys.host_tz
cpu_temp = hostsys.cpu_temp


def gpu_temp():
    g = hostsys.gpu()
    return g["temp"] if g else None


def sys_stats():
    """屏上左边那排占用：读不到的项不写，屏上显示破折号。"""
    out = {"t": "sys"}
    c = {}
    if (t := hostsys.cpu_temp()) is not None:
        c["temp"] = t
    if (ld := hostsys.cpu_load()) is not None:
        c["load"] = ld
    out["cpu"] = c
    if (g := hostsys.gpu()) is not None:
        out["gpu"] = g
    if (m := hostsys.mem()) is not None:
        out["mem"] = m
    return out


def telemetry():
    t = {}
    if (c := cpu_temp()) is not None:
        t["cpu"] = {"temperature": c}
    if (g := gpu_temp()) is not None:
        t["gpu"] = {"temperature": g}
    return t


def render(theme, view):
    w, h = (634, 1080) if view == "strip" else (2240, 1080)
    out = OUT / "renders" / f"push-{view}.png"
    subprocess.run(hostsys.python_cmd(ROOT / "tools" / "snapshot.py"), check=True, capture_output=True, **hostsys.no_window())
    render_page.render(out, f"theme={theme}&view={view}", w, h)
    return out


def latest_video(theme, kind):
    vids = sorted((OUT / "videos" / theme).glob(f"*-{kind}.mp4"), key=lambda p: p.stat().st_mtime)
    return vids[-1] if vids else None


class Screen:
    """真机和演练两种模式共用一套流程；演练时用假传输层，并且不调 adb。"""

    def __init__(self, live):
        self.live = live
        if live:  # 只有 Maple 允许连接屏幕、装好 udev 规则以后才会走到这里
            from ryuo.transport import open_default
            with step("打开 HID 设备"):
                self.dev = RyuoDevice(TimedTransport(open_default()))
        else:
            self.fake = FakeTransport()
            self.dev = RyuoDevice(self.fake)

    def adb(self, *args):
        cmd = [hostsys.adb(), *args]
        if not self.live:
            say("演练 · 会执行：" + " ".join(cmd))
            return
        with step("adb " + " ".join(args)):
            subprocess.run(cmd, check=True, timeout=120, **hostsys.no_window())

    def frames_sent(self):
        return 0 if self.live else len(self.fake.frames)


def push_round(scr, c, turn, pushed_videos, last, native=False):
    """native=True 且整屏时只放视频（面板由屏上程序叠上去），不用出图。
    原生分屏开了玻璃：屏幕按整屏放视频，窄条由屏上程序半透明叠在右边，宽度随意。"""
    theme, layout = c["theme"], c["layout"]
    glass = native and layout == "split" and c.get("panel_glass")
    if glass:
        layout = "full"
    video_only = native and layout != "split"
    name = None
    if not video_only:
        png = render(theme, "strip" if layout == "split" else "usage")
        name = f"lp-panel-{time.strftime('%m%d%H%M%S')}.png"
        scr.adb("push", str(png), SCREEN_DIR + name)

    video = latest_video(theme, "main" if layout == "split" else "full")
    if video and video.name not in pushed_videos:
        scr.adb("push", str(video), SCREEN_DIR + video.name)
        pushed_videos.add(video.name)

    dev = scr.dev
    dev.set_layout(layout)
    if layout == "split":
        dev.set_media([video.name] if video else [name], zone=0)
        dev.set_media([name], zone=1)
        # 1.0.7 实测：分屏时第三个读数会画到右边窄条上，所以只留两个
        dev.set_slots(["Date&Time", "CPU Temperature"])
    elif video_only:
        dev.set_media([video.name] if video else [], play_mode="Single")
        # 屏幕自带的时间、温度：6 个固定位置，数组下标就是位置，空串是空位。
        # 整屏默认全空（会和浮出的面板叠在一起）；玻璃分屏默认左边两项（第三项会被面板盖住）
        # 开了左边时钟（hud）就由屏上程序自己画，屏幕自带的读数关掉免得叠在一起
        if glass:
            dev.set_slots([] if c.get("hud") else c.get("split_slots", ["Date&Time", "CPU Temperature"]))
        else:
            dev.set_slots(c.get("full_slots", []))
    else:  # 整屏：视频和用量大图轮流放
        dev.set_media(([video.name] if video else []) + [name], play_mode="Cycle" if video else "Single")
        dev.set_slots(["Date&Time", "CPU Temperature", "GPU Temperature"])
    colors = json.loads((ROOT / "themes" / theme / "theme.json").read_text())["colors"]
    dev.set_colors(colors["title"], colors["content"])
    dev.screen.time_zone = host_tz()
    dev.apply()
    if last.get("name"):
        dev.delete_media([last["name"]])
    last["name"] = name
    say(f"第 {turn + 1} 轮：{theme} · {'分屏' if layout == 'split' else '整屏'} · {name or '只放视频'}" + ("" if scr.live else f"（演练，已组 {scr.frames_sent()} 帧）"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    c = conf()
    scr = Screen(live=bool(c["device_enabled"]))
    native = None
    if scr.live:
        import native_panel
        native = native_panel.NativePanel()
        for attr, name in (("sync", "把面板文件同步到屏上"), ("stop", "停掉屏上原生面板"), ("start", "拉起屏上原生面板"),
                           ("connect", "连上原生面板的 socket")):
            timed(native, attr, name)
    say(f"开始推送（进程 {os.getpid()}，父进程 {os.getppid()}，systemd 调用号 {os.environ.get('INVOCATION_ID', '无')}）"
        + ("" if scr.live else "（演练模式：不连屏幕）") + f"，{memory()}")
    pushed, turn, last = set(), 0, {}
    applied, data_at, sent_mode = None, 0.0, None
    seen = {"alive": time.time()}

    def stopped(signum, _frame):  # systemd 停服务发 SIGTERM；记一行再走，日志里才分得清「被停」和「卡死」
        say(f"收到停止信号 {signal.Signals(signum).name}，退出；{memory()}")
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    with scr.dev.heartbeat(telemetry) as hb:
        while True:
            watch_heartbeat(hb, seen)
            c = conf()
            use_native = native is not None and c["renderer"] == "native"
            key = (c["theme"], c["layout"], use_native, bool(c["panel_glass"]), bool(c["hud"]),
                   json.dumps([c.get("split_slots"), c.get("full_slots")]))
            wait = max(1, int(c["push_minutes"])) * 60
            try:
                if use_native:
                    wait = NATIVE_TICK_S
                    if key != applied:  # 换了主题或布局：重发一次 config，窄条先放静态图兜底
                        push_round(scr, c, turn, pushed, last, native=True)
                        turn += 1
                        applied = key
                        with step("准备主题文件"):
                            native_panel.build_bundle(c["theme"])
                        say(f"内存：{memory()}")
                        native.close()  # 下面重连时顺带把新主题的文件同步上去
                        data_at = 0
                    fresh = native.ensure()
                    mode = {"full": c["layout"] != "split", "show": c["full_show_seconds"], "hide": c["full_hide_seconds"],
                            "width": int(c["panel_width"]) if c["panel_glass"] else 634, "glass": bool(c["panel_glass"]),
                            "hud": bool(c["hud"]), "hudItems": list(c["hud_items"])}
                    if mode != sent_mode:  # 只改了浮出节奏：马上送，不用重发 config
                        data_at = 0
                    if fresh or time.time() - data_at >= NATIVE_DATA_S:
                        sent_mode = mode
                        native.send({"t": "state", "theme": native_panel.theme_spec(c["theme"]), "data": native_panel.fresh_data(), "mode": mode})
                        if fresh or data_at == 0:
                            say(f"原生面板已连上：{c['theme']}")
                        data_at = time.time()
                    else:
                        native.send(sys_stats() if c["hud"] else {"t": "ping"})
                else:
                    if native is not None and native.sock is not None:
                        native.send({"t": "hide"})
                        native.close()
                    push_round(scr, c, turn, pushed, last)
                    turn += 1
                    applied = key
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                say(f"这一轮没推成：{e}；稍后再试")
                if native is not None:
                    native.close()
                wait = min(wait, 30)
            if a.once:
                break
            time.sleep(wait)
    scr.dev.close()


if __name__ == "__main__":
    main()
