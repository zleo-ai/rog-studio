#!/usr/bin/env python3
"""编出屏上程序：python3 -I overlay/build.py → overlay/build/panel.dex（两边通用；Linux 上 build.sh 也还在）。

需要 JDK 17、Android 33 的 android.jar 和 d8，默认在 ~/.local/share/ryuo-toolchain/，
也可以用 RYUO_TOOLCHAIN 指到别处。目录结构：jdk-17*/（或用 JAVA_HOME / PATH 上的 javac）、
android.jar、android-14/lib/d8.jar（或 android-14/d8[.bat]）。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_jdk(tc):
    win = sys.platform == "win32"
    javac_name = "javac.exe" if win else "javac"
    jdks = sorted(tc.glob("jdk-17*"))
    if jdks and (jdks[0] / "bin" / javac_name).exists():
        return jdks[0]
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / javac_name).exists():
        return Path(java_home)
    found = shutil.which("javac")
    if found:
        return Path(found).resolve().parent.parent
    return None


def d8_command(tc, jdk, jar, out, class_files):
    win = sys.platform == "win32"
    java = jdk / "bin" / ("java.exe" if win else "java")
    d8_jar = tc / "android-14" / "lib" / "d8.jar"
    if d8_jar.exists():
        return [str(java), "-cp", str(d8_jar), "com.android.tools.r8.D8",
                "--lib", str(jar), "--min-api", "33", "--output", str(out), *class_files]
    d8 = tc / "android-14" / ("d8.bat" if win else "d8")
    if d8.exists():
        return [str(d8), "--lib", str(jar), "--min-api", "33", "--output", str(out), *class_files]
    sys.exit(f"没找到 d8：{d8_jar} 或 {d8}")


def build():
    tc = Path(os.environ.get("RYUO_TOOLCHAIN", Path.home() / ".local" / "share" / "ryuo-toolchain"))
    jdk = find_jdk(tc)
    if not jdk:
        sys.exit(f"没找到 JDK 17：{tc}/jdk-17*（也没找到 JAVA_HOME / PATH 上的 javac）")
    jar = tc / "android.jar"
    if not jar.exists():
        sys.exit(f"没找到 android.jar：{jar}")
    win = sys.platform == "win32"
    javac = jdk / "bin" / ("javac.exe" if win else "javac")
    classes, out = HERE / "build" / "classes", HERE / "build"
    shutil.rmtree(classes, ignore_errors=True)
    classes.mkdir(parents=True)
    extra = {"creationflags": 0x08000000} if win else {}
    subprocess.run([str(javac), "-encoding", "UTF-8", "--release", "11", "-nowarn", "-cp", str(jar), "-d", str(classes),
                    str(HERE / "src" / "RyuoPanel.java")], check=True, **extra)
    env = {**os.environ, "JAVA_HOME": str(jdk), "PATH": str(jdk / "bin") + os.pathsep + os.environ.get("PATH", "")}
    class_files = sorted(map(str, classes.glob("*.class")))
    subprocess.run(d8_command(tc, jdk, jar, out, class_files), check=True, env=env, **extra)
    dex = out / "panel.dex"
    (out / "classes.dex").replace(dex)
    return dex


if __name__ == "__main__":
    print(build())
