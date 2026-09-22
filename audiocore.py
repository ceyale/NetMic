"""
audio_core.py - the audio side of the NetMic GUI control panel.

Owns the microphone capture stream and a thread-safe ring buffer of raw PCM
blocks. Nothing here knows about the network; network_stream.py drains the
buffer. This split keeps a stalled network from ever blocking the audio
callback, which is what would cause an audible glitch in the recording itself.
"""

from __future__ import annotations

import array
import collections
import logging
import math
import threading
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

try:
    import sounddevice as sd
except (ImportError, OSError) as exc:  # OSError = PortAudio library missing
    raise RuntimeError(
        f"sounddevice / PortAudio is not available: {exc}\n"
        "Install it with 'pip install sounddevice' (Linux also needs "
        "'sudo apt install libportaudio2' first)."
    ) from exc

BYTES_PER_FRAME = 2  # 16-bit mono, matches computer_server/server.py
LEVEL_UPDATE_INTERVAL_S = 1 / 30  # cap level-meter recompute rate

log = logging.getLogger("netmic.gui.audio")


class RingBuffer:
    """
    Thread-safe FIFO of fixed-size audio blocks with a drop-oldest overflow
    policy. The audio callback (producer) must never block, so push() only
    ever waits on a lock, never on space becoming available.
    """

    def __init__(self, max_blocks: int = 64):
        self._blocks: collections.deque = collections.deque(maxlen=max_blocks)
        self._cv = threading.Condition()
        self.dropped_blocks = 0
        self.pushed_blocks = 0

    def push(self, block: bytes) -> None:
        with self._cv:
            if len(self._blocks) == self._blocks.maxlen:
                self.dropped_blocks += 1  # deque will silently evict the oldest
            self._blocks.append(block)
            self.pushed_blocks += 1
            self._cv.notify()

    def pop(self, timeout: float = 0.25) -> Optional[bytes]:
        with self._cv:
            if not self._blocks:
                self._cv.wait(timeout)
            if not self._blocks:
                return None
            return self._blocks.popleft()

    def clear(self) -> None:
        with self._cv:
            self._blocks.clear()
            self.dropped_blocks = 0
            self.pushed_blocks = 0

    def __len__(self) -> int:
        with self._cv:
            return len(self._blocks)


def list_input_devices() -> list[tuple[int, str]]:
    """Returns [(device_index, display_name), ...] for capture-capable devices."""
    devices = []
    try:
        for idx, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                devices.append((idx, info.get("name", f"Device {idx}")))
    except Exception as exc:  # PortAudio host API can fail to enumerate on some systems
        log.warning("Could not enumerate audio devices: %s", exc)
    return devices


def _level_from_block(block: bytes) -> tuple[float, float]:
    """RMS and peak of a 16-bit PCM block, both normalised to 0.0-1.0."""
    if not block:
        return 0.0, 0.0
    samples = array.array("h")
    try:
        samples.frombytes(block[: len(block) - (len(block) % 2)])
    except ValueError:
        return 0.0, 0.0
    if not samples:
        return 0.0, 0.0
    peak = max(abs(s) for s in samples) / 32768.0
    mean_sq = sum(s * s for s in samples) / len(samples)
    rms = math.sqrt(mean_sq) / 32768.0
    return min(rms, 1.0), min(peak, 1.0)


class AudioCaptureEngine(QObject):
    """
    Wraps a sounddevice.RawInputStream. State (levels, running flag, last
    error) is kept behind a lock and read by the GUI via polling methods
    (get_level(), is_running()) rather than push signals, so the hot audio
    callback path never has to touch Qt's cross-thread signal machinery.
    log_line() still uses a thread-safe queue the GUI drains on a timer.
    """

    def __init__(self, ring_buffer: RingBuffer, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._ring = ring_buffer
        self._stream: Optional[sd.RawInputStream] = None
        self._lock = threading.Lock()
        self._rms = 0.0
        self._peak = 0.0
        self._last_level_calc = 0.0
        self._running = False
        self._device = None
        self._rate = 44100
        self._chunk = 512
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._log_lines: collections.deque = collections.deque(maxlen=200)
        self._next_reopen = 0.0

    # ---- logging (thread-safe, polled by the GUI) -------------------------
    def _log(self, msg: str) -> None:
        log.info(msg)
        with self._lock:
            self._log_lines.append(msg)

    def drain_log(self) -> list[str]:
        with self._lock:
            lines = list(self._log_lines)
            self._log_lines.clear()
        return lines

    # ---- lifecycle ----------------------------------------------------------
    def start(self, device, rate: int, chunk: int) -> bool:
        """Opens the capture stream. Returns False (and logs) on failure."""
        self.stop()
        self._device = device
        self._rate = rate
        self._chunk = chunk
        self._ring.clear()
        try:
            self._stream = self._open_stream()
            self._stream.start()
        except Exception as exc:
            self._log(f"Cannot open microphone: {exc}")
            self._stream = None
            return False
        with self._lock:
            self._running = True
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()
        try:
            name = sd.query_devices(device, "input")["name"] if device is not None else "system default"
        except Exception:
            name = "system default"
        self._log(f"Microphone opened: {name} ({rate} Hz, {chunk} frames/block)")
        return True

    def stop(self) -> None:
        with self._lock:
            was_running = self._running
            self._running = False
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1.0)
            self._watchdog_thread = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._rms = 0.0
            self._peak = 0.0
        if was_running:
            self._log("Microphone closed.")

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def get_level(self) -> tuple[float, float]:
        with self._lock:
            return self._rms, self._peak

    # ---- internals ----------------------------------------------------------
    def _open_stream(self) -> "sd.RawInputStream":
        return sd.RawInputStream(
            samplerate=self._rate,
            blocksize=self._chunk,
            device=self._device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        # Runs on PortAudio's own thread: no logging, no blocking, no Qt calls.
        block = bytes(indata)
        self._ring.push(block)
        now = time.monotonic()
        if now - self._last_level_calc >= LEVEL_UPDATE_INTERVAL_S:
            self._last_level_calc = now
            rms, peak = _level_from_block(block)
            with self._lock:
                self._rms, self._peak = rms, peak
        if status:
            # e.g. input overflow - not fatal, just noted for diagnostics.
            self._log(f"Audio status flag: {status}")

    def _watchdog(self) -> None:
        """Reopens the microphone stream if the device disappears mid-run."""
        while not self._watchdog_stop.wait(0.5):
            stream = self._stream
            if stream is not None and stream.active:
                continue
            if not self.is_running():
                continue
            now = time.monotonic()
            if now < self._next_reopen:
                continue
            self._next_reopen = now + 2.0
            self._log("Microphone stream stopped unexpectedly - trying to reopen it...")
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            try:
                self._stream = self._open_stream()
                self._stream.start()
                self._log("Microphone restored.")
            except Exception as exc:
                self._log(f"Reopen failed: {exc}")