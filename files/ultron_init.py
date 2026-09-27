"""
ultron_init.py — Ultron Diagnostics & Safe Phone Connect Test

Run this FIRST, before ultron_runtime.py. It verifies that:
  1. pyautogui can control your mouse and take screenshots.
  2. ADB can see a connected Android phone.
  3. (Optional) A basic wake + swipe-to-unlock works on a phone that has
     NO PIN/pattern/password set (swipe-only lock screens only).

Safety notes:
  - Only run this against a phone/computer you own, with USB debugging
    enabled by you. ADB requires the device owner to explicitly authorize
    the debugging connection on the phone itself — that authorization
    prompt is your consent gate, don't bypass or automate past it.
  - This script does NOT guess or brute-force PINs/patterns. The swipe
    unlock only works on devices where the lock screen is "swipe to
    unlock" with no security code at all.
  - pyautogui.FAILSAFE is on: slam your mouse cursor into any screen
    corner at any time to instantly abort.
"""

import subprocess
import sys
import time

try:
    import pyautogui
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

pyautogui.FAILSAFE = True  # Corner-slam kill switch


def check_security_dependencies():
    """
    ultron_runtime.py's local screenshot redaction (OpenCV) and its
    Ctrl+Alt+K global emergency-stop hotkey (pynput) both degrade
    gracefully if missing — but degrade means "sends unredacted frames"
    and "no global hotkey", so check for them up front instead of finding
    out mid-run.
    """
    print("🛡️  Checking security-control dependencies...")
    ok = True
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        print("   OK — opencv/numpy present: screenshot redaction will run.")
    except ImportError:
        print("   MISSING — opencv-python-headless/numpy not installed.")
        print("   Without them, ultron_runtime.py sends UNREDACTED screenshots to Gemini.")
        ok = False
    try:
        import pynput  # noqa: F401
        print("   OK — pynput present: Ctrl+Alt+K emergency hotkey will be armed.")
    except ImportError:
        print("   MISSING — pynput not installed.")
        print("   Without it, only Ctrl+C / mouse-corner FAILSAFE can stop a run.")
        ok = False
    if not ok:
        print("   Run: pip install -r requirements.txt\n")
    else:
        print()
    return ok


def test_mouse_control():
    print("🖱️  Testing local mouse control...")
    for _ in range(2):
        pyautogui.moveRel(40, 0, duration=0.15)
        pyautogui.moveRel(0, 40, duration=0.15)
        pyautogui.moveRel(-40, 0, duration=0.15)
        pyautogui.moveRel(0, -40, duration=0.15)
    print("   OK — mouse driver responds.\n")


def test_screen_capture():
    print("🖥️  Testing screen capture...")
    shot = pyautogui.screenshot()
    shot.save("laptop_test_capture.png")
    print(f"   OK — saved laptop_test_capture.png ({shot.size[0]}x{shot.size[1]})\n")


def list_adb_devices():
    print("📱 Checking for connected Android devices via ADB...")
    try:
        out = subprocess.check_output(
            ["adb", "devices"], stderr=subprocess.STDOUT, timeout=10
        ).decode()
    except FileNotFoundError:
        print("   ADB is not installed or not on your PATH.")
        print("   Install: https://developer.android.com/tools/releases/platform-tools\n")
        return []
    except subprocess.CalledProcessError as e:
        print(f"   ADB error: {e.output.decode()}\n")
        return []

    lines = [l.strip() for l in out.splitlines()[1:] if l.strip()]
    devices = [l.split("\t")[0] for l in lines if l.endswith("device")]
    unauthorized = [l for l in lines if "unauthorized" in l]

    if unauthorized:
        print("   ⚠️  Phone connected but UNAUTHORIZED. Check your phone screen")
        print("      and tap 'Allow' on the USB debugging prompt.\n")
    if devices:
        print(f"   OK — found device(s): {devices}\n")
    else:
        print("   No authorized devices found. Plug in via USB, enable")
        print("   Developer Options → USB Debugging, and tap 'Allow' on the phone.\n")
    return devices


def swipe_unlock_no_pin(device_id: str):
    """
    Wakes the screen and swipes up. Only works if the lock screen has
    NO PIN/pattern/password (pure swipe-to-unlock). If your phone has a
    code set, this will just land on the keypad and stop there —
    it will not attempt to guess it.
    """
    print(f"🔓 Waking + swiping on {device_id} (swipe-only unlock)...")
    subprocess.run(["adb", "-s", device_id, "shell", "input", "keyevent", "26"], check=False)
    time.sleep(0.5)
    subprocess.run(
        ["adb", "-s", device_id, "shell", "input", "swipe", "500", "1500", "500", "500", "200"],
        check=False,
    )
    print("   Done. If a PIN/pattern screen is showing, that's expected —")
    print("   ultron_runtime.py handles authenticated unlocks separately.\n")


def main():
    print("🤖 ULTRON DIAGNOSTICS\n" + "=" * 40 + "\n")
    check_security_dependencies()
    test_mouse_control()
    test_screen_capture()
    devices = list_adb_devices()

    if devices:
        answer = input(
            f"Attempt a swipe-only unlock test on {devices[0]}? "
            "Only do this if that phone has NO PIN set. [y/N]: "
        ).strip().lower()
        if answer == "y":
            swipe_unlock_no_pin(devices[0])

    print("Diagnostics complete. If everything above says OK, you're ready")
    print("for ultron_runtime.py.")


if __name__ == "__main__":
    main()
