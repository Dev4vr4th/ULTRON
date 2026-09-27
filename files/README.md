# Ultron — Local Autonomous Screen/Phone Agent

## What this is (and isn't)

This is a personal accessibility/automation tool for your **own** devices
and accounts — it clicks and types things on the desktop/phone you run it
on, and sends WhatsApp messages through **your own** logged-in WhatsApp.
It's not a bot framework, and it isn't built or intended for automating
someone else's account, bulk messaging, or anything that gets around a
platform's own restrictions on automated use. If you publish or fork
this, keep it that way.

A free, local Python agent that watches a screen (your desktop or your own
Android phone over ADB), decides the next action with Gemini's free-tier
vision model, and executes it — looping until the goal is done.

This is the "Ultron" half of the JARVIS/Ultron blueprint. JARVIS (the
n8n Telegram assistant) is a separate, no-code build — say the word and
I'll put together the importable n8n workflow JSON next.

## What's here

| File | Purpose |
|---|---|
| `ultron_init.py` | Run first. Tests mouse control, screen capture, and ADB phone detection. |
| `ultron_runtime.py` | The autonomous loop: screenshot → ask Gemini → act → repeat. |
| `ultron_hologram.html` | The Iron Man–style HUD: webcam + MediaPipe hand tracking driving a Three.js orb. |
| `gesture_bridge.py` | Local server that connects the browser HUD to `ultron_runtime.py` — pinch launches it, spread kills it. Also collects browser console/network findings from the extension below. |
| `ultron_voice_daemon.py` | Optional. Background voice control with no browser needed — a global hotkey, your mic, and the same `/voice` logic as the hologram, minus the window-visibility requirement. |
| `requirements.txt` | Python dependencies. |
| `ultron_bug_watcher_extension/` | Optional Chrome extension. Relays console errors, uncaught exceptions, and failed requests from one site you choose into the same bug report. |
| `bug_report.md` | Created the first time you run `--watch` or use the extension. One merged, timestamped list of everything found — visually and in the browser console. |

## Setup (Windows, Core i3 laptop — matches your hardware)

Follow these in order. Each one only needs doing once.

1. **Install Python** — [python.org/downloads](https://www.python.org/downloads/). On the first install screen, check **"Add Python to PATH"** before clicking Install.
2. **Install ADB** (only needed for `--source phone`) — [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools), unzip it somewhere permanent (e.g. `C:\platform-tools`), then add that folder to your Windows PATH (Settings → System → About → Advanced system settings → Environment Variables → Path → New).
3. **Enable USB debugging on your phone** (skip if you're only doing desktop) — Settings → About Phone → tap Build Number 7×, then Developer Options → USB Debugging → on. Plug in via USB and tap "Allow" on the authorization prompt that appears on the phone — that prompt is what keeps this tool from touching a phone you don't control.
4. **Put all six files** (`ultron_init.py`, `ultron_runtime.py`, `ultron_hologram.html`, `gesture_bridge.py`, `README.md`, `requirements.txt`) **and the `ultron_bug_watcher_extension` folder** in one folder, e.g. `C:\Users\<you>\ULTRON`.
5. **Install Python deps** — open PowerShell in that folder and run:
   ```
   pip install -r requirements.txt
   ```
6. **Get a free Gemini API key** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey), then set it as an environment variable:
   ```
   setx GEMINI_API_KEY "your-key-here"      # permanent — close and reopen PowerShell after this
   $env:GEMINI_API_KEY = "your-key-here"    # current PowerShell session only
   ```
7. **Run the diagnostics** to confirm mouse control, screen capture, and (if used) phone detection all work:
   ```
   python ultron_init.py
   ```
   Everything should say `OK`. If opencv/pynput say `MISSING`, re-run step 5 — `pip install -r requirements.txt` again.

You're now set up. What you run next depends on what you want to do:

| I want to... | Run this |
|---|---|
| Have it do a task on my desktop | `python ultron_runtime.py --source desktop --goal "Open Notepad and type Hello"` |
| Have it do a task on my phone | `python ultron_runtime.py --source phone --goal "Open the Settings app"` |
| Watch our site for visual bugs, read-only | `python ultron_runtime.py --watch --goal "our staging site in Chrome"` |
| ...and also click-test buttons/links | add `--watch-test-buttons` to the line above |
| Control any of the above hands-free (voice/gesture/webcam orb) | `python gesture_bridge.py --goal "your default goal"` then open `http://localhost:8765/` |
| Catch console errors + hidden text on our site too | Load the extension (see below) *while* watch mode or the bridge is running |

### Setting up the browser extension (5 minutes, once)

1. Open Chrome → address bar → type `chrome://extensions` → Enter.
2. Toggle **Developer mode** on (top-right corner).
3. Click **Load unpacked** → select the `ultron_bug_watcher_extension` folder.
4. It'll appear in your toolbar (puzzle-piece icon → pin it if you don't see it).
5. Start `gesture_bridge.py` first (see above) — it prints a **bridge token** in the terminal.
6. Click the extension's icon → paste that token into **Bridge token** → set **Watch this site** to your team's URL, e.g. `https://staging.yoursite.com/` → check **Actively watching** → **Save**.

That's it — it now sits quietly until you actually browse to a page under that URL, then reports console errors, failed requests, hidden text, and dead-looking controls into `bug_report.md`, on the same timeline as anything the vision side finds.

## Running it

```
python ultron_init.py
```
Confirms mouse control, screen capture, and phone detection work before you trust it with anything autonomous.

```
python ultron_runtime.py --source desktop --goal "Open Notepad and type Hello"
python ultron_runtime.py --source phone   --goal "Open the Settings app"
```

Every step prints the model's reasoning and the exact action before it fires, and the loop hard-stops after 25 steps even if it never reports done.

## Unlocking your own phone

- If your lock screen is plain swipe-to-unlock (no code), `ultron_init.py` can do that for you — it's a wake+swipe, nothing more.
- If it has a PIN, `ultron_runtime.py --unlock-pin 1234` will tap out **a PIN you already know and supply**. It locates the keypad visually and taps the digits you gave it — it does not guess, brute-force, or try to recover a PIN you don't know. Don't point this at a phone that isn't yours.

## Safety rails already built in

- **pyautogui.FAILSAFE** — slam the mouse into any screen corner to instantly kill a desktop run.
- **Step cap, hard-limited to 20** — `--max-steps` (default 15) can't be raised past 20 no matter what you pass.
- **CALL_DELAY_SECONDS = 3** — keeps you inside Gemini's free-tier rate limit and gives you time to watch what's happening.
- **Confirmation prompt** before the loop starts, and (by default) before every action.
- Every action is logged to the terminal before it executes.

## Security controls

These are checked in Python, after the model responds and before anything executes — the model can't configure its way around them.

| Risk | Control |
|---|---|
| Model could try to do anything, including credential entry | **Action whitelist.** Only `TAP`/`SWIPE` run by default. `TYPE` (keyboard text entry — the highest-risk action for passwords/PINs) is off unless you pass `--allow-type`. Anything else the model proposes is refused and the step is skipped. `UNLOCK` is not a valid action *at all* — unlocking only ever happens via `--unlock-pin`, fed a PIN **you** already know on the command line; the model is never shown that flag. |
| Screenshots may contain notifications, banking apps, on-screen keyboards | **Local redaction before upload.** Every screenshot is processed with OpenCV, entirely on your machine, before being sent to Gemini: the top status-bar/notification strip is blacked out (`--redact-top-pct`, default 8%), and `--redact-center` also blacks out a center band to cover on-screen keyboards and password fields. `--no-redact` disables this — not recommended. |
| An unmonitored loop could misread the screen and run away | **Step cap + human-in-the-loop.** `--max-steps` (capped at 20) plus a per-action confirmation prompt by default — you see the model's reasoning and the exact action before it fires. Pass `--auto-approve` to skip the prompt once you trust a run; the whitelist and step cap still apply either way. |
| `gesture_bridge.py`'s control port reachable from other devices | **Bound strictly to `127.0.0.1`**, never `0.0.0.0` — nothing else on your Wi-Fi can reach it. |
| Any browser tab (not just the hologram page) could hit the bridge's `/gesture` or `/voice` endpoints | **Bridge token.** `gesture_bridge.py` prints a random token on startup; `/gesture` and `/voice` reject any request missing a matching `X-Ultron-Token` header. Paste the printed token into the hologram page's token field once — kept in page memory only, never written to disk. CORS is also scoped to the bridge's own origin instead of `*`. |
| `Ctrl+C` can leave a queued `adb shell input` command finishing after Python has already exited | **Global emergency hotkey.** `Ctrl+Alt+K` works from any window (via `pynput`) — it sets a stop flag checked before the next action, and for phone runs immediately sends `adb shell input keyevent KEYCODE_HOME` to back out of whatever's on screen. `gesture_bridge.py`'s spread-gesture kill switch does the same HOME-keyevent step after terminating the process. |
| `--watch` mode misreading the page and clicking/typing something unintended | **No action capability by default.** The two opt-in exceptions (`--watch-scroll`, `--watch-test-buttons`) still require confirmation unless `--auto-approve`, are limited to TAP/SWIPE like everything else, and the button-tester is explicitly told to skip anything destructive-looking. |
| Voice-sent WhatsApp message going out wrong, or to the wrong person | **Two separate spoken steps.** Compose types the message and explicitly stops before Enter/send; nothing goes out until a second, separate "confirm" — a misheard or ambiguous reply is treated as neither, not as a yes. The confirmation window also expires after 90s. |
| Extension's `/console-log` endpoint being hit by something other than the extension | Same bridge-token check as `/gesture` and `/voice` — a missing/wrong `X-Ultron-Token` gets a 403. The extension only reports on the one site prefix you configure, and its background script's bridge URL is hardcoded to `127.0.0.1`/`localhost`, not settable from its own popup. |

If you want it more cautious still, lower `--max-steps` further, or never pass `--auto-approve`/`--allow-type` at all.

## The hologram UI + hand tracking

This is a single HTML file — no Next.js, no npm, no build step, just open
it (or better, load it through the bridge server below). It uses:

- **Three.js** (loaded from a CDN) for the glowing wireframe orb, particle
  ring, and pulse/burst animations.
- **MediaPipe Tasks Vision — HandLandmarker** (also from a CDN) for hand
  tracking. This runs entirely in your browser; no video leaves your
  machine.

**Gesture logic:** it tracks the distance between your thumb tip and index
tip. Bring them together (pinch) → fires a "launch" event. Open your hand
back up (spread) → fires a "stop" event. Both have a ~1.8s cooldown so one
gesture doesn't fire a dozen times.

**Why you need `gesture_bridge.py` too:** a browser page can't launch a
local Python process on its own — that's a deliberate browser security
boundary, not a bug. `gesture_bridge.py` is a small local-only server that
sits in between: it serves the HTML page, listens for pinch/spread events,
and starts or kills `ultron_runtime.py` for you.

**Running it:**
```
python gesture_bridge.py --goal "Open the Settings app"
```
Then open **http://localhost:8765/** in your browser (not the `.html`
file directly — loading it through the server avoids webcam-permission
quirks some browsers have with `file://` pages). Click "Activate Camera",
allow webcam access, and pinch to launch, spread to stop.

You can run `ultron_hologram.html` by itself (just double-click it) if you
only want the orb + hand tracking with no automation wired up — the
"Local Bridge" status readout will just show OFFLINE and the gestures
won't do anything beyond the visual feedback.

**Customizing the goal:** `--goal` is fixed for the whole session right
now — every pinch launches the same task. If you want different gestures
to trigger different goals (e.g. left hand vs. right hand, or a third
gesture), that's a natural next step in `ultron_hologram.html`'s
`handleGesture()` function and `gesture_bridge.py`'s `/gesture` handler.

## Voice — listen and talk back

Hold the mic button (or hold the **V** key) and talk. Release, and it
sends your words to Gemini through `gesture_bridge.py`, speaks the reply
out loud, and — if you clearly asked it to do or stop something — starts
or kills `ultron_runtime.py` for you.

- **Listening**: browser's built-in Speech Recognition API. Free, no key,
  Chrome/Edge only (Firefox doesn't support it well, Safari is spotty).
- **Talking back**: browser's built-in Speech Synthesis API. Also free,
  also no key. It won't sound like movie-JARVIS — that needs a cloned
  voice via something like ElevenLabs, which costs money. This uses
  whatever voice your OS ships with.
- **The "brain" behind it**: your same `GEMINI_API_KEY`, called from
  `gesture_bridge.py` (never from the browser — so the key never shows up
  in page source if you ever share this file with anyone).

Say something like *"open notepad"* and it'll launch the loop with that
as the goal — no need to edit `--goal` on the command line first. Say
*"stop"* and it kills whatever's running. Anything else, it just replies
conversationally.

This needs `gesture_bridge.py` running — without it, the mic still
listens and the orb still reacts, but you'll just get "local bridge
offline" instead of an actual reply.

## WhatsApp messaging by voice

Say something like *"message Alex on WhatsApp saying I'll be a bit
late"* or *"send mom a whatsapp: call me when you're free."* This is a
two-step, two-confirmation flow, not a single-shot fire-and-forget:

1. **Compose.** It opens WhatsApp (Desktop app if installed, otherwise
   `web.whatsapp.com` in your default browser — either needs to already
   be logged in; this tool doesn't handle the QR-code login for you),
   searches for the contact you named, opens that chat, and types the
   message. It's explicitly told not to press Enter or tap send at this
   stage — there's no code path in this phase that can send anything.
2. **Confirm.** It reads the drafted message back to you — contact and
   exact text — and waits. You say **"confirm"** to actually send it, or
   **"cancel"** to drop it (nothing is sent either way until you say
   confirm). If it doesn't hear a clear yes/no, it just asks again. This
   pending confirmation also expires after 90 seconds so a stray "yes"
   minutes later can't send an old draft.

Why two spoken steps instead of one: sending a message is a real,
irreversible, other-person-facing action — unlike opening an app or
scrolling a page, a wrong send can't be undone. This mirrors how Siri or
Google Assistant handle "send a message" (draft → read back → confirm),
just voice-only both ways so you're never reaching for a keyboard or
mouse. A couple of practical notes: it types your message character-for-
character rather than paraphrasing it, and while it's composing, the mic
will feel unresponsive for a few seconds (it's mid-automation) — that's
expected, not a hang. And because this shares the mouse/keyboard with
everything else here, it won't start while a normal automation or watch
session is already running, and a pinch gesture is ignored while a send
is waiting on your confirm/cancel.

## Voice control that actually survives minimizing (no browser needed)

Here's the honest picture of what does and doesn't work minimized:

| Piece | Works minimized? | Why |
|---|---|---|
| `ultron_runtime.py` (the automation/watch loop) | ✅ Yes, already | It's a headless script — screenshotting and clicking don't need any window visible. |
| `gesture_bridge.py` (the server) | ✅ Yes, already | Also headless — it's just an HTTP server sitting in a console window. |
| Hologram's **mic/voice** via the browser | ❌ No | Needs the tab focused for key-holds, and Chrome throttles background tabs. |
| Hologram's **hand-gesture** tracking | ❌ No | Needs the tab visible for the webcam-processing loop to keep running at all. |

So the piece that actually needed fixing was the hologram's mic — and
`ultron_voice_daemon.py` replaces just that piece with a plain background
script that has no window-visibility requirement at all:

```
python ultron_voice_daemon.py --token <the bridge token gesture_bridge.py printed>
```

Leave it running (minimized, or not even in focus) alongside
`gesture_bridge.py`. Press **Ctrl+Alt+U** from anywhere — any app, any
window state — say one command, and it speaks the reply back. It's not a
new brain: it just records your voice, sends the transcript to
`gesture_bridge.py`'s existing `/voice` endpoint, and speaks whatever
comes back — so START/WATCH/MESSAGE-and-confirm/STOP all work exactly as
already documented above, just without needing the browser at all.

**What this doesn't give you back:** hand-gesture (pinch/spread) control
still needs the hologram tab open and actually visible — that's a real
webcam-processing dependency on the browser being active, not something
a background script can substitute for. If you want gestures too, keep
that tab open somewhere on screen (doesn't need to be big or focused,
just not minimized/hidden). Voice, through this daemon, has no such
requirement either way.

**Setup note for Windows:** unlike an earlier version of this that used
PyAudio, `sounddevice` ships a working binary in its own pip package —
`pip install -r requirements.txt` alone should be enough, no separate
build tools or workaround needed. If the mic never seems to trigger a
response, or cuts you off mid-sentence, open `ultron_voice_daemon.py` and
tune `SILENCE_AMPLITUDE_THRESHOLD` near the top — lower it if it's not
noticing you've started talking, raise it if background noise is
triggering it.

## Bug-watch mode (read-only) — for QA on your team's site

`--watch` is a different mode from the normal loop: it never taps, types,
or clicks anything. It just looks at the screen on a timer, asks Gemini
to flag visible bugs, and appends findings to a markdown report:

```
python ultron_runtime.py --source desktop --watch \
    --goal "our staging site in Chrome — the checkout flow" \
    --watch-report bug_report.md
```

- Checks every `--watch-interval` seconds (default 8) for up to
  `--watch-duration` minutes (default 20, hard-capped at 120).
- `--watch-scroll` lets it also scroll down periodically to see more of a
  long page — the only action this mode can take, and it still goes
  through the same confirmation prompt as the normal loop unless you also
  pass `--auto-approve`.
- `--watch-test-buttons` goes one step further: each cycle it picks one
  untested-looking button/link/tab, clicks it (confirmed, same as any
  other action), takes an after-screenshot, and asks the model whether
  anything visibly changed. If nothing changed, that gets logged as a
  possible dead control — e.g. a button that looks clickable but isn't
  wired up. It's told never to pick anything that looks destructive or
  irreversible (delete, log out, pay, submit, etc.) and you still see and
  confirm every click before it happens, but **run this against a
  staging/test environment, not production data you care about** — it's
  genuinely clicking things, and no model is perfect at judging "safe."
- You can also just say **"watch for bugs on this page"** to the voice
  assistant (see below) instead of typing the command — handy one-handed.
  (Voice-triggered watch mode is passive-only for now — no click-testing —
  since it launches without you at the keyboard to review each click.)

### The browser extension (console + network errors)

Vision alone can't see console errors or failed API calls — for that,
`ultron_bug_watcher_extension/` is a small Chrome extension that ships
**doing nothing** until you configure it:

1. `chrome://extensions` → enable Developer mode → **Load unpacked** →
   select the `ultron_bug_watcher_extension` folder.
2. Click its toolbar icon, paste the bridge token `gesture_bridge.py`
   printed on startup, set the site prefix to your team's site
   (e.g. `https://staging.yoursite.com/`), and check "Actively watching".
3. It only ever reports on pages whose URL starts with that prefix, and
   only ever sends findings to `127.0.0.1:8765` on your own machine — the
   bridge URL is hardcoded in `background.js`, not configurable from the
   popup, so nothing in the extension can be pointed anywhere else.

It reports four kinds of things: console errors/warnings and uncaught
exceptions, failed (4xx/5xx or network-level) requests, **text that's
hidden in a way that looks accidental** (zero opacity, collapsed to 0px,
pushed off-screen, or the same color as its own background — the kind of
CSS bug that leaves real content invisible to real users), and
**controls that look interactive but aren't** (pointer-events:none on
something clickable, a `role="button"` with no click handler, or an
input/button/link with no accessible name at all for screen readers).
None of this needs vision to catch — it's read straight from the page's
own DOM and styles, which is also why it can see things a screenshot
structurally can't.

Console errors, uncaught exceptions, and failed (4xx/5xx or network-level)
requests land in the same `bug_report.md` as the visual findings, each
timestamped, so you can line up "what broke" with "what it looked like at
that moment" — run both together (watch mode + the extension) while your
team clicks around the site, then read the one file afterward.

## One-handed / hands-free operation

Everything above already stacks with the voice and gesture controls:

- **Voice** now understands three intents, not two: *"open notepad"*
  (START), *"watch for bugs on this page"* (WATCH, read-only), and
  *"stop"* (STOP). Hold the mic button or the **V** key, say what you
  want, release.
- **Gesture** (pinch/spread) still maps to the fixed goal set with
  `--goal` when you launch `gesture_bridge.py` — voice is the more
  flexible option when you want a different task each time without
  reaching for the keyboard.
- If typing itself is the painful part, dictate through the browser's
  own voice-to-text or Windows' built-in **Win + H** dictation into
  whatever field you're filling — `ultron_runtime.py`'s `--allow-type`
  is for the *agent* typing on your behalf as part of a goal, not a
  replacement for your own text input.

## Notes on the model

The default model is `gemini-3.1-flash-lite` (updated August 2026 — the original default, `gemini-2.5-flash`, was retired ahead of its official shutdown date, which is exactly the kind of thing that causes a 401/404 out of nowhere). If Google renames or deprecates this one too, check [Google AI Studio](https://aistudio.google.com/) for the current free-tier vision model name and set `ULTRON_MODEL` as an environment variable to override it — no code changes needed, e.g.:
```
$env:ULTRON_MODEL = "gemini-3.5-flash"
```

## Good luck with exams

This is entirely a "build it after exams" project — nothing here needs to be run right now.
