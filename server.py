#!/usr/bin/env python3
"""
NetMic server - runs on the PC.

Receives raw 16-bit mono PCM over UDP from the NetMic phone client and plays it
through an audio output device. Point --output-device at a virtual cable
(e.g. VB-Cable's "CABLE Input") and other apps can use it as a microphone.

Wire protocol (identical in phone_client/client.py):
    header (9 bytes, big endian): magic "NM" | type u8 | session u16 | seq u32
    type 1 = AUDIO (payload: raw int16 PCM), 2 = ACK (payload: u32 sample rate),
    type 3 = BYE
"""

import argparse
import logging
import socket
import struct
import sys
import threading
import time

try:
    import sounddevice as sd
except (ImportError, OSError) as exc:  # OSError = PortAudio library missing
    sys.exit(f"sounddevice / PortAudio is not available: {exc}\n"
             "See README.md, section 'Install'.")

# --------------------------------------------------------------------------
# Configuration (all overridable on the command line)
# --------------------------------------------------------------------------
DEFAULT_PORT = 5005
DEFAULT_RATE = 44100
DEFAULT_CHUNK = 512            # frames per audio-callback block
DEFAULT_PREBUFFER_MS = 60      # audio collected before playback starts
DEFAULT_MAX_BUFFER_MS = 250    # hard cap on buffered audio (bounds latency)

# --------------------------------------------------------------------------
# Wire protocol - keep in sync with client.py
# --------------------------------------------------------------------------
MAGIC = b"NM"
HEADER = struct.Struct(">2sBHI")
T_AUDIO, T_ACK, T_BYE = 1, 2, 3
BYTES_PER_FRAME = 2            # 16-bit mono

# --------------------------------------------------------------------------
# Behaviour tuning
# --------------------------------------------------------------------------
MAX_CONCEAL_PACKETS = 8        # gaps up to this size are filled with silence
CLIENT_TIMEOUT_S = 5.0         # no audio for this long -> client considered gone
ACK_INTERVAL_S = 1.0
STATS_INTERVAL_S = 5.0

log = logging.getLogger("netmic.server")


class JitterBuffer:
    """Thread-safe byte FIFO between the network thread and the audio callback."""

    def __init__(self, prebuffer_bytes: int, max_bytes: int):
        self._prebuffer = prebuffer_bytes
        self._max = max_bytes
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._playing = False
        self.underruns = 0
        self.overflows = 0

    def push(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            excess = len(self._buf) - self._max
            if excess > 0:                      # too much queued: drop oldest audio
                excess -= excess % BYTES_PER_FRAME
                del self._buf[:excess]
                self.overflows += 1

    def pull(self, nbytes: int) -> bytes:
        """Called from the audio thread; always returns exactly nbytes."""
        with self._lock:
            if not self._playing:
                if len(self._buf) < self._prebuffer:
                    return bytes(nbytes)        # still buffering -> silence
                self._playing = True
            if len(self._buf) >= nbytes:
                out = bytes(self._buf[:nbytes])
                del self._buf[:nbytes]
                return out
            out = bytes(self._buf) + bytes(nbytes - len(self._buf))
            self._buf.clear()
            self._playing = False               # underrun: re-buffer before resuming
            self.underruns += 1
            return out

    def level_bytes(self) -> int:
        with self._lock:
            return len(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._playing = False
            self.underruns = 0
            self.overflows = 0


def local_ips() -> list:
    """Best-effort list of this machine's LAN IPv4 addresses (for the startup hint)."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))    # no packet is sent
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


class NetMicServer:
    def __init__(self, args):
        self.args = args
        self.rate = args.rate
        bytes_per_ms = self.rate * BYTES_PER_FRAME / 1000
        self.bytes_per_ms = bytes_per_ms
        prebuffer = self._align(int(args.prebuffer_ms * bytes_per_ms))
        max_bytes = self._align(int(args.max_buffer_ms * bytes_per_ms))
        self.jitter = JitterBuffer(prebuffer, max(max_bytes, prebuffer * 2))
        self.sock = None
        self.stream = None
        self._throttle = {}
        self._next_reopen = 0.0
        self._reset_client()

    @staticmethod
    def _align(n: int) -> int:
        return n - n % BYTES_PER_FRAME

    # ---- helpers ---------------------------------------------------------
    def _log_every(self, key, seconds, level, msg, *a):
        now = time.monotonic()
        last = self._throttle.get(key)
        if last is None or now - last >= seconds:
            self._throttle[key] = now
            log.log(level, msg, *a)

    def _reset_client(self):
        self.client_addr = None
        self.session = None
        self.expected_seq = 0
        self.last_rx = 0.0
        self.last_ack = 0.0
        self.last_stats = time.monotonic()
        self.last_payload_len = 1024
        self.received = self.lost = self.late = self.bad = 0
        self.jitter.clear()

    # ---- setup -----------------------------------------------------------
    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
        except OSError:
            pass
        sock.bind((self.args.bind, self.args.port))
        sock.settimeout(0.5)
        if hasattr(socket, "SIO_UDP_CONNRESET"):    # Windows: ignore ICMP "port unreachable"
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (OSError, ValueError):
                pass
        return sock

    def _open_stream(self):
        return sd.RawOutputStream(
            samplerate=self.rate,
            blocksize=self.args.chunk,
            device=self.args.output_device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=self._audio_callback,
        )

    def _audio_callback(self, outdata, frames, time_info, status):
        outdata[:] = self.jitter.pull(frames * BYTES_PER_FRAME)

    # ---- main loop ---------------------------------------------------------
    def run(self) -> int:
        try:
            self.sock = self._open_socket()
        except OSError as exc:
            log.error("Cannot bind UDP %s:%d: %s (port in use?)",
                      self.args.bind, self.args.port, exc)
            return 1
        try:
            self.stream = self._open_stream()
            self.stream.start()
        except Exception as exc:
            log.error("Cannot open audio output: %s", exc)
            log.error("Try --list-devices, --output-device <index> or a different --rate.")
            self.sock.close()
            return 1

        log.info("Receiving audio packets on UDP port %d (%d Hz, 16-bit, mono)",
                 self.args.port, self.rate)
        for ip in local_ips():
            log.info("Phone client target:  python client.py --server %s --port %d",
                     ip, self.args.port)
        log.info("Waiting for the phone... (Ctrl+C to quit)")

        try:
            self._serve()
        except KeyboardInterrupt:
            log.info("Shutting down.")
        finally:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.sock.close()
        return 0

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                self._housekeeping()
                continue
            except ConnectionResetError:            # Windows ICMP quirk
                continue
            except OSError as exc:
                self._log_every("recv", 5, logging.ERROR, "Socket error: %s", exc)
                time.sleep(0.2)
                continue
            try:
                self._handle(data, addr)
            except Exception:                       # never let one bad packet kill the server
                log.exception("Unexpected error while handling packet from %s", addr)
            self._housekeeping()

    # ---- packet handling ---------------------------------------------------
    def _handle(self, data: bytes, addr):
        if len(data) < HEADER.size:
            self.bad += 1
            return
        magic, ptype, session, seq = HEADER.unpack_from(data)
        if magic != MAGIC:
            self.bad += 1
            return
        if ptype == T_AUDIO:
            payload = data[HEADER.size:]
            if not payload or len(payload) % BYTES_PER_FRAME:
                self.bad += 1
                return
            self._on_audio(addr, session, seq, payload)
        elif ptype == T_BYE:
            if self.client_addr and addr[0] == self.client_addr[0] and session == self.session:
                log.info("Client %s disconnected (goodbye received).", addr[0])
                self._reset_client()

    def _start_client(self, addr, session, seq):
        self._reset_client()
        self.client_addr = addr
        self.session = session
        self.expected_seq = seq
        log.info("Client connected: %s:%d - receiving audio", addr[0], addr[1])

    def _on_audio(self, addr, session, seq, payload):
        now = time.monotonic()
        if self.client_addr is None:
            self._start_client(addr, session, seq)
        elif addr[0] != self.client_addr[0]:
            self._log_every("foreign", 10, logging.WARNING,
                            "Ignoring audio from %s - already serving %s",
                            addr[0], self.client_addr[0])
            return
        elif session != self.session:
            log.info("Client %s restarted its stream.", addr[0])
            self._start_client(addr, session, seq)
        self.client_addr = addr
        self.last_rx = now

        diff = (seq - self.expected_seq) & 0xFFFFFFFF
        if diff >= 0x80000000:                      # older than expected: late/duplicate
            self.late += 1
            return
        if diff:                                    # packets missing
            self.lost += diff
            if diff <= MAX_CONCEAL_PACKETS:         # keep timing with silence
                self.jitter.push(bytes(self.last_payload_len) * diff)
        self.expected_seq = (seq + 1) & 0xFFFFFFFF
        self.last_payload_len = len(payload)
        self.received += 1
        self.jitter.push(payload)

        if now - self.last_ack >= ACK_INTERVAL_S:
            self._send_ack(now)

    def _send_ack(self, now):
        self.last_ack = now
        pkt = HEADER.pack(MAGIC, T_ACK, self.session, 0) + struct.pack(">I", self.rate)
        try:
            self.sock.sendto(pkt, self.client_addr)
        except OSError as exc:
            self._log_every("ack", 10, logging.WARNING, "Could not send ACK: %s", exc)

    # ---- periodic work -------------------------------------------------------
    def _housekeeping(self):
        now = time.monotonic()
        if self.client_addr is not None:
            if now - self.last_rx > CLIENT_TIMEOUT_S:
                log.warning("No audio from %s for %.0f s - waiting for it to reconnect.",
                            self.client_addr[0], CLIENT_TIMEOUT_S)
                self._reset_client()
            elif now - self.last_stats >= STATS_INTERVAL_S:
                self.last_stats = now
                total = self.received + self.lost
                pct = 100.0 * self.lost / total if total else 0.0
                log.info("Rx %d pkts | lost %d (%.1f%%) | late %d | bad %d | "
                         "underruns %d | overflows %d | buffer %.0f ms",
                         self.received, self.lost, pct, self.late, self.bad,
                         self.jitter.underruns, self.jitter.overflows,
                         self.jitter.level_bytes() / self.bytes_per_ms)

        if self.stream is not None and not self.stream.active and now >= self._next_reopen:
            self._next_reopen = now + 2.0
            log.error("Audio output stopped - trying to reopen it...")
            try:
                self.stream.close()
            except Exception:
                pass
            try:
                self.stream = self._open_stream()
                self.stream.start()
                log.info("Audio output restored.")
            except Exception as exc:
                log.error("Reopen failed: %s", exc)


def parse_device(value):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def parse_args():
    p = argparse.ArgumentParser(description="NetMic server: play phone microphone audio received over UDP.")
    p.add_argument("--bind", default="0.0.0.0", help="interface to listen on (default: all)")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default {DEFAULT_PORT})")
    p.add_argument("-r", "--rate", type=int, default=DEFAULT_RATE, help="sample rate; must match the client")
    p.add_argument("-c", "--chunk", type=int, default=DEFAULT_CHUNK, help="playback block size in frames")
    p.add_argument("--prebuffer-ms", type=int, default=DEFAULT_PREBUFFER_MS,
                   help="audio buffered before playback starts; lower = less latency, more dropouts")
    p.add_argument("--max-buffer-ms", type=int, default=DEFAULT_MAX_BUFFER_MS,
                   help="maximum buffered audio; older audio is dropped beyond this")
    p.add_argument("-o", "--output-device", default=None,
                   help="output device index or name (see --list-devices); default: system default")
    p.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    if args.list_devices:
        print(sd.query_devices())
        return 0
    args.output_device = parse_device(args.output_device)
    return NetMicServer(args).run()


if __name__ == "__main__":
    sys.exit(main())