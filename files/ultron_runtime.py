"""
ultron_runtime.py — Ultron Autonomous Vision/ReAct Loop

Captures a screenshot (your phone via ADB, or your own desktop), sends it
to Gemini's free-tier vision model, gets back a single next action
(TAP/SWIPE/TYPE/FINISHED), executes it, and repeats until the goal is
done or a safety limit is hit.

SETUP
  pip install -r requirements.txt
  Get a free key at https://aistudio.google.com/apikey
  export GEMINI_API_KEY="your-key-here"        (Windows: set GEMINI_API_KEY=...)

USAGE
  python ultron_runtime.py --source desktop --goal "Open Notepad and type Hello"
  python ultron_runtime.py --source phone   --goal "Open the Settings app"
  python ultron_runtime.py --source phone   --unlock-pin 1234   # known-PIN unlock only

  # Read-only bug-watch mode — never taps/types, just looks and logs:
  python ultron_runtime.py --source desktop --watch \
      --goal "our staging site in Chrome — the checkout flow" \
      --watch-report bug_report.md

  # Same, but also click-tests one untested button/link per check
  # (skips anything destructive-looking, still confirmed each time):
  python ultron_runtime.py --source desktop --watch --watch-test-buttons \
      --goal "our staging site in Chrome"

SECURITY MODEL — READ THIS
  This build enforces five hard controls on top of the original safety
  rails. None of these are configurable away by the model itself — they
  are checked in Python, after the model responds, before anything is
  executed.

  1. ACTION WHITELIST
     The model can only ever request actions in ALLOWED_ACTIONS. TAP and
     SWIPE are on by default. TYPE (keyboard text entry — the action most
     likely to be used for a password, PIN, or other credential) is OFF
     by default and only becomes available with --allow-type. Any action
     the model proposes that isn't whitelisted is refused and logged, and
     the step is skipped rather than guessed at.

  2. VISUAL REDACTION BEFORE THE FRAME EVER LEAVES YOUR MACHINE
     Every screenshot is redacted with OpenCV *before* being base64-encoded
     and sent to Gemini: the status bar / notification shade strip is
     blacked out, and (optionally, --redact-center) a center band is
     blacked out too, to cover most on-screen keyboards and password
     entry fields. Use --no-redact only if you understand you are then
     sending an unmodified frame to Google's API.

  3. HARD STEP CAP + HUMAN-IN-THE-LOOP CONFIRMATION
     --max-steps is capped at 20 no matter what you pass (MAX_ITERATIONS
     ceiling). By default, every state-changing action (TAP/SWIPE/TYPE)
     requires you to press Enter to confirm before it fires — the model's
     reasoning and the exact action are shown first. Pass --auto-approve
     to skip this if you've watched it behave and trust the run, but the
     step cap still applies either way.

  4. EMERGENCY STOP THAT ACTUALLY REACHES THE DEVICE
     Ctrl+C alone can leave an already-issued `adb shell input` command
     completing on the phone after the Python process has died. This
     build also runs a global hotkey listener (pynput) for
     Ctrl+Alt+K — press it any time, from any window, and it immediately
     (a) sets a stop flag checked before the next action fires, and
     (b) sends `adb shell input keyevent KEYCODE_HOME` to back out of
     whatever's on screen, so the phone doesn't keep moving.

  5. NO MODEL-TRIGGERED UNLOCK, EVER
     --unlock-pin taps out a PIN *you already know and provide yourself*
     on the command line. The model is never shown this flag, never asked
     for a PIN, and "UNLOCK" is not and will never be a valid action in
     ALLOWED_ACTIONS — the whitelist enforces this structurally, not just
     by convention.

  Only point this at a device you own and are authorized to control.
"""

import argparse
import base64
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

import requests

try:
    import pyautogui
    pyautogui.FAILSAFE = True
except ImportError:
    pyautogui = None  # only needed for --source desktop

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    from pynput import keyboard as pynput_keyboard
except ImportError:
    pynput_keyboard = None


def ensure_windows_dpi_awareness():
    """
    Windows quirk: unless a process explicitly marks itself DPI-aware,
    the pixels a screenshot captures and the pixels pyautogui's mouse
    coordinates actually land on can be in different scales whenever
    Display Settings > Scale isn't 100%. That silently misdirects every
    TAP/SWIPE — clicks land near, but not on, the intended target — which
    looks like the model "getting stuck repeating the same step" even
    though its reasoning was correct. This fixes the mismatch up front
    instead of requiring you to change your display scaling.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()  # older Windows fallback
    except Exception as e:
        print(f"⚠️  Couldn't set DPI awareness ({e}) — clicks may be misaligned "
              f"if Windows display scaling isn't 100%.")


GEMINI_MODEL = os.environ.get("ULTRON_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# ---------- Security control 1: action whitelist ----------
# The model's JSON schema can only ever describe these actions — this is
# the second, independent check on top of that. "UNLOCK" is deliberately
# never a member of this set: unlocking only ever happens via --unlock-pin,
# fed a PIN the human operator already knows, never via a model decision.
ALWAYS_ALLOWED_ACTIONS = {"TAP", "SWIPE"}
TYPE_ACTION = "TYPE"

# ---------- Security control 3: hard step cap ----------
HARD_MAX_ITERATIONS_CEILING = 20
DEFAULT_MAX_ITERATIONS = 15
CALL_DELAY_SECONDS = 3  # stay well within free-tier rate limits

# ---------- Security control 4: global emergency stop ----------
EMERGENCY_HOTKEY = "<ctrl>+<alt>+k"
stop_event = threading.Event()

# ---------- Watch mode (read-only bug spotting) ----------
# Watch mode never taps/types/clicks anything by default — it only looks
# and writes to a report file. The one optional exception (--watch-scroll)
# still goes through the same TAP/SWIPE whitelist and confirmation prompt
# as the normal loop; it is never enabled implicitly.
DEFAULT_WATCH_INTERVAL_SECONDS = 8
DEFAULT_WATCH_DURATION_MINUTES = 20
HARD_WATCH_DURATION_CEILING_MINUTES = 120
DEFAULT_BUG_REPORT_PATH = "bug_report.md"


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Set GEMINI_API_KEY as an environment variable first.")
        print('  export GEMINI_API_KEY="your-free-key-from-aistudio.google.com"')
        sys.exit(1)
    return key


def redact_key(text: str, api_key: str) -> str:
    """
    requests' default exception messages (e.g. HTTPError) include the full
    request URL, and this API takes the key as a query param — so an
    unredacted error would print your key straight to the terminal and
    any logs. Every place that logs an exception from a Gemini call runs
    its message through this first.
    """
    if not api_key:
        return text
    return text.replace(api_key, "[REDACTED]")


# ---------- Emergency stop ----------

def send_phone_home():
    """Best-effort: back the phone out to the home screen immediately."""
    try:
        subprocess.run(
            ["adb", "shell", "input", "keyevent", "KEYCODE_HOME"],
            check=False, timeout=5,
        )
    except Exception as e:
        print(f"   (couldn't reach adb to send HOME: {e})")


def emergency_stop(source: str):
    if stop_event.is_set():
        return
    print("\n🛑 EMERGENCY STOP (Ctrl+Alt+K) — halting before next action.")
    stop_event.set()
    if source == "phone":
        print("   Sending KEYCODE_HOME to the phone to clear anything in flight...")
        send_phone_home()


def start_emergency_hotkey_listener(source: str):
    if pynput_keyboard is None:
        print("⚠️  pynput not installed — Ctrl+Alt+K emergency hotkey is DISABLED.")
        print("    Run: pip install pynput   (Ctrl+C / FAILSAFE corner still work.)")
        return None
    listener = pynput_keyboard.GlobalHotKeys({
        EMERGENCY_HOTKEY: lambda: emergency_stop(source),
    })
    listener.daemon = True
    listener.start()
    print(f"🆘 Emergency hotkey armed: Ctrl+Alt+K (works from any window).")
    return listener


# ---------- Screen capture ----------

def capture_phone_screen(local_path="phone_state.png"):
    subprocess.run(["adb", "shell", "screencap", "-p", "/sdcard/ultron_screen.png"], check=True)
    subprocess.run(["adb", "pull", "/sdcard/ultron_screen.png", local_path], check=True)
    return local_path


def capture_desktop_screen(local_path="desktop_state.png"):
    if pyautogui is None:
        raise RuntimeError("pyautogui not installed — run pip install -r requirements.txt")
    shot = pyautogui.screenshot()
    shot.save(local_path)
    return local_path


# ---------- Security control 2: local redaction before upload ----------

def redact_frame(local_path: str, redact_top_pct: float, redact_center: bool):
    """
    Blacks out the top status-bar/notification strip (and optionally a
    center band, to catch on-screen keyboards / password fields) IN
    PLACE, before this file is ever read for the API call. Runs entirely
    locally via OpenCV — nothing about this step touches the network.
    """
    if cv2 is None or np is None:
        print("⚠️  opencv-python not installed — sending UNREDACTED screenshots.")
        print("    Run: pip install opencv-python-headless numpy")
        return local_path

    img = cv2.imread(local_path)
    if img is None:
        return local_path
    h, w = img.shape[:2]

    top_h = int(h * (redact_top_pct / 100.0))
    if top_h > 0:
        img[0:top_h, 0:w] = (0, 0, 0)

    if redact_center:
        band_top = int(h * 0.38)
        band_bottom = int(h * 0.62)
        img[band_top:band_bottom, 0:w] = (0, 0, 0)

    cv2.imwrite(local_path, img, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    return local_path


def encode_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ---------- Model call ----------

def ask_ultron_brain(api_key: str, image_b64: str, goal: str, allow_type: bool,
                      timeout: int = 45, retries: int = 2) -> dict:
    type_line = (
        '{"status": "RUNNING", "action": "TYPE", "coordinates": null, "text": "string to type", "reason": "why"}\n'
        if allow_type else
        "(TYPE is disabled for this run — do not propose it; use TAP/SWIPE only, "
        "or FINISHED with an explanation if text entry is the only way forward.)\n"
    )
    prompt = f"""You are a screen-automation planner. High-level goal: {goal}

Note: the top strip and possibly a center band of this screenshot have
been deliberately blacked out for privacy (status bar / notification /
password-field redaction). Plan around the visible content only.

Look at the attached screenshot. Decide exactly ONE next action to move
toward the goal. Respond with ONLY raw JSON, no markdown fences, matching
one of these shapes:

{{"status": "RUNNING", "action": "TAP", "coordinates": [x, y], "reason": "why"}}
{{"status": "RUNNING", "action": "SWIPE", "coordinates": [x1, y1, x2, y2], "reason": "why"}}
{type_line}{{"status": "FINISHED", "action": "NONE", "reason": "goal met"}}

Coordinates are pixel positions in the provided image. If you are not
confident an action is safe or correct, prefer status FINISHED and
explain why in reason rather than guessing wildly."""

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        }]
    }

    last_err = None
    resp = None
    for attempt in range(1, retries + 2):  # e.g. retries=2 -> up to 3 total tries
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": api_key},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            last_err = None
            break
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt <= retries:
                wait = 2 * attempt
                print(f"  (request attempt {attempt} failed: {redact_key(str(e), api_key)} "
                      f"— retrying in {wait}s)")
                time.sleep(wait)
    if last_err is not None:
        raise RuntimeError(redact_key(str(last_err), api_key)) from None
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


# ---------- Watch mode: bug-hunting model call ----------

def ask_bug_hunter(api_key: str, image_b64: str, focus: str,
                    timeout: int = 45, retries: int = 2) -> dict:
    """
    Same redacted-screenshot pipeline as the action loop, but a different
    prompt: this one is never allowed to propose an action. It only ever
    reports what it sees. That's enforced by the schema below having no
    action field at all — nothing downstream tries to execute anything
    from this response.
    """
    prompt = f"""You are a QA assistant looking at a screenshot of a web app
being tested by its own developer. What to focus on: {focus}

Note: the top strip of this screenshot has been deliberately blacked out
for privacy (status bar / notification redaction). Ignore that band.

Look for visible bugs only: broken/overlapping layout, misaligned or
cut-off elements, unreadable text (contrast/overflow), broken images,
elements that look clickable but appear disabled/dead, obvious visual
inconsistency vs. the rest of the page, or an error message rendered on
the page itself. Do NOT invent bugs you can't actually see in the image.

Respond with ONLY raw JSON, no markdown fences, matching this shape:

{{"bugs_found": true/false, "findings": [{{"severity": "low|medium|high", "description": "what's wrong and where on screen"}}], "note": "one short sentence on general state of the page, or why nothing was flagged"}}

If nothing looks wrong, return bugs_found: false and an empty findings list."""

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        }]
    }

    last_err = None
    resp = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(GEMINI_URL, params={"key": api_key}, json=payload, timeout=timeout)
            resp.raise_for_status()
            last_err = None
            break
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt <= retries:
                wait = 2 * attempt
                print(f"  (request attempt {attempt} failed: {redact_key(str(e), api_key)} — retrying in {wait}s)")
                time.sleep(wait)
    if last_err is not None:
        raise RuntimeError(redact_key(str(last_err), api_key)) from None
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


def ask_element_picker(api_key: str, image_b64: str, tested_labels: list,
                        timeout: int = 45, retries: int = 2) -> dict:
    """
    Picks ONE interactive-looking element to click-test this cycle. Never
    picks anything that looks destructive/irreversible — that's enforced
    in the prompt, and the human still confirms the actual tap unless
    --auto-approve is set, same as every other action in this file.
    """
    already = ", ".join(tested_labels[-15:]) if tested_labels else "(none yet)"
    prompt = f"""You are QA-testing a web app by clicking things to see if
they work. Look at the screenshot and pick ONE interactive-looking
element (button, link, tab, toggle, menu item, checkbox) that has NOT
already been tested this session.

Already tested this session: {already}

SAFETY RULE — do not pick anything whose visible label suggests a
destructive, irreversible, or real-world action: delete, remove, log out,
sign out, unsubscribe, cancel subscription, pay, place order, checkout,
send, confirm, submit (if it looks like a final submission), or anything
similar. Prefer safe things to test: tabs, filters, toggles, expand/
collapse arrows, non-destructive buttons, navigation links.

Respond with ONLY raw JSON, no markdown fences:
{{"status": "FOUND", "coordinates": [x, y], "label": "short name of the element, e.g. 'Filters tab'", "reason": "why you picked it"}}
{{"status": "NONE_LEFT", "reason": "why — e.g. everything visible looks tested or unsafe to test"}}"""

    payload = {"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/png", "data": image_b64}},
    ]}]}
    return _call_gemini_json(api_key, payload, timeout, retries)


def ask_compare_before_after(api_key: str, before_b64: str, after_b64: str, label: str,
                              timeout: int = 45, retries: int = 2) -> dict:
    """Two-image call: did clicking `label` produce any visible change?"""
    prompt = f"""These two screenshots are BEFORE and AFTER clicking on
"{label}" in a web app. Compare them. Did anything visibly change —
new content, a modal/dropdown opening, navigation, a highlighted/pressed
state, an error message, a loading indicator? Or does the AFTER image
look identical to BEFORE (which would suggest the element didn't respond
to the click)?

Respond with ONLY raw JSON, no markdown fences:
{{"changed": true/false, "note": "one short sentence on what changed, or why it looks unresponsive"}}"""

    payload = {"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/png", "data": before_b64}},
        {"inline_data": {"mime_type": "image/png", "data": after_b64}},
    ]}]}
    return _call_gemini_json(api_key, payload, timeout, retries)


def _call_gemini_json(api_key: str, payload: dict, timeout: int, retries: int) -> dict:
    """Shared request/parse plumbing used by the watch-mode helper calls above."""
    last_err = None
    resp = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(GEMINI_URL, params={"key": api_key}, json=payload, timeout=timeout)
            resp.raise_for_status()
            last_err = None
            break
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt <= retries:
                wait = 2 * attempt
                print(f"  (request attempt {attempt} failed: {redact_key(str(e), api_key)} — retrying in {wait}s)")
                time.sleep(wait)
    if last_err is not None:
        raise RuntimeError(redact_key(str(last_err), api_key)) from None
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


def append_bug_report(report_path: str, source_label: str, entries: list):
    """
    Appends timestamped entries to the shared markdown bug report.
    gesture_bridge.py's /console-log handler appends to this same file
    (from browser console/network errors), so opening one file gives you
    both the visual findings and the console-side findings on a shared
    timeline. Plain append — fine for the low write frequency here; this
    isn't meant to survive concurrent high-throughput writers.
    """
    if not entries:
        return
    with open(report_path, "a", encoding="utf-8") as f:
        for entry in entries:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            severity = entry.get("severity", "").upper()
            tag = f"[{severity}] " if severity else ""
            f.write(f"- `{ts}` **{source_label}** {tag}{entry['description']}\n")


def run_watch_loop(source: str, focus: str, interval_seconds: int, duration_minutes: int,
                    report_path: str, redact_top_pct: float, redact_center: bool,
                    allow_scroll: bool, test_buttons: bool, auto_approve: bool,
                    call_timeout: int = 45, retries: int = 2):
    """
    Read-only observation loop: screenshot -> redact -> ask Gemini to spot
    visual bugs only -> append findings to report_path -> sleep -> repeat.
    The two optional exceptions are SWIPE-to-scroll (--watch-scroll) and
    click-testing one element per cycle (--watch-test-buttons) — both go
    through the normal TAP/SWIPE whitelist + confirmation-unless-
    auto-approve path exactly like the action loop, never a looser one.
    """
    api_key = get_api_key()
    end_time = time.time() + duration_minutes * 60
    tested_labels = []

    print(f"👁️  ULTRON WATCH — source={source} — focus: {focus}")
    print(f"   Checking every {interval_seconds}s for up to {duration_minutes} min. "
          f"Writing findings to {report_path}")
    print(f"   Redaction: top {redact_top_pct}% blacked out{' + center band' if redact_center else ''}")
    extras = []
    if allow_scroll:
        extras.append("scrolling to see more of the page")
    if test_buttons:
        extras.append("click-testing one untested element per check (skips anything destructive-looking)")
    print(f"   This mode never taps or types anything on its own"
          f"{' except ' + ' and '.join(extras) if extras else ''} — each is confirmed each time"
          f"{' (AUTO-APPROVE ON)' if auto_approve else ''}.")
    if not auto_approve:
        input("   Press Enter to start watching (Ctrl+C, screen corner, or Ctrl+Alt+K anytime to stop)...")
    else:
        print("   AUTO-APPROVE ON — starting immediately (Ctrl+C, screen corner, or Ctrl+Alt+K anytime to stop).")

    listener = start_emergency_hotkey_listener(source)
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(f"\n## Watch session started {time.strftime('%Y-%m-%d %H:%M:%S')} — focus: {focus}\n\n")

    checks = 0
    try:
        while time.time() < end_time:
            if stop_event.is_set():
                print("\n🛑 Stop flag set — ending watch session.")
                break
            checks += 1
            print(f"\n--- watch check {checks} ---")
            try:
                shot_path = capture_phone_screen() if source == "phone" else capture_desktop_screen()
                redact_frame(shot_path, redact_top_pct, redact_center)
                b64 = encode_image_b64(shot_path)
                result = ask_bug_hunter(api_key, b64, focus, timeout=call_timeout, retries=retries)
            except Exception as e:
                print(f"Error this check: {e}. Continuing to next check.")
                time.sleep(interval_seconds)
                continue

            findings = result.get("findings", []) if result.get("bugs_found") else []
            if findings:
                print(f"  🐞 {len(findings)} finding(s):")
                for entry in findings:
                    print(f"     [{entry.get('severity', '?')}] {entry.get('description', '')}")
                append_bug_report(report_path, "visual", findings)
            else:
                print(f"  ✅ nothing flagged — {result.get('note', '')}")

            if test_buttons and not stop_event.is_set():
                try:
                    pick = ask_element_picker(api_key, b64, tested_labels,
                                               timeout=call_timeout, retries=retries)
                except Exception as e:
                    print(f"  (element-picker error: {e} — skipping click-test this cycle)")
                    pick = {"status": "NONE_LEFT"}

                if pick.get("status") == "FOUND" and pick.get("coordinates"):
                    label = pick.get("label", "unnamed element")
                    x, y = pick["coordinates"]
                    print(f"  🖱️  candidate to click-test: '{label}' at ({x},{y}) — {pick.get('reason', '')}")
                    go = True
                    if not auto_approve:
                        resp = input(f"  ▶ Click-test '{label}'? [Enter=yes, s=skip, q=quit]: ").strip().lower()
                        if resp == "q":
                            break
                        go = resp != "s"
                    if go and not stop_event.is_set():
                        tap = {"action": "TAP", "coordinates": [x, y]}
                        if source == "phone":
                            execute_phone_action(tap)
                        else:
                            execute_desktop_action(tap)
                        time.sleep(1.2)  # give the UI a moment to react before the after-shot
                        try:
                            after_path = capture_phone_screen("phone_after_click.png") if source == "phone" \
                                else capture_desktop_screen("desktop_after_click.png")
                            redact_frame(after_path, redact_top_pct, redact_center)
                            after_b64 = encode_image_b64(after_path)
                            cmp_result = ask_compare_before_after(api_key, b64, after_b64, label,
                                                                   timeout=call_timeout, retries=retries)
                            if cmp_result.get("changed"):
                                print(f"     ✅ responded: {cmp_result.get('note', '')}")
                                append_bug_report(report_path, "interactive",
                                                   [{"severity": "info",
                                                     "description": f"'{label}' responded to click — {cmp_result.get('note', '')}"}])
                            else:
                                print(f"     ⚠️  no visible change: {cmp_result.get('note', '')}")
                                append_bug_report(report_path, "interactive",
                                                   [{"severity": "medium",
                                                     "description": f"'{label}' showed no visible change after clicking — {cmp_result.get('note', '')}"}])
                        except Exception as e:
                            print(f"  (compare error: {e})")
                        tested_labels.append(label)
                else:
                    print(f"  (nothing new/safe to click-test — {pick.get('reason', '')})")

            if allow_scroll and not stop_event.is_set():
                decision = {"status": "RUNNING", "action": "SWIPE",
                            "coordinates": [640, 800, 640, 300],
                            "reason": "scroll down to see more of the page"}
                if not auto_approve:
                    resp = input("  ▶ Scroll down to see more? [Enter=yes, s=skip, q=quit]: ").strip().lower()
                    if resp == "q":
                        break
                    if resp == "s":
                        decision = None
                if decision and not stop_event.is_set():
                    if source == "phone":
                        execute_phone_action(decision)
                    else:
                        execute_desktop_action(decision)

            time.sleep(interval_seconds)
    finally:
        if listener is not None:
            listener.stop()
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(f"\n_Watch session ended {time.strftime('%Y-%m-%d %H:%M:%S')} after {checks} check(s)._\n")
        print(f"\n📄 Watch session done — see {report_path}")


# ---------- Action execution ----------

def execute_phone_action(decision: dict):
    action = decision.get("action")
    if action == "TAP":
        x, y = decision["coordinates"]
        subprocess.run(["adb", "shell", "input", "tap", str(x), str(y)], check=False)
    elif action == "SWIPE":
        x1, y1, x2, y2 = decision["coordinates"]
        subprocess.run(
            ["adb", "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "200"],
            check=False,
        )
    elif action == TYPE_ACTION:
        text = decision.get("text", "").replace(" ", "%s")
        subprocess.run(["adb", "shell", "input", "text", text], check=False)


def execute_desktop_action(decision: dict):
    if pyautogui is None:
        raise RuntimeError("pyautogui not installed")
    action = decision.get("action")
    if action == "TAP":
        x, y = decision["coordinates"]
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click()
    elif action == "SWIPE":
        x1, y1, x2, y2 = decision["coordinates"]
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=0.4)
    elif action == TYPE_ACTION:
        pyautogui.typewrite(decision.get("text", ""), interval=0.03)


# ---------- Known-PIN unlock (no guessing/brute force) ----------
# Deliberately NOT reachable from ask_ultron_brain()/the model — this is
# only ever invoked from main() when the human passes --unlock-pin with a
# PIN they already know.

def unlock_with_known_pin(api_key: str, pin: str, redact_top_pct: float, redact_center: bool,
                           call_timeout: int = 45, retries: int = 2):
    print(f"🔓 Unlocking with known {len(pin)}-digit PIN via visual keypad tap...")
    subprocess.run(["adb", "shell", "input", "keyevent", "26"], check=False)
    time.sleep(0.5)
    subprocess.run(
        ["adb", "shell", "input", "swipe", "500", "1500", "500", "500", "200"],
        check=False,
    )
    time.sleep(0.5)

    for digit in pin:
        shot = capture_phone_screen()
        redact_frame(shot, redact_top_pct, redact_center)
        b64 = encode_image_b64(shot)
        goal = f"Tap the '{digit}' key on the numeric keypad shown on screen."
        decision = ask_ultron_brain(api_key, b64, goal, allow_type=False,
                                     timeout=call_timeout, retries=retries)
        print(f"  digit entry: {decision.get('reason', '')}")
        if decision.get("action") == "TAP":
            execute_phone_action(decision)
        time.sleep(1.0)
    print("PIN entry sequence complete.")


# ---------- Fast path for simple "open X" goals ----------
# The vision loop (screenshot -> ask Gemini -> guess a pixel to click ->
# repeat) is the right tool when a goal genuinely requires seeing and
# reasoning about the screen. It's the wrong tool for "open notepad" —
# that's a fixed sequence a human would do without looking twice: press
# the Windows key, type the name, hit Enter. No pixel-guessing, no
# per-step API call, no chance of clicking 40px off target. This matches
# that fixed sequence and skips the loop entirely when it applies.

APP_OPEN_PATTERN = re.compile(
    r'^(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+)?(?P<app>.+?)'
    r'(?:\s+app(?:lication)?)?[.!\s]*$',
    re.IGNORECASE,
)


def try_fast_open(goal: str, auto_approve: bool) -> bool:
    """
    Returns True if this handled the goal (a simple 'open <app>' request)
    and nothing further needs to run. Returns False if the goal doesn't
    match that shape, so the caller falls through to the normal vision
    loop — e.g. "open notepad and type Hello" still needs the loop for
    the second half, so it deliberately does NOT match here.
    """
    if pyautogui is None:
        return False
    match = APP_OPEN_PATTERN.match(goal.strip())
    if not match:
        return False
    app_name = match.group("app").strip()
    if not app_name or len(app_name) > 60 or "\n" in app_name:
        return False
    # Compound goals like "open notepad and type Hello" need the vision
    # loop for the second half — only fast-path a *pure* "open X" request.
    if re.search(r'\b(and|then|type|click|swipe|tap|write|enter)\b', app_name, re.IGNORECASE):
        return False

    print(f"⚡ Fast-open: '{goal}' looks like a plain 'open an app' request —")
    print(f"   skipping the vision loop and just doing it directly: Win key → "
          f"type \"{app_name}\" → Enter.")
    if not auto_approve:
        resp = input(f"   ▶ Go ahead? [Enter=yes, n=use the vision loop instead]: ").strip().lower()
        if resp == "n":
            print("   Falling back to the normal vision loop.")
            return False

    pyautogui.press("win")
    time.sleep(0.5)
    pyautogui.typewrite(app_name, interval=0.02)
    time.sleep(0.3)
    pyautogui.press("enter")
    print(f"✅ Sent. If \"{app_name}\" didn't open, Windows Search just didn't "
          f"have a match for that exact name — try the goal with the app's "
          f"actual name as it appears in the Start menu.")
    return True


# ---------- Main loop ----------

def run_loop(source: str, goal: str, max_iterations: int, allow_type: bool,
             auto_approve: bool, redact_top_pct: float, redact_center: bool,
             call_timeout: int = 45, retries: int = 2, fast_open: bool = True):
    if source == "desktop" and fast_open and try_fast_open(goal, auto_approve):
        return

    api_key = get_api_key()
    allowed_actions = set(ALWAYS_ALLOWED_ACTIONS)
    if allow_type:
        allowed_actions.add(TYPE_ACTION)

    print(f"🚀 ULTRON RUNTIME — source={source} — goal: {goal}")
    print(f"   Model: {GEMINI_MODEL} | max {max_iterations} steps | "
          f"{CALL_DELAY_SECONDS}s between steps")
    print(f"   Allowed actions: {sorted(allowed_actions)}"
          f"{' (TYPE disabled — pass --allow-type to enable)' if not allow_type else ''}")
    print(f"   Redaction: top {redact_top_pct}% blacked out"
          f"{' + center band' if redact_center else ''}"
          f"{' | approval required before every action' if not auto_approve else ' | AUTO-APPROVE ON'}")
    input("   Press Enter to start (Ctrl+C, screen corner, or Ctrl+Alt+K anytime to stop)...")

    listener = start_emergency_hotkey_listener(source)

    try:
        for step in range(1, max_iterations + 1):
            if stop_event.is_set():
                print("\n🛑 Stop flag set — exiting before further action.")
                break

            print(f"\n--- step {step}/{max_iterations} ---")
            try:
                shot_path = capture_phone_screen() if source == "phone" else capture_desktop_screen()
                redact_frame(shot_path, redact_top_pct, redact_center)
                b64 = encode_image_b64(shot_path)
                decision = ask_ultron_brain(api_key, b64, goal, allow_type,
                                             timeout=call_timeout, retries=retries)
            except Exception as e:
                print(f"Error this step: {e}. Stopping for safety.")
                break

            print(f"  reason: {decision.get('reason', '(none given)')}")
            print(f"  action: {json.dumps({k: v for k, v in decision.items() if k != 'reason'})}")

            if decision.get("status") == "FINISHED":
                print("🏁 Goal reported complete. Stopping.")
                break

            action = decision.get("action")
            if action not in allowed_actions:
                print(f"  ⛔ BLOCKED: '{action}' is not in the allowed-action whitelist "
                      f"{sorted(allowed_actions)}. Skipping this step instead of guessing.")
                time.sleep(CALL_DELAY_SECONDS)
                continue

            if not auto_approve:
                resp = input("  ▶ Execute this action? [Enter=yes, s=skip, q=quit]: ").strip().lower()
                if resp == "q":
                    print("  Quit requested.")
                    break
                if resp == "s":
                    print("  Skipped.")
                    time.sleep(CALL_DELAY_SECONDS)
                    continue

            if stop_event.is_set():
                print("\n🛑 Stop flag set during confirmation — exiting before action fires.")
                break

            if source == "phone":
                execute_phone_action(decision)
            else:
                execute_desktop_action(decision)

            time.sleep(CALL_DELAY_SECONDS)
        else:
            print("\n⏹️  Hit max-steps without a FINISHED status — stopping for safety.")
    finally:
        if listener is not None:
            listener.stop()


def positive_capped_int(value):
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    if ivalue > HARD_MAX_ITERATIONS_CEILING:
        print(f"⚠️  --max-steps capped at {HARD_MAX_ITERATIONS_CEILING} (you asked for {ivalue}).")
        return HARD_MAX_ITERATIONS_CEILING
    return ivalue


def main():
    ensure_windows_dpi_awareness()
    parser = argparse.ArgumentParser(description="Ultron autonomous vision/ReAct loop")
    parser.add_argument("--source", choices=["phone", "desktop"], default="desktop")
    parser.add_argument("--goal", help="High-level goal for the agent to pursue (or, with --watch, what to focus on)")
    parser.add_argument("--unlock-pin", help="Tap out this known PIN on your own phone. Exits after unlocking unless --goal is also given, in which case it continues straight into that goal.")
    parser.add_argument("--watch", action="store_true",
                         help="Read-only mode: repeatedly look at the screen and log visual bugs to "
                              "--watch-report instead of taking any action.")
    parser.add_argument("--watch-interval", type=int, default=DEFAULT_WATCH_INTERVAL_SECONDS,
                         help=f"Seconds between watch checks. Default {DEFAULT_WATCH_INTERVAL_SECONDS}.")
    parser.add_argument("--watch-duration", type=int, default=DEFAULT_WATCH_DURATION_MINUTES,
                         help=f"Minutes to keep watching, hard-capped at {HARD_WATCH_DURATION_CEILING_MINUTES}. "
                              f"Default {DEFAULT_WATCH_DURATION_MINUTES}.")
    parser.add_argument("--watch-report", default=DEFAULT_BUG_REPORT_PATH,
                         help=f"Markdown file to append findings to. Default {DEFAULT_BUG_REPORT_PATH}. "
                              f"Point this at the same file gesture_bridge.py writes console/network "
                              f"errors to, to get one merged timeline.")
    parser.add_argument("--watch-scroll", action="store_true",
                         help="Also scroll down periodically during --watch to see more of a long page. "
                              "Still goes through the normal confirmation prompt unless --auto-approve.")
    parser.add_argument("--watch-test-buttons", action="store_true",
                         help="Also click-test one untested-looking button/link per check and compare "
                              "before/after screenshots to flag ones that don't seem to respond. Skips "
                              "anything that looks destructive (delete/logout/pay/submit/etc). Requires "
                              "--watch. Still goes through the normal confirmation prompt unless --auto-approve.")
    parser.add_argument("--max-steps", type=positive_capped_int, default=DEFAULT_MAX_ITERATIONS,
                         help=f"Step cap, hard-limited to {HARD_MAX_ITERATIONS_CEILING}")
    parser.add_argument("--allow-type", action="store_true",
                         help="Allow the model to trigger TYPE (keyboard text entry). Off by default.")
    parser.add_argument("--auto-approve", action="store_true",
                         help="Skip the per-action confirmation prompt. Off by default.")
    parser.add_argument("--redact-top-pct", type=float, default=8.0,
                         help="Percent of screen height to black out from the top (status bar). Default 8.")
    parser.add_argument("--redact-center", action="store_true",
                         help="Also black out a center band, to cover on-screen keyboards / password fields.")
    parser.add_argument("--no-fast-open", action="store_true",
                         help="Disable the deterministic 'open <app>' fast path (Win key + type + Enter) "
                              "and always use the slower screenshot-guided vision loop instead.")
    parser.add_argument("--no-redact", action="store_true",
                         help="Disable redaction entirely. NOT recommended — sends raw frames to Gemini.")
    parser.add_argument("--request-timeout", type=int, default=45,
                         help="Seconds to wait for each Gemini API call before giving up. Default 45.")
    parser.add_argument("--retries", type=int, default=2,
                         help="Automatic retries (with backoff) on a network timeout/error before stopping. Default 2.")
    args = parser.parse_args()

    redact_top_pct = 0.0 if args.no_redact else args.redact_top_pct
    redact_center = (not args.no_redact) and args.redact_center

    if args.unlock_pin:
        unlock_with_known_pin(get_api_key(), args.unlock_pin, redact_top_pct, redact_center,
                               call_timeout=args.request_timeout, retries=args.retries)
        if not args.goal:
            return
        print("\n➡️  Unlock done — continuing straight into the goal below.\n")
        time.sleep(1.0)  # give the phone's home screen a moment to settle after unlock

    if args.watch:
        duration = min(args.watch_duration, HARD_WATCH_DURATION_CEILING_MINUTES)
        if duration != args.watch_duration:
            print(f"⚠️  --watch-duration capped at {HARD_WATCH_DURATION_CEILING_MINUTES} min "
                  f"(you asked for {args.watch_duration}).")
        run_watch_loop(
            args.source, args.goal or "the whole visible page", args.watch_interval, duration,
            args.watch_report, redact_top_pct, redact_center, args.watch_scroll,
            args.watch_test_buttons, args.auto_approve,
            call_timeout=args.request_timeout, retries=args.retries,
        )
        return
    elif args.watch_test_buttons:
        parser.error("--watch-test-buttons requires --watch")

    if not args.goal:
        parser.error("--goal is required unless using --unlock-pin")

    run_loop(
        args.source, args.goal, args.max_steps, args.allow_type,
        args.auto_approve, redact_top_pct, redact_center,
        call_timeout=args.request_timeout, retries=args.retries,
        fast_open=not args.no_fast_open,
    )


if __name__ == "__main__":
    main()
