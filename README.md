# NetMic
 
Use a smartphone as a wireless microphone for your PC over Wi-Fi.
Raw 16-bit / 44.1 kHz / mono PCM is streamed over UDP from the phone to the PC.
 
```
netmic/
├── computer_server/server.py   # PC: receive UDP -> play / virtual mic
├── phone_client/client.py      # phone (Termux) or any computer: mic -> UDP
├── requirements.txt
└── README.md
```
 
Both scripts use [`sounddevice`](https://python-sounddevice.readthedocs.io/) (PortAudio) with `dtype="int16"`
(the equivalent of PyAudio's `paInt16`). Only the *Raw* stream classes are used, so **NumPy is not required**.
 
## 1. Install
 
Python 3.8+ on both devices, and PC and phone on the **same Wi-Fi network**.
 
### PC (server)
 
| OS | Steps |
|---|---|
| Windows | `pip install sounddevice` (PortAudio is bundled in the wheel) |
| macOS | `pip install sounddevice` (PortAudio is bundled in the wheel) |
| Linux (Debian/Ubuntu) | `sudo apt install libportaudio2` then `pip install sounddevice` |
 
### Phone (client) – Termux
 
Install Termux from **F-Droid** (the Play Store build is outdated), then:
 
```bash
pkg update
pkg install python portaudio libffi clang
pip install sounddevice
```
 
Grant the microphone permission: *Android Settings → Apps → Termux → Permissions → Microphone*.
 
Then check that PortAudio sees a capture device:
 
```bash
python phone_client/client.py --list-devices
```
 
**Caveat (not tested by me on a real phone):** PortAudio has no native Android backend. If the list shows no input
device in Termux, the commonly used route is to run a PulseAudio server inside Termux
(`pkg install pulseaudio`, `pulseaudio --start --exit-idle-time=-1`, `pactl load-module module-sles-source`) and re-check
`--list-devices`. If that doesn't work, the client still runs unchanged on a laptop for testing, and any Android app that
can send raw UDP can talk to the server using the protocol below.
 
Copy `phone_client/client.py` to the phone (e.g. `git clone`, `scp`, or `termux-setup-storage` + copy).
 
## 2. Run
 
**On the PC** – find the IP the server prints on startup (or use `ipconfig` / `ip a`):
 
```bash
python computer_server/server.py
```
 
**On the phone**:
 
```bash
python client.py --server 192.168.1.50
```
 
Instead of `--server` you can edit `SERVER_IP` at the top of `client.py`.
 
Expected logs:
 
```
[INFO] Receiving audio packets on UDP port 5005 (44100 Hz, 16-bit, mono)
[INFO] Phone client target:  python client.py --server 192.168.1.50 --port 5005
...
[INFO] Streaming audio to 192.168.1.50:5005 (44100 Hz, 16-bit, mono, 512 frames/block)
[INFO] Client connected: 192.168.1.23:41234 - receiving audio
[INFO] Server is receiving audio.
```
 
Use headphones on the PC while testing, otherwise the speaker feeds back into the phone microphone.
 
### Useful options
 
| Option | Where | Meaning |
|---|---|---|
| `--server IP` | client | PC address (overrides `SERVER_IP`) |
| `--port N` | both | UDP port, default 5005 |
| `--rate N` | both | sample rate, default 44100 – **must match** (client warns on mismatch) |
| `--chunk N` | both | block size in frames, default 512 (1024 works; the client splits it into 2 datagrams to stay under the Wi-Fi MTU) |
| `--device / --output-device` | client / server | audio device index or name |
| `--list-devices` | both | show audio devices |
| `--prebuffer-ms N` | server | start-up buffer (default 60). Lower = less latency, more dropouts |
| `--max-buffer-ms N` | server | buffer cap (default 250); older audio is dropped beyond it |
 
## 3. Use it as a real microphone on the PC (optional)
 
Install a virtual audio cable ([VB-Cable](https://vb-audio.com/Cable/) on Windows, BlackHole on macOS, a PulseAudio
null-sink on Linux), then send the server output into it:
 
```bash
python computer_server/server.py --list-devices
python computer_server/server.py --output-device 12      # index of "CABLE Input"
```
 
Select **CABLE Output** as the microphone in Discord/OBS/Zoom/etc. Prefer the numeric index: on Windows the same device
appears once per audio API and a name match can be ambiguous.
 
## 4. Troubleshooting
 
- **Client says "No response from server yet"** – wrong IP, different network (guest Wi-Fi/AP isolation blocks
  device-to-device traffic), or the PC firewall blocks UDP 5005. On Windows allow Python through the firewall or
  `netsh advfirewall firewall add rule name=NetMic dir=in action=allow protocol=UDP localport=5005`.
- **Crackling / dropouts** – raise `--prebuffer-ms` (e.g. 100), or try `--chunk 1024`. Watch `lost` and `underruns` in the
  server's 5-second stats line.
- **Growing delay** – clocks of phone and PC drift slightly; the server caps the buffer (`--max-buffer-ms`), which may
  cause an occasional small click instead of ever-growing latency.
- **Pitch is wrong** – sample-rate mismatch; use the same `--rate` on both.
- **Cannot open microphone/output** – use `--list-devices` and pass an explicit index; some devices don't support 44.1 kHz.
Typical end-to-end latency is roughly 100–150 ms (capture block + Wi-Fi + prebuffer + output device). Wi-Fi jitter is the
main variable; expect it to be noticeable for live monitoring but fine for calls and recording.
 
## 5. How robustness is handled
 
- **Packet loss:** every datagram carries a sequence number. Small gaps (≤ 8 packets) are filled with silence so timing
  stays correct; late or duplicate packets are discarded; loss is reported in the stats.
- **Jitter / underruns:** the server buffers `--prebuffer-ms` before playing; on an underrun it outputs silence and
  re-buffers instead of stuttering.
- **Connection drops:** the client never blocks on the network (audio is dropped, streaming continues) and detects a dead
  link via 1 Hz ACKs from the server; the server forgets a silent client after 5 s and accepts it again on return.
  Restarting the client is picked up automatically (new session id).
- **Bad input:** malformed or foreign packets are counted and ignored. Only one client (IP) is served at a time.
- **Audio device loss:** both sides try to reopen the audio device every 2 s.
## Protocol
 
All values big-endian. Header (9 bytes): `"NM"` magic · `type u8` · `session u16` · `seq u32`.
 
| type | name | direction | payload |
|---|---|---|---|
| 1 | AUDIO | phone → PC | raw int16 mono PCM, ≤ 1024 bytes |
| 2 | ACK | PC → phone | `u32` server sample rate (sent ~1×/s) |
| 3 | BYE | phone → PC | none |
 
> Note: the stream is **unencrypted and unauthenticated**. Use it on a network you trust.
