"""
ultron_voice_daemon.py — Voice control for Ultron with no browser window
required at all.

This replaces the hologram page's mic for voice control specifically. It
doesn't need any window visible, focused, or even open — it listens for
a global hotkey (works no matter what's focused or minimized, using the
same pynput mechanism as ultron_runtime.py's Ctrl+Alt+K emergency stop),
records one utterance with your microphone, sends the transcript to
gesture_bridge.py's existing /voice endpoint (so all the START/WATCH/
MESSAGE/confirm logic you already have is reused, not duplicated), and
speaks the reply back with offline text-to-speech.

WHY THIS EXISTS, SPECIFICALLY
  ultron_runtime.py and gesture_bridge.py already run fine minimized —
  they're headless scripts, not windows. The one piece that genuinely
  couldn't work minimized was the hologram page's mic/hand-tracking,
  because browsers throttle camera processing and can't see a key-hold
  on an unfocused window. This script sidesteps that by not being a
  browser page at all.

WHAT THIS DOES NOT FIX
  Hand-gesture (pinch/spread) control still needs the hologram tab open
  and visible — that part genuinely depends on the browser reading your
  webcam, which pauses when the tab is hidden. Voice, via this script,
  has no such requirement. If you want gestures too, keep the hologram
  tab open (even a small/off-to-the-side window is fine — it just can't
  be minimized or hidden behind other windows for the camera loop to
  keep running).

REQUIREMENTS
  pip install -r requirements.txt   (adds SpeechRecognition + pyttsx3 +
                                      sounddevice — sounddevice ships a
                                      working binary in its own pip
                                      package, so unlike PyAudio it needs
                                      no compiler/build tools on Windows)
  gesture_bridge.py must already be running (this only talks to it).

USAGE
  python ultron_voice_daemon.py --token <paste the bridge token here>
  Then press Ctrl+Alt+U anywhere, anytime — even with this window
  minimized — say your command, and wait for the spoken reply.
  Ctrl+C in this window to stop the daemon. This does not touch or
  replace ultron_runtime.py's own Ctrl+Alt+K emergency stop.
"""

import argparse
import sys
import threading
import time

import numpy as np
import requests
import sounddevice as sd

try:
    import speech_recognition as sr
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    import pyttsx3
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from pynput import keyboard
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

HOTKEY = "<ctrl>+<alt>+u"
BRIDGE_URL = "http://127.0.0.1:8765/voice"
SAMPLE_RATE = 16000
MAX_UTTERANCE_SECONDS = 15       # hard cap on one recording, whatever happens
PRE_SPEECH_TIMEOUT_SECONDS = 6   # give up if nothing is said at all within this long
TRAILING_SILENCE_SECONDS = 1.2   # how much quiet after speech means "done talking"
SILENCE_AMPLITUDE_THRESHOLD = 500  # tune up if it cuts you off early in a noisy room,
                                    # down if it never seems to detect you've started

busy_lock = threading.Lock()


def record_utterance():
    """
    Records from the default microphone via sounddevice — no PyAudio, no
    compiler needed, since sounddevice ships a working binary in its own
    pip package. Stops automatically once speech has been heard and then
    trails off into ~1.2s of quiet, same idea as a normal push-to-talk.
    Returns (raw_pcm_bytes, sample_rate) or None if nothing was said.
    """
    block_seconds = 0.05
    block_size = int(SAMPLE_RATE * block_seconds)
    frames = []
    speech_started = False
    silence_run = 0.0
    waited = 0.0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=block_size) as stream:
        while True:
            block, _ = stream.read(block_size)
            frames.append(block.copy())
            amplitude = float(np.abs(block).mean())
            waited += block_seconds

            if amplitude > SILENCE_AMPLITUDE_THRESHOLD:
                speech_started = True
                silence_run = 0.0
            elif speech_started:
                silence_run += block_seconds

            if speech_started and silence_run >= TRAILING_SILENCE_SECONDS:
                break
            if not speech_started and waited >= PRE_SPEECH_TIMEOUT_SECONDS:
                return None
            if waited >= MAX_UTTERANCE_SECONDS:
                break

    audio_np = np.concatenate(frames, axis=0)
    return audio_np.tobytes(), SAMPLE_RATE


def speak(engine: "pyttsx3.Engine", text: str):
    print(f"🔊 {text}")
    engine.say(text)
    engine.runAndWait()


def handle_utterance(token: str, engine: "pyttsx3.Engine"):
    if not busy_lock.acquire(blocking=False):
        print("(already processing one utterance — ignoring extra hotkey press)")
        return
    try:
        recognizer = sr.Recognizer()
        print("🎤 listening...")
        result = record_utterance()
        if result is None:
            speak(engine, "Didn't hear anything.")
            return
        raw_pcm, sample_rate = result
        audio = sr.AudioData(raw_pcm, sample_rate, 2)  # 2 = bytes per sample for int16

        try:
            transcript = recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            speak(engine, "Didn't catch that.")
            return
        except sr.RequestError as e:
            speak(engine, "Speech recognition service error.")
            print(f"  (details: {e})")
            return

        print(f'🗣️  "{transcript}"')

        try:
            resp = requests.post(
                BRIDGE_URL,
                headers={"X-Ultron-Token": token, "Content-Type": "application/json"},
                json={"transcript": transcript},
                timeout=95,  # WhatsApp compose can legitimately take a while
            )
            resp.raise_for_status()
            decision = resp.json()
        except requests.exceptions.RequestException as e:
            speak(engine, "Can't reach the bridge — is gesture_bridge.py running?")
            print(f"  (details: {e})")
            return

        reply = decision.get("reply", "").strip()
        if reply:
            speak(engine, reply)
    finally:
        busy_lock.release()


def main():
    parser = argparse.ArgumentParser(description="Background voice control for Ultron — no browser needed.")
    parser.add_argument("--token", help="Bridge token printed by gesture_bridge.py on startup. "
                                         "If omitted, you'll be prompted for it.")
    args = parser.parse_args()

    token = args.token or input("Paste the bridge token gesture_bridge.py printed on startup: ").strip()
    if not token:
        print("No token given — exiting.")
        sys.exit(1)

    engine = pyttsx3.init()

    print("🤖 ULTRON VOICE DAEMON")
    print("=" * 40)
    print(f"Hotkey: {HOTKEY.replace('<', '').replace('>', '').upper()} — works anywhere, even minimized.")
    print("Press it, speak one command, wait for the spoken reply.")
    print("Make sure gesture_bridge.py is already running in another window.")
    print("Ctrl+C here to stop this daemon (does not stop gesture_bridge.py).\n")

    def on_hotkey():
        threading.Thread(target=handle_utterance, args=(token, engine), daemon=True).start()

    listener = keyboard.GlobalHotKeys({HOTKEY: on_hotkey})
    listener.start()
    try:
        while listener.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping voice daemon.")
        listener.stop()


if __name__ == "__main__":
    main()
