#!/usr/bin/env python3
"""
NetMic client - runs on the phone (Termux) or any computer for testing.

Captures the microphone (44.1 kHz, 16-bit, mono) and streams raw PCM to the
NetMic server over UDP. Streaming never blocks on the network: if the network or
the server is unavailable, audio is simply dropped and the client keeps going.

Wire protocol (identical in computer_server/server.py):
    header (9 bytes, big endian): magic "NM" | type u8 | session u16 | seq u32
    type 1 = AUDIO (payload: raw int16 PCM), 2 = ACK (payload: u32 sample rate),
    type 3 = BYE
"""

import argparse
import logging
import queue
import random
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
# Configuration: set your PC's IP here, or pass --server on the command line
# --------------------------------------------------------------------------
SERVER_IP = "192.168.1.100"
DEFAULT_PORT = 5005
DEFAULT_RATE = 44100
DEFAULT_CHUNK = 512            # frames per capture block (512 or 1024)

# --------------------------------------------------------------------------
# Wire protocol - keep in sync with server.py
# --------------------------------------------------------------------------
MAGIC = b"NM"
HEADER = struct.Struct(">2sBHI")
T_AUDIO, T_ACK, T_BYE = 1, 2, 3
MAX_PAYLOAD = 1024             # bytes per datagram (512 frames) -> stays under Wi-Fi MTU

# --------------------------------------------------------------------------
# Behaviour tuning
# --------------------------------------------------------------------------
MAX_QUEUE_BLOCKS = 50          # ~0.6 s of audio at 512 frames; older blocks are dropped
ACK_TIMEOUT_S = 4.0            # no ACK for this long -> link considered down
STATS_INTERVAL_S = 5.0

log = logging.getLogger("netmic.client")
_last_log = {}


def log_every(key, seconds, level, msg, *args):
    """Rate-limited logging so a dead network can't flood the terminal."""
    now = time.monotonic()
    last = _last_log.get(key)
    if last is None or now - last >= seconds:
        _last_log[key] = now
        log.log(level, msg, *args)


class LinkMonitor:
    """Tracks whether the server is acknowledging our stream."""

    def __init__(self):
        self.started = time.monotonic()
        self.last_ack = None
        self.state = "connecting"

    def on_ack(self):
        self.last_ack = time.monotonic()

    def check(self):
        now = time.monotonic()
        if self.last_ack is None:
            if now - self.started > 3.0:
                log_every("noack", 10, logging.WARNING,
                          "No response from server yet - still streaming. "
                          "Check IP, port, same Wi-Fi network and the PC firewall.")
            return
        alive = now - self.last_ack < ACK_TIMEOUT_S
        if alive and self.state != "up":
            log.info("Server is receiving audio.")
            self.state = "up"
        elif not alive and self.state == "up":
            log.warning("Lost contact with server (no ACK for %.0f s) - still streaming, "
                        "will reconnect automatically.", ACK_TIMEOUT_S)
            self.state = "down"


class NetMicClient:
    def __init__(self, args, server_addr):
        self.args = args
        self.addr = server_addr
        self.session = random.randint(1, 0xFFFF)
        self.seq = 0
        self.audio_q = queue.Queue(maxsize=MAX_QUEUE_BLOCKS)
        self.stop = threading.Event()
        self.link = LinkMonitor()
        self.sent = 0
        self.dropped = 0            # blocks discarded because the sender fell behind
        self.send_errors = 0
        self.rate_warned = False
        self._next_open = 0.0
        self.sock = self._open_socket()

    # ---- setup -----------------------------------------------------------
    @staticmethod
    def _open_socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:                        # DSCP EF: hint for low-latency queueing on some routers
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        except OSError:
            pass
        return sock

    def _open_stream(self):
        stream = sd.RawInputStream(
            samplerate=self.args.rate,
            blocksize=self.args.chunk,
            device=self.args.device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=self._audio_callback,
        )
        stream.start()
        return stream

    def _audio_callback(self, indata, frames, time_info, status):
        # Runs on the audio thread: no logging, no blocking.
        data = bytes(indata)
        try:
            self.audio_q.put_nowait(data)
        except queue.Full:          # sender stalled: drop the oldest block to bound latency
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.audio_q.put_nowait(data)
            except queue.Full:
                pass
            self.dropped += 1

    # ---- network -----------------------------------------------------------
    def _send_block(self, block: bytes):
        for off in range(0, len(block), MAX_PAYLOAD):
            pkt = HEADER.pack(MAGIC, T_AUDIO, self.session, self.seq) + block[off:off + MAX_PAYLOAD]
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            try:
                self.sock.sendto(pkt, self.addr)
                self.sent += 1
            except OSError as exc:  # e.g. "Network is unreachable" while Wi-Fi drops
                self.send_errors += 1
                log_every("send", 5, logging.WARNING,
                          "Send failed (%s) - dropping audio until the network is back.", exc)

    def _send_bye(self):
        pkt = HEADER.pack(MAGIC, T_BYE, self.session, self.seq)
        for _ in range(2):
            try:
                self.sock.sendto(pkt, self.addr)
            except OSError:
                break

    def _listen(self):
        """Receives ACKs from the server (own thread; audio never waits on this)."""
        while not self.stop.is_set():
            try:
                data, src = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    break
                time.sleep(0.1)     # e.g. ICMP unreachable reported as an error
                continue
            if len(data) < HEADER.size or src[0] != self.addr[0]:
                continue
            magic, ptype, session, _ = HEADER.unpack_from(data)
            if magic != MAGIC or ptype != T_ACK or session != self.session:
                continue
            self.link.on_ack()
            if len(data) >= HEADER.size + 4 and not self.rate_warned:
                (server_rate,) = struct.unpack_from(">I", data, HEADER.size)
                if server_rate != self.args.rate:
                    self.rate_warned = True
                    log.warning("Sample-rate mismatch: server plays at %d Hz, client sends %d Hz "
                                "- audio will sound wrong. Use the same --rate on both.",
                                server_rate, self.args.rate)

    # ---- main loop -----------------------------------------------------------
    def _mic_name(self) -> str:
        try:
            return sd.query_devices(self.args.device, "input")["name"]
        except Exception:
            return "default input"

    def run(self) -> int:
        try:
            stream = self._open_stream()
        except Exception as exc:
            log.error("Cannot open microphone: %s", exc)
            log.error("Try --list-devices, --device <index> or a different --rate. "
                      "On Termux make sure the microphone permission is granted.")
            self.sock.close()
            return 3

        threading.Thread(target=self._listen, daemon=True).start()
        log.info("Microphone: %s", self._mic_name())
        log.info("Streaming audio to %s:%d (%d Hz, 16-bit, mono, %d frames/block)",
                 self.addr[0], self.addr[1], self.args.rate, self.args.chunk)
        log.info("Press Ctrl+C to stop.")

        next_stats = time.monotonic() + STATS_INTERVAL_S
        try:
            while True:
                stream = self._ensure_stream(stream)
                try:
                    block = self.audio_q.get(timeout=0.25)
                except queue.Empty:
                    block = None
                if block:
                    self._send_block(block)
                self.link.check()
                now = time.monotonic()
                if now >= next_stats:
                    next_stats = now + STATS_INTERVAL_S
                    log.info("Sent %d pkts | send errors %d | dropped blocks %d",
                             self.sent, self.send_errors, self.dropped)
        except KeyboardInterrupt:
            log.info("Stopping.")
        finally:
            self.stop.set()
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._send_bye()
            self.sock.close()
        return 0

    def _ensure_stream(self, stream):
        """Reopens the microphone if the audio device disappeared mid-run."""
        if stream is not None and stream.active:
            return stream
        now = time.monotonic()
        if now < self._next_open:
            return stream
        self._next_open = now + 2.0
        if stream is not None:
            log.error("Microphone stream stopped - trying to reopen it...")
            try:
                stream.close()
            except Exception:
                pass
        try:
            new_stream = self._open_stream()
            log.info("Microphone restored.")
            return new_stream
        except Exception as exc:
            log.error("Reopen failed: %s", exc)
            return stream


def parse_device(value):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def parse_args():
    p = argparse.ArgumentParser(description="NetMic client: stream the microphone to a NetMic server over UDP.")
    p.add_argument("-s", "--server", default=SERVER_IP,
                   help=f"server IP or hostname (default: SERVER_IP in this file, currently {SERVER_IP})")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default {DEFAULT_PORT})")
    p.add_argument("-r", "--rate", type=int, default=DEFAULT_RATE, help="sample rate; must match the server")
    p.add_argument("-c", "--chunk", type=int, default=DEFAULT_CHUNK,
                   help="capture block size in frames (512 or 1024)")
    p.add_argument("-d", "--device", default=None,
                   help="input device index or name (see --list-devices); default: system default")
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
    if args.chunk <= 0 or args.rate <= 0 or not (0 < args.port < 65536):
        log.error("Invalid --chunk, --rate or --port.")
        return 2
    args.device = parse_device(args.device)
    try:
        ip = socket.gethostbyname(args.server)
    except socket.gaierror:
        log.error("Cannot resolve server address '%s'. Pass the PC's IP with --server.", args.server)
        return 2
    return NetMicClient(args, (ip, args.port)).run()


if __name__ == "__main__":
    sys.exit(main())