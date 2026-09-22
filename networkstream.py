"""
network_stream.py - the network side of the NetMic GUI control panel.

Owns the UDP socket, drains a RingBuffer (see audio_core.py) of captured
audio and sends it to the server, and separately tracks link health (RTT via
PING/PONG, and loss/throughput via ACKs and local send counters). All state
is kept behind a lock and exposed through get_stats()/get_state(), which the
GUI polls on a timer - this is the "thread-safe queue" alternative to Qt
signals for the hot, frequent data; pyqtSignal is used only for the rare,
discrete "connection changed" event.

Wire protocol - kept in sync with computer_server/server.py:
    header (9 bytes, big endian): magic "NM" | type u8 | session u16 | seq u32
    type 1 = AUDIO (payload: raw int16 PCM, <=1024 bytes)
    type 2 = ACK   (payload: u32 rate [, u32 received, u32 lost])
    type 3 = BYE   (payload: none)
    type 4 = PING  (payload: 8 bytes, our timestamp, echoed back)
    type 5 = PONG  (payload: the PING payload, echoed verbatim)
"""

from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from gui_client.audio_core import RingBuffer

MAGIC = b"NM"
HEADER = struct.Struct(">2sBHI")
T_AUDIO, T_ACK, T_BYE, T_PING, T_PONG = 1, 2, 3, 4, 5
MAX_PAYLOAD = 1024                 # bytes per datagram, stays under the Wi-Fi MTU

PING_INTERVAL_S = 1.0
LINK_WARNING_S = 4.0               # no PONG for this long -> "warning" state
LINK_LOST_S = 10.0                 # no PONG for this long -> back to "connecting"
STATS_WINDOW_S = 1.0                # throughput averaging window

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_WARNING = "warning"

log = logging.getLogger("netmic.gui.network")


class NetworkTransport(QObject):
    """UDP transport with independent connect/stream lifecycles.

    connect_to()/disconnect() manage the socket and link-health probing.
    start_streaming()/stop_streaming() manage whether captured audio is
    actually drained from the ring buffer and sent - you can be "connected"
    (probing link health) without streaming, and streaming implies connected.
    """

    connection_changed = pyqtSignal(str)  # one of STATE_*
    fatal_error = pyqtSignal(str)         # unrecoverable setup error (e.g. bad hostname)

    def __init__(self, ring_buffer: RingBuffer, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._ring = ring_buffer
        self._sock: Optional[socket.socket] = None
        self._addr: Optional[tuple[str, int]] = None
        self._session = 0
        self._seq = 0

        self._lock = threading.Lock()
        self._state = STATE_DISCONNECTED
        self._streaming = False
        self._last_pong = 0.0
        self._rtt_ms: Optional[float] = None
        self._sent_pkts = 0
        self._sent_bytes = 0
        self._send_errors = 0
        self._dropped_blocks_reported = 0
        self._throughput_kbps = 0.0
        self._server_rate: Optional[int] = None
        self._server_received = 0
        self._server_lost = 0
        self._log_lines: list = []

        self._stop = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None
        self._receiver_thread: Optional[threading.Thread] = None
        self._pinger_thread: Optional[threading.Thread] = None

    # ---- logging (thread-safe, polled by the GUI) -------------------------
    def _log(self, msg: str) -> None:
        log.info(msg)
        with self._lock:
            self._log_lines.append(msg)
            del self._log_lines[:-200]

    def drain_log(self) -> list[str]:
        with self._lock:
            lines = list(self._log_lines)
            self._log_lines.clear()
        return lines

    def _set_state(self, state: str) -> None:
        with self._lock:
            changed = state != self._state
            self._state = state
        if changed:
            self.connection_changed.emit(state)

    # ---- public snapshot API -------------------------------------------------
    def get_state(self) -> str:
        with self._lock:
            return self._state

    def is_streaming(self) -> bool:
        with self._lock:
            return self._streaming

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "streaming": self._streaming,
                "rtt_ms": self._rtt_ms,
                "sent_pkts": self._sent_pkts,
                "sent_bytes": self._sent_bytes,
                "send_errors": self._send_errors,
                "dropped_blocks": self._dropped_blocks_reported,
                "throughput_kbps": self._throughput_kbps,
                "server_rate": self._server_rate,
                "server_received": self._server_received,
                "server_lost": self._server_lost,
            }

    # ---- connect / disconnect -------------------------------------------------
    def connect_to(self, host: str, port: int) -> bool:
        self.disconnect()
        try:
            ip = socket.gethostbyname(host)
        except socket.gaierror as exc:
            self._log(f"Cannot resolve '{host}': {exc}")
            self.fatal_error.emit(f"Cannot resolve host '{host}': {exc}")
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF hint
            except OSError:
                pass
        except OSError as exc:
            self._log(f"Cannot create UDP socket: {exc}")
            self.fatal_error.emit(f"Cannot create UDP socket: {exc}")
            return False

        self._sock = sock
        self._addr = (ip, port)
        self._session = random.randint(1, 0xFFFF)
        self._seq = 0
        with self._lock:
            self._rtt_ms = None
            self._sent_pkts = self._sent_bytes = self._send_errors = 0
            self._dropped_blocks_reported = 0
            self._server_rate = None
            self._server_received = self._server_lost = 0
            self._last_pong = 0.0
        self._stop.clear()
        self._set_state(STATE_CONNECTING)
        self._log(f"Connecting to {ip}:{port}...")

        self._receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self._pinger_thread = threading.Thread(target=self._pinger_loop, daemon=True)
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._receiver_thread.start()
        self._pinger_thread.start()
        self._sender_thread.start()
        return True

    def disconnect(self) -> None:
        if self._sock is None and self.get_state() == STATE_DISCONNECTED:
            return
        self.stop_streaming(send_bye=True)
        self._stop.set()
        for t in (self._receiver_thread, self._pinger_thread, self._sender_thread):
            if t is not None:
                t.join(timeout=1.0)
        self._receiver_thread = self._pinger_thread = self._sender_thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._ring.clear()
        self._set_state(STATE_DISCONNECTED)
        self._log("Disconnected.")

    # ---- streaming on/off -------------------------------------------------------
    def start_streaming(self) -> bool:
        if self.get_state() == STATE_DISCONNECTED:
            self._log("Cannot start streaming: not connected.")
            return False
        with self._lock:
            self._streaming = True
        self._log("Streaming started.")
        return True

    def stop_streaming(self, send_bye: bool = False) -> None:
        with self._lock:
            was_streaming = self._streaming
            self._streaming = False
        if was_streaming and send_bye and self._sock is not None and self._addr is not None:
            pkt = HEADER.pack(MAGIC, T_BYE, self._session, self._seq)
            for _ in range(2):
                try:
                    self._sock.sendto(pkt, self._addr)
                except OSError:
                    break
            self._log("Streaming stopped.")

    # ---- worker loops -------------------------------------------------------------
    def _sender_loop(self) -> None:
        window_start = time.monotonic()
        window_bytes = 0
        while not self._stop.is_set():
            if not self.is_streaming():
                time.sleep(0.05)
                window_start = time.monotonic()
                window_bytes = 0
                continue
            block = self._ring.pop(timeout=0.25)
            now = time.monotonic()
            if block:
                self._send_block(block)
                window_bytes += len(block)
            if now - window_start >= STATS_WINDOW_S:
                kbps = (window_bytes * 8 / 1000.0) / (now - window_start)
                with self._lock:
                    self._throughput_kbps = kbps
                    self._dropped_blocks_reported = self._ring.dropped_blocks
                window_start = now
                window_bytes = 0

    def _send_block(self, block: bytes) -> None:
        if self._sock is None or self._addr is None:
            return
        for off in range(0, len(block), MAX_PAYLOAD):
            pkt = HEADER.pack(MAGIC, T_AUDIO, self._session, self._seq) + block[off:off + MAX_PAYLOAD]
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            try:
                self._sock.sendto(pkt, self._addr)
                with self._lock:
                    self._sent_pkts += 1
                    self._sent_bytes += len(pkt)
            except OSError as exc:
                with self._lock:
                    self._send_errors += 1
                self._log(f"Send failed ({exc}) - dropping audio until the network is back.")

    def _pinger_loop(self) -> None:
        while not self._stop.is_set():
            if self._sock is not None and self._addr is not None:
                payload = struct.pack("!d", time.monotonic())
                pkt = HEADER.pack(MAGIC, T_PING, self._session, 0) + payload
                try:
                    self._sock.sendto(pkt, self._addr)
                except OSError as exc:
                    self._log(f"Ping failed: {exc}")
            self._update_link_state()
            self._stop.wait(PING_INTERVAL_S)

    def _update_link_state(self) -> None:
        with self._lock:
            last_pong = self._last_pong
        if last_pong == 0.0:
            return  # still waiting for the first reply; stay in "connecting"
        age = time.monotonic() - last_pong
        state = self.get_state()
        if age > LINK_LOST_S and state != STATE_CONNECTING:
            self._log(f"No response from server for {age:.0f}s - reconnecting probe.")
            self._set_state(STATE_CONNECTING)
        elif age > LINK_WARNING_S and state == STATE_CONNECTED:
            self._set_state(STATE_WARNING)
        elif age <= LINK_WARNING_S and state in (STATE_CONNECTING, STATE_WARNING):
            self._set_state(STATE_CONNECTED)

    def _receiver_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                time.sleep(0.1)
                continue
            try:
                data, src = sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.1)
                continue
            if self._addr is None or src[0] != self._addr[0] or len(data) < HEADER.size:
                continue
            magic, ptype, session, _seq = HEADER.unpack_from(data)
            if magic != MAGIC or session != self._session:
                continue
            if ptype == T_PONG:
                self._on_pong(data[HEADER.size:])
            elif ptype == T_ACK:
                self._on_ack(data[HEADER.size:])

    def _on_pong(self, payload: bytes) -> None:
        if len(payload) < 8:
            return
        (sent_at,) = struct.unpack_from("!d", payload)
        rtt_ms = max((time.monotonic() - sent_at) * 1000.0, 0.0)
        with self._lock:
            self._rtt_ms = rtt_ms
            self._last_pong = time.monotonic()
        if self.get_state() in (STATE_CONNECTING, STATE_WARNING):
            self._set_state(STATE_CONNECTED)

    def _on_ack(self, payload: bytes) -> None:
        if len(payload) < 4:
            return
        with self._lock:
            (self._server_rate,) = struct.unpack_from(">I", payload)
            if len(payload) >= 12:
                self._server_received, self._server_lost = struct.unpack_from(">II", payload, 4)