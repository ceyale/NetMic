"""
main_gui.py - the NetMic control panel window.

Threading model:
    PortAudio thread  -> AudioCaptureEngine._callback() -> RingBuffer.push()
    Sender thread      -> RingBuffer.pop() -> UDP socket.sendto()
    Receiver thread     -> UDP socket.recvfrom() -> updates NetworkTransport stats
    Pinger thread       -> UDP socket.sendto() (PING) every second
    Qt main thread      -> QTimer polls AudioCaptureEngine / NetworkTransport
                            snapshot methods every ~66ms and repaints widgets

No audio or network thread ever touches a Qt widget directly. They only
write to lock-protected state; the GUI thread is the sole reader of that
state and the sole writer of the UI. This avoids the classic "updated a
widget from a worker thread" crash class without needing a signal per frame.
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui_client.audio_core import AudioCaptureEngine, RingBuffer, list_input_devices
from gui_client.network_stream import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_WARNING,
    NetworkTransport,
)
from gui_client.widgets import LineGraphWidget, VUMeterWidget

POLL_INTERVAL_MS = 66  # ~15 Hz: smooth enough for meters, cheap enough to idle at

STATE_COLORS = {
    STATE_DISCONNECTED: QColor(140, 140, 148),
    STATE_CONNECTING: QColor(230, 190, 60),
    STATE_CONNECTED: QColor(70, 200, 120),
    STATE_WARNING: QColor(230, 110, 60),
}
STATE_LABELS = {
    STATE_DISCONNECTED: "Disconnected",
    STATE_CONNECTING: "Connecting...",
    STATE_CONNECTED: "Connected",
    STATE_WARNING: "Packet Loss Warning",
}

SAMPLE_RATES = [16000, 44100, 48000]
UNSUPPORTED_PROTOCOLS = {"TCP", "RTP"}
UNSUPPORTED_CODECS = {"Opus", "AAC"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NetMic Control Panel")
        self.resize(880, 640)

        self._ring = RingBuffer(max_blocks=64)
        self._audio = AudioCaptureEngine(self._ring)
        self._net = NetworkTransport(self._ring)
        self._net.connection_changed.connect(self._on_connection_changed)
        self._net.fatal_error.connect(self._on_fatal_error)

        self._build_ui()
        self._refresh_devices()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    # ---- UI construction -----------------------------------------------------------
    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_dashboard_tab(), "Dashboard")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_diagnostics_tab(), "Diagnostics")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("Ready.")

    def _build_dashboard_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        status_row = QHBoxLayout()
        self.status_dot = QLabel("\u25CF")
        self.status_dot.setStyleSheet(f"color: {STATE_COLORS[STATE_DISCONNECTED].name()}; font-size: 18px;")
        self.status_label = QLabel(STATE_LABELS[STATE_DISCONNECTED])
        self.status_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        button_row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumHeight(44)
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.stream_btn = QPushButton("Start Streaming")
        self.stream_btn.setMinimumHeight(44)
        self.stream_btn.setEnabled(False)
        self.stream_btn.clicked.connect(self._toggle_stream)
        button_row.addWidget(self.connect_btn)
        button_row.addWidget(self.stream_btn)
        layout.addLayout(button_row)

        vu_box = QGroupBox("Microphone Level")
        vu_layout = QVBoxLayout(vu_box)
        self.vu_meter = VUMeterWidget()
        vu_layout.addWidget(self.vu_meter)
        layout.addWidget(vu_box)

        stats_box = QGroupBox("At a Glance")
        stats_layout = QFormLayout(stats_box)
        self.lbl_rtt = QLabel("--")
        self.lbl_throughput = QLabel("--")
        self.lbl_loss = QLabel("--")
        stats_layout.addRow("Round-trip time:", self.lbl_rtt)
        stats_layout.addRow("Throughput:", self.lbl_throughput)
        stats_layout.addRow("Server-reported loss:", self.lbl_loss)
        layout.addWidget(stats_box)

        layout.addStretch(1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        device_box = QGroupBox("Input Device")
        device_layout = QHBoxLayout(device_box)
        self.device_combo = QComboBox()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_devices)
        device_layout.addWidget(self.device_combo, 1)
        device_layout.addWidget(refresh_btn)
        layout.addWidget(device_box)

        net_box = QGroupBox("Network")
        net_layout = QFormLayout(net_box)
        self.ip_edit = QLineEdit("127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5005)
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["UDP", "TCP", "RTP"])
        self.protocol_combo.currentTextChanged.connect(self._on_protocol_changed)
        net_layout.addRow("Target IP address:", self.ip_edit)
        net_layout.addRow("Port:", self.port_spin)
        net_layout.addRow("Protocol:", self.protocol_combo)
        layout.addWidget(net_box)

        audio_box = QGroupBox("Audio Parameters")
        audio_layout = QFormLayout(audio_box)
        self.rate_combo = QComboBox()
        for r in SAMPLE_RATES:
            self.rate_combo.addItem(f"{r} Hz", r)
        self.rate_combo.setCurrentIndex(SAMPLE_RATES.index(44100))
        self.bitdepth_label = QLabel("16-bit (fixed)")
        chunk_row = QWidget()
        chunk_row_layout = QHBoxLayout(chunk_row)
        chunk_row_layout.setContentsMargins(0, 0, 0, 0)
        self.chunk_slider = QSlider(Qt.Orientation.Horizontal)
        self.chunk_slider.setRange(256, 2048)
        self.chunk_slider.setSingleStep(64)
        self.chunk_slider.setValue(512)
        self.chunk_value_label = QLabel("512 frames (~11.6 ms)")
        self.chunk_slider.valueChanged.connect(self._on_chunk_changed)
        chunk_row_layout.addWidget(self.chunk_slider)
        chunk_row_layout.addWidget(self.chunk_value_label)
        audio_layout.addRow("Sample rate:", self.rate_combo)
        audio_layout.addRow("Bit depth:", self.bitdepth_label)
        audio_layout.addRow("Buffer / chunk size:", chunk_row)
        layout.addWidget(audio_box)

        codec_box = QGroupBox("Codec")
        codec_layout = QFormLayout(codec_box)
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["Raw PCM", "Opus", "AAC"])
        self.codec_combo.currentTextChanged.connect(self._on_codec_changed)
        codec_layout.addRow("Codec:", self.codec_combo)
        self.codec_note = QLabel("")
        self.codec_note.setWordWrap(True)
        self.codec_note.setStyleSheet("color: #c98;")
        codec_layout.addRow(self.codec_note)
        layout.addWidget(codec_box)

        layout.addStretch(1)
        return page

    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.rtt_graph = LineGraphWidget("RTT", "ms", color=QColor(90, 170, 250))
        self.throughput_graph = LineGraphWidget("Throughput", "kbps", color=QColor(120, 210, 140))
        layout.addWidget(self.rtt_graph)
        layout.addWidget(self.throughput_graph)

        counters_box = QGroupBox("Counters")
        counters_layout = QFormLayout(counters_box)
        self.lbl_sent = QLabel("--")
        self.lbl_dropped = QLabel("--")
        self.lbl_errors = QLabel("--")
        self.lbl_server_rx = QLabel("--")
        counters_layout.addRow("Packets sent:", self.lbl_sent)
        counters_layout.addRow("Blocks dropped locally:", self.lbl_dropped)
        counters_layout.addRow("Send errors:", self.lbl_errors)
        counters_layout.addRow("Server received / lost:", self.lbl_server_rx)
        layout.addWidget(counters_box)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log_console)
        layout.addWidget(log_box, 1)

        return page

    # ---- device handling -----------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.device_combo.clear()
        self.device_combo.addItem("System default", None)
        for idx, name in list_input_devices():
            self.device_combo.addItem(f"[{idx}] {name}", idx)

    # ---- settings guardrails -----------------------------------------------------------
    def _on_protocol_changed(self, protocol: str) -> None:
        if protocol in UNSUPPORTED_PROTOCOLS:
            self.connect_btn.setEnabled(False)
            self.statusBar().showMessage(
                f"{protocol} is not implemented in this build - only UDP is functional. "
                "Switch back to UDP to connect.", 8000)
        else:
            self.connect_btn.setEnabled(self._net.get_state() != STATE_CONNECTED)
            self.statusBar().clearMessage()

    def _on_codec_changed(self, codec: str) -> None:
        if codec in UNSUPPORTED_CODECS:
            self.codec_note.setText(
                f"{codec} is not implemented in this build (no codec library is bundled). "
                "Streaming will use Raw PCM until this is selected back."
            )
        else:
            self.codec_note.setText("")

    def _on_chunk_changed(self, value: int) -> None:
        rate = self.rate_combo.currentData() or 44100
        ms = 1000.0 * value / rate
        self.chunk_value_label.setText(f"{value} frames (~{ms:.1f} ms)")

    def _selected_codec_is_raw(self) -> bool:
        return self.codec_combo.currentText() not in UNSUPPORTED_CODECS

    # ---- connect / stream actions -----------------------------------------------------------
    def _toggle_connect(self) -> None:
        if self._net.get_state() == STATE_DISCONNECTED:
            protocol = self.protocol_combo.currentText()
            if protocol in UNSUPPORTED_PROTOCOLS:
                QMessageBox.warning(self, "Unsupported protocol",
                                    f"{protocol} is not implemented in this build. Use UDP.")
                return
            host = self.ip_edit.text().strip()
            if not host:
                QMessageBox.warning(self, "Missing address", "Enter a target IP address first.")
                return
            port = self.port_spin.value()
            self.ip_edit.setEnabled(False)
            self.port_spin.setEnabled(False)
            self.protocol_combo.setEnabled(False)
            self._net.connect_to(host, port)
            self.connect_btn.setText("Disconnect")
        else:
            if self._net.is_streaming():
                self._toggle_stream()
            self._net.disconnect()
            self.connect_btn.setText("Connect")
            self.stream_btn.setEnabled(False)
            self.ip_edit.setEnabled(True)
            self.port_spin.setEnabled(True)
            self.protocol_combo.setEnabled(True)

    def _toggle_stream(self) -> None:
        if not self._net.is_streaming():
            if not self._selected_codec_is_raw():
                QMessageBox.information(
                    self, "Codec unavailable",
                    f"{self.codec_combo.currentText()} is not implemented in this build. "
                    "Streaming will use Raw PCM instead.")
            device = self.device_combo.currentData()
            rate = self.rate_combo.currentData() or 44100
            chunk = self.chunk_slider.value()
            if not self._audio.start(device, rate, chunk):
                QMessageBox.critical(self, "Microphone error",
                                     "Could not open the selected input device. "
                                     "Check the Diagnostics log for details.")
                return
            if not self._net.start_streaming():
                self._audio.stop()
                return
            self.stream_btn.setText("Stop Streaming")
            self.rate_combo.setEnabled(False)
            self.chunk_slider.setEnabled(False)
            self.device_combo.setEnabled(False)
        else:
            self._net.stop_streaming(send_bye=True)
            self._audio.stop()
            self.stream_btn.setText("Start Streaming")
            self.rate_combo.setEnabled(True)
            self.chunk_slider.setEnabled(True)
            self.device_combo.setEnabled(True)

    # ---- signal handlers -----------------------------------------------------------
    def _on_connection_changed(self, state: str) -> None:
        self.status_dot.setStyleSheet(f"color: {STATE_COLORS[state].name()}; font-size: 18px;")
        self.status_label.setText(STATE_LABELS[state])
        self.stream_btn.setEnabled(state in (STATE_CONNECTED, STATE_WARNING))
        if state == STATE_DISCONNECTED and self.stream_btn.text() == "Stop Streaming":
            self._audio.stop()
            self.stream_btn.setText("Start Streaming")
            self.rate_combo.setEnabled(True)
            self.chunk_slider.setEnabled(True)
            self.device_combo.setEnabled(True)

    def _on_fatal_error(self, message: str) -> None:
        QMessageBox.critical(self, "Connection error", message)
        self.connect_btn.setText("Connect")
        self.ip_edit.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.protocol_combo.setEnabled(True)

    # ---- polling -----------------------------------------------------------
    def _poll(self) -> None:
        rms, peak = self._audio.get_level()
        self.vu_meter.set_level(rms, peak)

        stats = self._net.get_stats()
        rtt = stats["rtt_ms"]
        self.lbl_rtt.setText(f"{rtt:.0f} ms" if rtt is not None else "--")
        self.lbl_throughput.setText(f"{stats['throughput_kbps']:.1f} kbps")
        if stats["server_rate"] is not None:
            total = stats["server_received"] + stats["server_lost"]
            pct = (100.0 * stats["server_lost"] / total) if total else 0.0
            self.lbl_loss.setText(f"{stats['server_lost']} pkts ({pct:.1f}%)")
        else:
            self.lbl_loss.setText("--")
        self.lbl_sent.setText(str(stats["sent_pkts"]))
        self.lbl_dropped.setText(str(stats["dropped_blocks"]))
        self.lbl_errors.setText(str(stats["send_errors"]))
        if stats["server_rate"] is not None:
            self.lbl_server_rx.setText(f"{stats['server_received']} / {stats['server_lost']}")
        else:
            self.lbl_server_rx.setText("--")

        self.rtt_graph.add_point(rtt)
        self.throughput_graph.add_point(stats["throughput_kbps"])

        for line in self._audio.drain_log() + self._net.drain_log():
            self.log_console.appendPlainText(line)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._net.disconnect()
        self._audio.stop()
        super().closeEvent(event)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())