<div align="center">

# Adaptive Digital Communication System with BER Optimization

**Real-time adaptive wireless communication simulator built on Arduino + Python**

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Arduino](https://img.shields.io/badge/Arduino-Uno%20%2F%20Nano-00979D?style=flat-square&logo=arduino&logoColor=white)](https://www.arduino.cc/)
[![Dash](https://img.shields.io/badge/Dashboard-Plotly%20Dash-00B4D8?style=flat-square)](https://dash.plotly.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey?style=flat-square)]()

<br/>

> Simulate a full wireless communication link — noise injection, forward error correction, adaptive modulation — and watch every metric update live in a web dashboard, all driven by real Arduino hardware over Bluetooth.

</div>

---

## What this project does

Two Arduino boards communicate over Bluetooth while simulating real-world wireless impairments:

- The **RX board** measures Bit Error Rate (BER) from incoming packets, runs Forward Error Correction (FEC), and automatically selects the best modulation scheme.
- The **TX board** transmits data, applies channel-model noise, and adapts its modulation on command from RX.
- A **Python web dashboard** streams live telemetry over USB serial and plots BER, SNR, throughput, constellation diagrams, and a full event log in real time.

```
┌─────────────────┐         USB Serial          ┌──────────────────────────┐
│  Python Dashboard│◄───────────────────────────►│      TX Arduino          │
│  (adapt.py)      │         COM5                │  TX.ino  │  20×4 LCD    │
└─────────────────┘                              └──────────┼───────────────┘
                                                            │  Bluetooth (HC-05)
                                                            │
                                                 ┌──────────┼───────────────┐
                                                 │      RX Arduino          │
                                                 │  RX.ino  │  20×4 LCD    │
                                                 └──────────────────────────┘
```

### Key capabilities

| Feature | Details |
|---|---|
| **Modulation schemes** | BPSK → QPSK → 16QAM, auto-selected by BER |
| **FEC algorithms** | Hamming(7,4) · Convolutional · Repetition(3×) · None |
| **Channel models** | AWGN · Rayleigh fading |
| **SNR range** | 1 – 25 dB (configurable from dashboard) |
| **BER tracking** | EMA-smoothed, per-packet, + SNR-derived estimate |
| **Live display** | 4-tab Dash web app with real-time Plotly charts |
| **LCD displays** | Flicker-free 20×4 status on both boards |

---

## Repository structure

```
Adaptive_Hardware/
├── adapt.py              # Dashboard — dual USB serial (TX + RX both connected)
├── adapt1.py             # Dashboard — single USB serial (RX relayed via Bluetooth)
├── requirements.txt      # Python dependencies
├── TX/
│   └── TX.ino            # Arduino firmware for the transmitter
├── RX/
│   └── RX.ino            # Arduino firmware for the receiver
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   └── PULL_REQUEST_TEMPLATE.md
└── LICENSE
```

**Which dashboard script to use:**

| Scenario | Script |
|---|---|
| Both Arduinos plugged into PC via USB | `adapt.py` |
| Only TX plugged in, RX runs standalone (BT relay) | `adapt1.py` |

---

## Hardware requirements

| Component | Qty | Notes |
|---|---|---|
| Arduino Uno / Nano / Mega | 2 | One TX, one RX |
| HC-05 or HC-06 Bluetooth module | 2 | Paired as master / slave |
| 20×4 parallel LCD display | 2 | Standard HD44780 |
| USB cable | 1 (min) | TX must connect to PC |
| Jumper wires | — | |
| External 5 V supply (optional) | 1 | To run RX standalone |

---

## Wiring

### Both Arduinos share the same pinout

| Arduino Pin | Connected to |
|---|---|
| D12 | LCD RS |
| D11 | LCD Enable |
| D5 | LCD D4 |
| D4 | LCD D5 |
| D3 | LCD D6 |
| D2 | LCD D7 |
| D7 | Bluetooth module TX pin |
| D6 | Bluetooth module RX pin |
| 5V / GND | LCD and Bluetooth VCC / GND |

> **HC-05 note:** The module RX pin is 3.3 V tolerant. Put a voltage divider (1 kΩ + 2 kΩ) on the Arduino TX → module RX line to avoid damage.

### Bluetooth pairing

Before first use, configure one module as **Master** and one as **Slave** via AT commands. The default pairing PIN is `1234` or `0000`. The Master module auto-connects to the Slave on power-up — the LED changes from fast-blink to slow-blink when linked.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/adaptive-hardware.git
cd adaptive-hardware
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

<details>
<summary>Manual install (no requirements.txt)</summary>

```bash
pip install dash plotly pyserial
```

</details>

### 3. Flash the Arduino firmware

1. Open **Arduino IDE 2.x** ([download](https://www.arduino.cc/en/software)).
2. Open `TX/TX.ino`, select the correct board and port, click **Upload**.
3. Repeat with `RX/RX.ino` on the second board.

Both sketches use only built-in Arduino libraries (`LiquidCrystal`, `SoftwareSerial`) — no Library Manager installs needed.

---

## Configuration

### Python — set your COM ports

Open `adapt.py` (dual USB) or `adapt1.py` (single USB) and update the port constants near the top:

```python
# adapt.py
RX_PORT   = "COM7"   # ← your RX Arduino port
TX_PORT   = "COM5"   # ← your TX Arduino port
BAUD_RATE = 9600
```

```python
# adapt1.py
TX_PORT   = "COM5"   # ← your TX Arduino port
BAUD_RATE = 9600
```

To list available ports:

```bash
python -c "import serial.tools.list_ports; [print(p) for p in serial.tools.list_ports.comports()]"
```

---

## Running

1. Power on both Arduinos. The LCDs show a welcome screen then switch to the live status view.
2. Wait for the Bluetooth LED to settle into slow-blink (link established).
3. Start the dashboard:

```bash
python adapt.py
# or
python adapt1.py
```

4. Open your browser at **http://127.0.0.1:8050**

The dashboard connects within a few seconds and all charts begin updating in real time.

---

## Dashboard overview

### Tab 1 — Live Status
Real-time message flow (TX original → noisy channel → RX raw → FEC decoded) with per-bit error highlighting. Hero strip shows BER, link health, active modulation, and SNR. Controls let you change SNR, FEC scheme, and channel model on the fly.

### Tab 2 — Signal Analysis
- BER vs Time (raw + EMA-smoothed overlay)
- SNR vs BER scatter against theoretical BPSK / QPSK / 16QAM curves
- Throughput vs SNR
- Live constellation diagram matching the active modulation

### Tab 3 — Diagnostics
- FEC performance comparison across all three schemes
- AWGN vs Rayleigh channel impact on BER
- Modulation zone timeline

### Tab 4 — Event Log
Colour-coded events (INFO / WARN / CRIT) for every modulation switch, BER zone crossing, and connection event. Filterable by severity.

---

## How the adaptive algorithm works

```
Every 1 second:
  1. RX estimates BER from SNR (with ±35% jitter to model estimation noise)
  2. RX updates a post-FEC measured BER via EMA (α = 0.3)
  3. BER for decision = measured (if packet received < 10 s ago) else SNR estimate
  4. Hysteretic thresholds select modulation:
       BER > 0.018  →  BPSK   (most robust)
       BER < 0.003  →  16QAM  (highest throughput)
       otherwise    →  QPSK
  5. On change: RX sends CMD_MOD to TX over Bluetooth
  6. Dashboard receives MOD_DECISION and updates all charts
```

---

## Serial protocol

All messages are newline-terminated. Binary bytes are hex-encoded to keep the line protocol clean.

| Direction | Message | Meaning |
|---|---|---|
| TX → RX | `H:HEXDATA` | Packet with Hamming(7,4) FEC |
| TX → RX | `R:HEXDATA` | Packet with Repetition(3×) FEC |
| TX → RX | `V:HEXDATA` | Packet with Convolutional FEC |
| TX → RX | `SNR:15` | Set SNR (1–25 dB) |
| TX → RX | `CH:RAY` / `CH:AWG` | Switch channel model |
| TX → RX | `FEC:HAM(7,4)` | Change FEC scheme |
| RX → Python | `BER:0.001234` | Estimated BER |
| RX → Python | `MOD_DECISION:QPSK` | Adaptation decision |
| RX → Python | `RX_DATA:HEX\|bits\|errs\|corr\|resid` | Per-packet telemetry |

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| [Dash](https://dash.plotly.com/) | ≥ 2.14 | Web dashboard framework |
| [Plotly](https://plotly.com/python/) | ≥ 5.18 | Interactive charts |
| [pyserial](https://pyserial.readthedocs.io/) | ≥ 3.5 | USB serial communication |

Arduino libraries: `LiquidCrystal` and `SoftwareSerial` (both bundled with Arduino IDE).

---

## Contributing

Bug reports and feature requests are welcome — use the issue templates.  
For code contributions, open a pull request using the PR template.

---

## License

This project is licensed under the [MIT License](LICENSE).
