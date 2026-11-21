#!/usr/bin/env python3
"""Install the EarlyStar APK on a running Android emulator, open it and check it stays up.

    python3 android/smoke.py android/dist/EarlyStar-android.apk

The app is a release build, so the page inside cannot be read from outside. This checks what can be:
the package installs, the window is the app's, the process is still there after 25 s and nothing crashed.
A screenshot (android-emulator.png) is saved next to the APK to look at. Needs adb on PATH.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def adb(*args, check=True):
    result = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=180)
    if check and result.returncode != 0:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def main():
    apk = os.path.abspath(sys.argv[1])
    out = os.path.dirname(apk)
    with open(os.path.join(HERE, "app.json"), encoding="utf-8") as handle:
        package = json.load(handle)["id"]
    failures = []

    def check(condition, label):
        print(("  ok   " if condition else "  FAIL ") + label, flush=True)
        if not condition:
            failures.append(label)

    adb("wait-for-device")
    adb("install", "-r", apk)
    adb("logcat", "-c", check=False)
    adb("shell", "am", "start", "-W", "-n", package + "/.MainActivity")

    first = ""
    for _ in range(30):
        first = adb("shell", "pidof", package, check=False).strip()
        if first:
            break
        time.sleep(1)
    check(bool(first), "the app started (pid %s)" % (first or "-"))
    time.sleep(25)
    later = adb("shell", "pidof", package, check=False).strip()
    check(bool(later) and later == first, "the same process is still there after 25 s (pid %s)" % (later or "-"))
    focus = adb("shell", "dumpsys", "window", check=False)
    focused = [line.strip() for line in focus.splitlines() if "mCurrentFocus" in line or "mFocusedApp" in line]
    check(any(package in line for line in focused), "its window is in front (%s)" % (focused[0] if focused else "no focus line"))
    with open(os.path.join(out, "android-emulator.png"), "wb") as handle:
        handle.write(subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=60).stdout)
    crashes = [line for line in adb("logcat", "-d", "-b", "crash", check=False).splitlines() if package in line]
    check(not crashes, "no crash in logcat")

    if failures:
        print("\n%d check(s) failed" % len(failures))
        sys.exit(1)
    print("\nEarlyStar for Android installs, opens and stays up")


if __name__ == "__main__":
    main()
