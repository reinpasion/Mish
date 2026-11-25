"""Optional Windows reply speech for the MISH_VOICE=1 trial."""

import os
import threading

from .win_utils import log

# SAPI 5 SpeechVoiceSpeakFlags: asynchronous, purge prior speech, plain text.
_SPEAK_FLAGS = 1 | 2 | 16
_VOICE_RATE = 2  # SAPI: -10 (slow) to 10 (fast)


class _Voice:
    def __init__(self, enabled: bool):
        self._enabled = enabled
        self._ready = threading.Condition()
        self._pending: str | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def speak(self, text: str):
        if not self._enabled or not text.strip():
            return
        with self._ready:
            if self._closed:
                return
            self._pending = text
            if self._thread is None:
                worker = threading.Thread(target=self._run, name="mish-voice", daemon=True)
                try:
                    worker.start()
                except RuntimeError:
                    self._pending = None
                    self._closed = True
                    return
                self._thread = worker
            self._ready.notify()

    def stop(self):
        with self._ready:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            worker = self._thread
            self._ready.notify()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.25)

    def _run(self):
        try:
            import pythoncom
            from win32com.client import Dispatch

            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            try:
                self._play(Dispatch, pythoncom.PumpWaitingMessages)
            finally:
                pythoncom.CoUninitialize()
        except Exception as error:
            log("voice: trial disabled for this session:", repr(error))
        finally:
            with self._ready:
                self._pending = None
                self._closed = True

    def _play(self, dispatch, pump_messages):
        engine = dispatch("SAPI.SpVoice")
        engine.Rate = _VOICE_RATE
        playing = False
        try:
            if engine.GetVoices().Count == 0:
                raise RuntimeError("No installed SAPI voices")

            while True:
                with self._ready:
                    self._ready.wait_for(
                        lambda: self._closed or self._pending is not None,
                        timeout=0.05 if playing else None,
                    )
                    if self._closed:
                        return
                    text, self._pending = self._pending, None

                if text is not None:
                    playing = True
                    engine.Speak(text, _SPEAK_FLAGS)
                if playing:
                    pump_messages()
                    playing = not engine.WaitUntilDone(0)
        finally:
            if playing:
                try:
                    engine.Speak("", _SPEAK_FLAGS)
                except Exception:
                    pass
            engine = None


_voice = _Voice(enabled=os.environ.get("MISH_VOICE") == "1")


def speak(text: str):
    """Submit the newest reply for background speech."""
    _voice.speak(text)


def stop():
    """Discard pending speech and request worker shutdown."""
    _voice.stop()
