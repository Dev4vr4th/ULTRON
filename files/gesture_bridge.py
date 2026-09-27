"""
gesture_bridge.py — Local bridge between the browser hologram UI and
ultron_runtime.py.

The browser can't launch a local Python process directly (browsers are
sandboxed on purpose), so this tiny local-only server sits in between:

  ultron_hologram.html  --POST /gesture-->  gesture_bridge.py  --subprocess-->  ultron_runtime.py

  - "pinch"  → if not already running, starts ultron_runtime.py with the
               goal you set below (or pass --goal on the command line).
  - "spread" → if running, terminates it immediately, then (for phone
               runs) also sends KEYCODE_HOME over adb so nothing left in
               the ADB input queue keeps moving after the process dies.

  Voice ("hold mic / hold V and talk") can also:
  - Start a task with its own spoken goal, or ask for read-only bug-
    watching ("watch for bugs on this page").
  - Send a WhatsApp message: "message Alex on WhatsApp saying I'll be
    late" opens WhatsApp, finds the contact, types the message, then
    STOPS and asks you to say "confirm" or "cancel" before it actually
    sends anything — a second, separate spoken step, not a checkbox.

SECURITY
  - Binds strictly to 127.0.0.1 — never 0.0.0.0 — so nothing on your Wi-Fi
    can reach this server, only processes on this machine.
  - Every request to /gesture and /voice must carry the bridge token
    printed to this console at startup, in an X-Ultron-Token header.
    Without it, ANY tab open in your browser (not just the hologram page)
    could otherwise silently POST here and launch or kill automation.
    Paste the printed token into the hologram page's token field once —
    it's kept in that page's memory only, never written to disk.
  - CORS is restricted to this server's own origin rather than "*".
  - This server only ever runs one automation process at a time.

USAGE
  python gesture_bridge.py --goal "Open the Settings app"
  (then open http://localhost:8765/ in your browser — it's served
  directly by this script)
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import requests

HOST = "127.0.0.1"  # never 0.0.0.0 — keeps this off the local Wi-Fi entirely
PORT = 8765
HTML_PATH = Path(__file__).parent / "ultron_hologram.html"
RUNTIME_SCRIPT = Path(__file__).parent / "ultron_runtime.py"
BUG_REPORT_PATH = Path(__file__).parent / "bug_report.md"
# Same file ultron_runtime.py's --watch mode writes to by default, so
# visual findings and browser console/network findings land on one
# shared, timestamped timeline instead of two separate files.
MAX_CONSOLE_EVENTS_PER_REQUEST = 200  # basic sanity cap on one POST body
GEMINI_MODEL = os.environ.get("ULTRON_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Shared secret the browser page must send back on every /gesture and
# /voice request. Regenerated every time this server starts.
BRIDGE_TOKEN = secrets.token_hex(16)
# localhost and 127.0.0.1 both point at this same machine, but browsers
# treat them as different origins for CORS purposes — accept either,
# since the page can legitimately be loaded via either hostname, while
# still rejecting anything else.
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

current_process = None
goal_text = "Take a screenshot and describe what's currently on screen"
source = "desktop"
runtime_extra_args = []  # e.g. ["--max-steps", "15"] — never includes --allow-type or --auto-approve by default

# Holds {"contact": str, "message": str} between "draft this message" and
# the separate spoken "send it"/"cancel" that must follow before anything
# actually goes out. This — not a checkbox, not auto-approve — is the
# confirmation step for an irreversible, real-world send.
pending_send = None
WHATSAPP_STEP_TIMEOUT_SECONDS = 90


def sanitize_for_prompt(text: str, max_len: int = 300) -> str:
    """
    Strips newlines/control characters out of anything spoken that ends up
    inside a goal string. Two reasons: (1) a stray newline typed into
    WhatsApp's message box sends the message early there (Enter = send),
    so this keeps the drafted text to a single line no matter what the
    transcript contained; (2) it keeps one long ramble from blowing up
    the prompt sent to the model.
    """
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:max_len]


def run_whatsapp_step(extra_args: list, timeout: int = WHATSAPP_STEP_TIMEOUT_SECONDS) -> bool:
    """Runs one phase (compose or send) of the WhatsApp flow and blocks until it exits."""
    cmd = [sys.executable, str(RUNTIME_SCRIPT)] + extra_args
    print(f"👉 running: {' '.join(cmd[1:])}")
    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  ⏱️ WhatsApp step timed out.")
        return False


def start_whatsapp_compose(contact: str, message: str) -> bool:
    """
    Phase 1: open WhatsApp, find the contact, type the message — and
    explicitly stop there. The goal below tells the model not to press
    Enter or tap send; the only actions available to it in this call are
    TAP and TYPE (no action here is "press Enter as a keyboard key"),
    so there is no code path in this phase that can send anything.
    """
    contact = sanitize_for_prompt(contact, 80)
    message = sanitize_for_prompt(message, 500)
    goal = (
        f"Open WhatsApp (the Desktop app if it's installed, otherwise open "
        f"web.whatsapp.com in the default browser). Use the search box to find and "
        f"open the chat with the contact named '{contact}'. Click into the message "
        f"text box at the bottom and type EXACTLY this message, character for "
        f"character, with nothing added or removed: \"{message}\". "
        f"Do NOT press Enter and do NOT tap the send button under any circumstances — "
        f"stop as soon as the text is visibly typed into the box and report the goal "
        f"as finished."
    )
    return run_whatsapp_step([
        "--source", "desktop", "--allow-type", "--auto-approve",
        "--max-steps", "10", "--goal", goal,
    ])


def start_whatsapp_send() -> bool:
    """Phase 2: only ever called after a separate spoken 'confirm'. Taps send, nothing else."""
    goal = (
        "A WhatsApp message is already typed and sitting in the message box, ready "
        "to go. Tap the send button (the paper-plane icon next to the message box) "
        "to send it. Do nothing else — report the goal as finished immediately after."
    )
    return run_whatsapp_step([
        "--source", "desktop", "--auto-approve",
        "--max-steps", "3", "--goal", goal,
    ])


def ask_confirmation_brain(transcript: str) -> str:
    """
    Used only while a send is pending. A separate, narrow classification —
    not the general command schema below — so a pending send can't be
    accidentally reinterpreted as some unrelated new command.
    """
    prompt = f"""The user was just asked to confirm or cancel sending a
drafted WhatsApp message. They said: "{transcript}"

Respond with ONLY one word, no punctuation: CONFIRM, CANCEL, or UNCLEAR."""
    try:
        api_key = get_api_key()
        resp = requests.post(GEMINI_URL, params={"key": api_key},
                              json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
    except Exception:
        return "UNCLEAR"
    if "CONFIRM" in text:
        return "CONFIRM"
    if "CANCEL" in text:
        return "CANCEL"
    return "UNCLEAR"


def append_console_findings(events: list):
    """
    Appends browser console/network findings (from the companion Chrome
    extension) to the same bug_report.md that --watch mode writes visual
    findings to. This endpoint only ever appends text to a local file —
    it never executes anything from the browser's payload.
    """
    if not events:
        return
    with open(BUG_REPORT_PATH, "a", encoding="utf-8") as f:
        for ev in events[:MAX_CONSOLE_EVENTS_PER_REQUEST]:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            kind = str(ev.get("type", "console"))[:40]
            url = str(ev.get("url", ""))[:300]
            message = str(ev.get("message", ""))[:500]
            status = ev.get("status")
            status_part = f" (HTTP {status})" if status else ""
            url_part = f" — {url}" if url else ""
            f.write(f"- `{ts}` **browser/{kind}**{status_part} {message}{url_part}\n")


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set in this terminal's environment")
    return key


def redact_key(text: str, api_key: str) -> str:
    """Same rationale as ultron_runtime.py: never let a raw key reach the terminal/logs."""
    if not api_key:
        return text
    return text.replace(api_key, "[REDACTED]")


def ask_voice_brain(transcript: str) -> dict:
    """
    Sends the spoken transcript to Gemini (text-only, no image) and asks
    for a short spoken reply plus an optional START/STOP command. The API
    key stays server-side here — the browser never sees it.
    """
    prompt = f"""You are ULTRON, a terse voice assistant for a local automation
tool. The user just said, via voice: "{transcript}"

Reply with ONLY raw JSON, no markdown fences, matching one of these shapes:

{{"reply": "short spoken reply (1-2 sentences)", "command": "START", "goal": "the task to automate, in plain English"}}
{{"reply": "short spoken reply (1-2 sentences)", "command": "WATCH", "goal": "what to look for/focus on, in plain English"}}
{{"reply": "short spoken reply (1-2 sentences)", "command": "MESSAGE", "contact": "the contact name exactly as said", "message": "the message content to send, exactly as said"}}
{{"reply": "short spoken reply (1-2 sentences)", "command": "STOP"}}
{{"reply": "short spoken reply (1-2 sentences)", "command": "NONE"}}

Use START only when the user is clearly asking you to go do something on
screen (open an app, click something, automate a task). Use WATCH only
when they want you to just look at the screen and report problems/bugs
without touching anything (e.g. "watch for bugs on this page", "keep an
eye on the site"). Use MESSAGE only when they clearly want to send a
WhatsApp message to someone by name (e.g. "message Alex on WhatsApp
saying I'll be late", "send mom a whatsapp: call me back") — pull out the
contact name and the message content as separately as you can; don't add
words to the message they didn't say. Use STOP only when they clearly
want the running loop to stop. Otherwise NONE — just reply
conversationally. Keep "reply" short and natural, it will be spoken out
loud by the browser."""

    api_key = get_api_key()
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(redact_key(str(e), api_key)) from None
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip().strip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


def start_ultron(goal_override: Optional[str] = None, watch: bool = False):
    global current_process
    if current_process and current_process.poll() is None:
        print("Already running — ignoring start request.")
        return
    active_goal = goal_override or goal_text
    if watch:
        # Deliberately does NOT reuse runtime_extra_args' --auto-approve:
        # watch mode is read-only by construction (no --watch-scroll here),
        # so there's nothing for auto-approve to skip the confirmation on.
        # --auto-approve IS added here for one reason: without it, the
        # subprocess blocks on "Press Enter to start watching..." in
        # gesture_bridge.py's own terminal, which defeats a voice/gesture
        # launch. Safe specifically because this command never adds
        # --watch-scroll or --watch-test-buttons, so there is nothing to
        # confirm in the first place.
        cmd = [sys.executable, str(RUNTIME_SCRIPT), "--source", source, "--watch",
               "--goal", active_goal, "--watch-report", str(BUG_REPORT_PATH), "--auto-approve"]
    else:
        cmd = [sys.executable, str(RUNTIME_SCRIPT), "--source", source, "--goal", active_goal] + runtime_extra_args
    print(f"👉 launching: {' '.join(cmd[1:])}")
    current_process = subprocess.Popen(cmd)


def stop_ultron():
    global current_process
    if current_process and current_process.poll() is None:
        print("✋ SPREAD → terminating running loop.")
        current_process.terminate()
        try:
            current_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            current_process.kill()
        if source == "phone":
            print("   Sending KEYCODE_HOME so nothing queued on the phone keeps moving...")
            subprocess.run(["adb", "shell", "input", "keyevent", "KEYCODE_HOME"], check=False, timeout=5)
    else:
        print("Nothing running — ignoring spread.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep console clean; comment out to see raw HTTP logs

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ultron-Token")

    def _check_token(self) -> bool:
        return self.headers.get("X-Ultron-Token") == BRIDGE_TOKEN

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/ultron_hologram.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(HTML_PATH.read_bytes())
        elif self.path == "/status":
            # Deliberately unauthenticated + no-op read-only: exposes only
            # a running/idle boolean, nothing state-changing or sensitive.
            running = current_process is not None and current_process.poll() is None
            body = json.dumps({"running": running}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def do_POST(self):
        global pending_send
        if self.path not in ("/gesture", "/voice", "/console-log"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        if not self._check_token():
            self.send_response(403)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "missing/invalid X-Ultron-Token"}).encode())
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/console-log":
            # From the companion browser extension: batched console
            # errors/warnings and failed network requests. This handler
            # only ever writes text into bug_report.md — nothing from
            # this payload is ever executed or passed to a shell.
            events = body.get("events", [])
            if isinstance(events, list):
                append_console_findings(events)
                print(f"🌐 logged {min(len(events), MAX_CONSOLE_EVENTS_PER_REQUEST)} browser finding(s)")
            self.send_response(200)
            self._cors()
            self.end_headers()
            return

        if self.path == "/gesture":
            gesture = body.get("gesture")
            if gesture == "pinch":
                if pending_send:
                    print("✋ Ignoring pinch — a WhatsApp send is waiting on a spoken confirm/cancel.")
                else:
                    start_ultron()
            elif gesture == "spread":
                stop_ultron()
            self.send_response(200)
            self._cors()
            self.end_headers()
            return

        # /voice
        transcript = body.get("transcript", "").strip()

        if not transcript:
            decision = {"reply": "I didn't catch that.", "command": "NONE"}

        elif pending_send and (time.time() - pending_send["ts"]) <= WHATSAPP_STEP_TIMEOUT_SECONDS:
            # A message is drafted and waiting on exactly this: a separate
            # spoken confirm or cancel. Nothing else is accepted here —
            # this branch can only ever CONFIRM, CANCEL, or ask again.
            verdict = ask_confirmation_brain(transcript)
            contact = pending_send["contact"]
            if verdict == "CONFIRM":
                print(f"✅ confirmed — sending to {contact}")
                ok = start_whatsapp_send()
                decision = {"reply": "Sent." if ok else "Something went wrong sending that — sorry.",
                            "command": "NONE"}
                pending_send = None
            elif verdict == "CANCEL":
                print(f"🛑 cancelled — not sending to {contact}")
                decision = {"reply": "Okay, cancelled. Nothing was sent.", "command": "NONE"}
                pending_send = None
            else:
                decision = {"reply": f"Say confirm to send to {contact}, or cancel.", "command": "NONE"}
                # left pending_send as-is — same draft, same expiry, ask again

        else:
            if pending_send:
                print("⏱️ pending WhatsApp send expired unconfirmed — discarding draft.")
                pending_send = None
            try:
                decision = ask_voice_brain(transcript)
                print(f'🎤 "{transcript}" → {decision}')
            except Exception as e:
                decision = {"reply": f"Voice brain error: {e}", "command": "NONE"}
                print(f"Voice error: {e}")

            if decision.get("command") == "START":
                start_ultron(goal_override=decision.get("goal"))
            elif decision.get("command") == "WATCH":
                start_ultron(goal_override=decision.get("goal"), watch=True)
            elif decision.get("command") == "STOP":
                stop_ultron()
            elif decision.get("command") == "MESSAGE":
                contact = decision.get("contact", "").strip()
                message = decision.get("message", "").strip()
                if current_process and current_process.poll() is None:
                    decision = {"reply": "Something's already running — say stop first.", "command": "NONE"}
                elif not contact or not message:
                    decision = {"reply": "I didn't catch who to message or what to say — try again?",
                                "command": "NONE"}
                else:
                    print(f"✍️  composing WhatsApp message to '{contact}'")
                    ok = start_whatsapp_compose(contact, message)
                    if ok:
                        pending_send = {"contact": contact, "message": message, "ts": time.time()}
                        decision = {"reply": f'Ready to send to {contact}: "{message}". '
                                              f"Say confirm to send, or cancel.",
                                    "command": "NONE"}
                    else:
                        decision = {"reply": f"Couldn't get that typed out for {contact} — "
                                              f"is WhatsApp open and is that contact findable?",
                                    "command": "NONE"}

        resp_body = json.dumps(decision).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(resp_body)


def main():
    global goal_text, source, runtime_extra_args
    parser = argparse.ArgumentParser(description="Gesture bridge for the Ultron hologram UI")
    parser.add_argument("--goal", default=goal_text, help="Goal to pass to ultron_runtime.py on pinch")
    parser.add_argument("--source", choices=["phone", "desktop"], default=source)
    parser.add_argument("--max-steps", type=int, default=15,
                         help="Forwarded to ultron_runtime.py's --max-steps (hard-capped there at 20)")
    args = parser.parse_args()
    goal_text = args.goal
    source = args.source
    runtime_extra_args = ["--max-steps", str(args.max_steps), "--auto-approve"]
    # --auto-approve here trades the per-step terminal confirmation for
    # gesture/voice control being the confirmation gesture itself; the
    # action whitelist (TAP/SWIPE only, no TYPE) and step cap in
    # ultron_runtime.py still apply unconditionally.

    if not HTML_PATH.exists():
        print(f"Can't find {HTML_PATH.name} next to this script.")
        sys.exit(1)
    if not RUNTIME_SCRIPT.exists():
        print(f"Can't find {RUNTIME_SCRIPT.name} next to this script.")
        sys.exit(1)

    print(f"🌉 Gesture + voice bridge running at http://{HOST}:{PORT}/  (bound to localhost only)")
    print(f"   Pinch will run: --source {source} --goal \"{goal_text}\" --max-steps {args.max_steps}")
    print()
    print(f"🔑 BRIDGE TOKEN (paste this into the hologram page's token field, and into the")
    print(f"   Ultron Bug Watcher browser extension's popup, if you use it): {BRIDGE_TOKEN}")
    print()
    print("   Voice commands can launch with their own spoken goal, ask for read-only")
    print('   bug-watching ("watch for bugs on this page"), send a WhatsApp message')
    print('   (say who and what — it reads it back and waits for "confirm" before')
    print('   actually sending), or stop the loop.')
    print(f"   Browser console/network findings from the extension land in: {BUG_REPORT_PATH}")
    print("   Ctrl+C to stop.\n")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_ultron()
        print("\nBridge stopped.")


if __name__ == "__main__":
    main()
