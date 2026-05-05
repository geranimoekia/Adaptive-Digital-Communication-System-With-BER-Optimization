import binascii
import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go
import serial
import threading
import time
import math
import random
import webbrowser
from collections import deque
from plotly.subplots import make_subplots

# --- CONFIGURATION ---
# Single USB connection: TX (which relays all RX telemetry via Bluetooth)
TX_PORT   = 'COM5'
BAUD_RATE = 9600
AVG_WINDOW = 15

# --- GLOBAL DATA STORAGE ---
raw_ber_history    = deque([0.0] * 100, maxlen=100)
avg_ber_history    = deque([0.0] * 100, maxlen=100)
recent_samples     = deque(maxlen=AVG_WINDOW)
error_rate_history = deque([0.0] * 100, maxlen=100)
snr_ber_pairs      = deque(maxlen=200)
snr_ber_detail     = deque(maxlen=500)   # (snr, ber, mod, fec, channel)
throughput_data    = deque(maxlen=300)   # (snr, throughput_norm)
event_log          = deque(maxlen=500)

stats = {
    "tx_data":           "---",
    "tx_typing":         "",
    "tx_raw_bits":       "0",
    "tx_mod":            "16QAM",
    "tx_fec":            "HAM(7,4)",
    "tx_packets":        "0",
    "tx_last_seen":      0,
    "rx_data":           "WAITING...",
    "rx_data_corrected": "WAITING...",
    "rx_raw":            "0",
    "mod":               "16QAM",
    "ber":               "0.0000",
    "raw_bits":          "0",
    "error_bits":        "0",
    "corrected_bits":    "0",
    "residual_bits":     "0",
    "fec_type":          "HAM(7,4)",
    "status":            "OFFLINE",
    "rx_last_seen":      0,   # updated whenever forwarded RX data arrives
    "tx_binary":         "",
    "rx_binary_before":  "",
    "rx_binary_after":   "",
    "current_snr":       15,
    "current_channel":   "AWGN",
    "ber_zone":          "16QAM",
}

_prev_snr        = 15
_prev_fec_clean  = "HAM(7,4)"
_prev_ch_name    = "AWGN"
_last_clear_clks = 0
lock = threading.Lock()

MOD_BITS  = {'BPSK': 1, 'QPSK': 2, '16QAM': 4}
FEC_RATES = {'HAM(7,4)': 4/7, 'REP3X': 1/3, 'CONV': 1/2, 'NONE': 1.0}

# ---------------------------------------------------------------
# SERIAL
# ---------------------------------------------------------------
def open_serial(port, baud):
    try:
        s = serial.Serial(port, baud, timeout=0.1)
        print(f"[OK] Connected: {port}")
        return s
    except Exception as e:
        print(f"[ERR] Cannot open {port}: {e}")
        return None

ser_tx = open_serial(TX_PORT, BAUD_RATE)

# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------
def from_hex(h):
    """Decode a hex string back to the original byte string (latin-1 safe)."""
    try:
        return binascii.unhexlify(h.strip()).decode('latin-1')
    except Exception:
        return h  # fall back to raw if decoding fails

def string_to_binary(s):
    if not s or s in ("---", "WAITING...", "(garbled)"):
        return ""
    return " ".join(format(ord(c), '08b') for c in s)

_SEV_COLOR = {'INFO': '#34c759', 'WARN': '#ff9500', 'CRIT': '#ff3b30'}
_SEV_ICON  = {'INFO': 'ℹ', 'WARN': '⚠', 'CRIT': '●'}
_CAT_COLOR = {
    'ADAPTIVE':   '#0071e3',
    'PACKET':     '#5856d6',
    'QUALITY':    '#ff3b30',
    'CONFIG':     '#8e8e93',
    'CONNECTION': '#34c759',
}

def log_event(severity, category, msg):
    event_log.append({
        'ts':       time.strftime('%H:%M:%S'),
        'severity': severity,
        'category': category,
        'msg':      msg,
    })

def render_event_log(filt='ALL'):
    rows = []
    for ev in reversed(list(event_log)):
        if filt != 'ALL' and ev['severity'] != filt:
            continue
        sc = _SEV_COLOR.get(ev['severity'], '#8e8e93')
        cc = _CAT_COLOR.get(ev['category'], '#8e8e93')
        rows.append(html.Div([
            html.Span(ev['ts'],
                      style={'color': '#8e8e93', 'minWidth': '64px',
                             'display': 'inline-block', 'marginRight': '10px'}),
            html.Span(f"{_SEV_ICON.get(ev['severity'], '·')} {ev['severity']}",
                      style={'color': sc, 'minWidth': '52px', 'fontWeight': '700',
                             'display': 'inline-block', 'marginRight': '10px'}),
            html.Span(ev['category'],
                      style={'color': cc, 'minWidth': '84px',
                             'display': 'inline-block', 'marginRight': '10px'}),
            html.Span(ev['msg'], style={'color': '#1d1d1f', 'flex': '1'}),
        ], style={
            'display': 'flex', 'alignItems': 'center',
            'padding': '5px 10px',
            'borderBottom': '1px solid #f2f2f7',
            'fontSize': '11px',
            'fontFamily': "'SF Mono','Menlo','Monaco','Courier New',monospace",
        }))
    if not rows:
        return [html.Div("No events yet.",
                         style={'color': '#aeaeb2', 'padding': '24px',
                                'fontSize': '12px', 'textAlign': 'center'})]
    return rows

# ---------------------------------------------------------------
# TX LISTENER — handles both TX-native messages and RX telemetry
# forwarded by TX1 from the Bluetooth link
# ---------------------------------------------------------------
def tx_listener():
    buf = b""
    while True:
        if ser_tx is None:
            time.sleep(1); continue
        try:
            waiting = ser_tx.in_waiting
            if waiting:
                buf += ser_tx.read(waiting)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    print(f"[TX] {line}")
                    with lock:
                        stats["tx_last_seen"] = time.time()

                        # ── TX-native messages ─────────────────────────────
                        if line.startswith("TX_DATA:"):
                            msg = line[8:].strip()
                            if msg:
                                stats["tx_data"]   = msg
                                stats["tx_binary"] = string_to_binary(msg)
                                log_event('INFO', 'PACKET',
                                          f'TX → "{msg}" | {len(msg)*8} bits | '
                                          f'Mod={stats["tx_mod"]} | FEC={stats["tx_fec"]} | '
                                          f'BER={stats["ber"]} | SNR={stats["current_snr"]}dB')
                            stats["tx_typing"] = ""
                            stats["status"]    = "TRANSMITTING"

                        elif line.startswith("TX_BITS:"):
                            try: stats["tx_raw_bits"] = str(int(line[8:].strip()))
                            except ValueError: pass

                        elif line.startswith("TX_TYPING:"):
                            stats["tx_typing"] = line[10:].strip()

                        elif line == "TX_CLEARED":
                            stats["tx_typing"] = ""

                        elif line.startswith("TX_STATUS_LIVE:"):
                            parts = line[15:].split("|")
                            if len(parts) >= 3:
                                stats["tx_mod"] = parts[0]
                                stats["tx_fec"] = parts[1]
                                try: stats["tx_packets"] = parts[2].strip()
                                except: pass

                        elif line.startswith("TX_STATUS:"):
                            if line[10:].strip() in ("READY", "ONLINE"):
                                stats["status"] = "ONLINE"
                                log_event('INFO', 'CONNECTION', f'TX online | {TX_PORT}')

                        elif line.startswith("TX_MOD:"):
                            stats["tx_mod"] = line[7:].strip()

                        elif line.startswith("TX_FEC:"):
                            stats["tx_fec"] = line[7:].strip()

                        # ── Forwarded RX telemetry (via BT → TX1 → Serial) ─
                        elif line.startswith("BER:"):
                            stats["rx_last_seen"] = time.time()
                            try:
                                val = float(line[4:])
                                raw_ber_history.append(val)
                                recent_samples.append(val)
                                avg_val = sum(recent_samples) / len(recent_samples)
                                avg_ber_history.append(avg_val)
                                stats["ber"] = f"{avg_val:.6f}"
                                error_rate_history.append(avg_val * 100)

                                new_zone = ('BPSK' if avg_val >= 0.018
                                            else 'QPSK' if avg_val >= 0.006
                                            else '16QAM')
                                if new_zone != stats["ber_zone"]:
                                    sev = ('CRIT' if new_zone == 'BPSK'
                                           else 'WARN' if new_zone == 'QPSK'
                                           else 'INFO')
                                    log_event(sev, 'ADAPTIVE',
                                              f'BER zone: {stats["ber_zone"]} → {new_zone} | '
                                              f'BER={avg_val:.4f} | SNR={stats["current_snr"]}dB')
                                    stats["ber_zone"] = new_zone

                                if avg_val >= 0.018 and (not event_log or
                                        event_log[-1].get('category') != 'QUALITY' or
                                        not event_log[-1].get('msg', '').startswith('High BER')):
                                    log_event('CRIT', 'QUALITY',
                                              f'High BER alert | BER={avg_val:.4f} | '
                                              f'SNR={stats["current_snr"]}dB')

                                snr = stats["current_snr"]
                                snr_ber_pairs.append((snr, avg_val))
                                snr_ber_detail.append((
                                    snr, avg_val,
                                    stats["mod"], stats["fec_type"],
                                    stats["current_channel"]
                                ))
                                mb = MOD_BITS.get(stats["mod"], 2)
                                fr = FEC_RATES.get(stats["fec_type"], 1.0)
                                throughput_data.append((snr, mb * fr * max(0.0, 1.0 - avg_val)))
                            except ValueError:
                                pass

                        elif line.startswith("MOD_DECISION:"):
                            stats["rx_last_seen"] = time.time()
                            new_mod = line[13:].strip()
                            log_event('WARN', 'ADAPTIVE',
                                      f'Modulation switch → {new_mod} | '
                                      f'BER={stats["ber"]} | SNR={stats["current_snr"]}dB')
                            stats["mod"] = new_mod
                            # TX1 already updated its display via CMD_MOD: from RX1;
                            # send SET_MOD: anyway for resilience
                            if ser_tx:
                                try:
                                    ser_tx.write(f"SET_MOD:{new_mod}\n".encode())
                                    ser_tx.flush()
                                except Exception as e:
                                    print(f"[FORWARD ERR] {e}")

                        elif line.startswith("RX_DATA:"):
                            stats["rx_last_seen"] = time.time()
                            parts = line[8:].split("|")
                            if len(parts) >= 4:
                                errored_str = from_hex(parts[0]) if parts[0] else ""
                                stats["rx_data"]          = errored_str if errored_str else "(garbled)"
                                stats["rx_binary_before"] = string_to_binary(errored_str) if errored_str else ""
                                stats["raw_bits"]         = parts[1]
                                stats["error_bits"]       = parts[2]
                                stats["corrected_bits"]   = parts[3]
                                if len(parts) >= 5:
                                    stats["residual_bits"] = parts[4].strip()
                                try:
                                    tb  = float(parts[1]) if float(parts[1]) > 0 else 1
                                    eb  = float(parts[2])
                                    res = float(parts[4]) if len(parts) >= 5 else eb
                                    cor = float(parts[3])
                                    error_rate_history.append((res / tb) * 100)
                                    fec_fail = res > 0
                                    sev = 'WARN' if fec_fail else 'INFO'
                                    log_event(sev, 'PACKET',
                                              f'RX ← "{errored_str}" | {int(tb)} bits | '
                                              f'{int(eb)} err | {int(cor)} fixed | '
                                              f'{int(res)} residual | FEC={stats["fec_type"]}')
                                    if fec_fail and res / tb > 0.05:
                                        log_event('CRIT', 'QUALITY',
                                                  f'FEC decode failure | {int(res)}/{int(tb)} bits '
                                                  f'unrecovered | scheme={stats["fec_type"]}')
                                except (ValueError, ZeroDivisionError):
                                    pass

                        elif line.startswith("RX_CORRECTED:"):
                            stats["rx_last_seen"] = time.time()
                            corrected_str = from_hex(line[13:].strip())
                            stats["rx_data_corrected"] = corrected_str
                            stats["rx_binary_after"]   = string_to_binary(corrected_str)

                        elif line.startswith("RX_RAW:"):
                            stats["rx_last_seen"] = time.time()
                            stats["rx_raw"] = line[7:].strip()

                        elif line.startswith("FEC:"):
                            stats["rx_last_seen"] = time.time()
                            stats["fec_type"] = line[4:].strip()

                        elif line.startswith("RX_CONTROLLER:"):
                            stats["rx_last_seen"] = time.time()
                            stats["status"] = "ONLINE"
                            log_event('INFO', 'CONNECTION', 'RX online via Bluetooth')

        except serial.SerialException as e:
            print(f"[TX ERR] {e}"); time.sleep(1)
        except Exception as e:
            print(f"[TX ERR] {e}")
        time.sleep(0.005)


threading.Thread(target=tx_listener, daemon=True).start()

# ---------------------------------------------------------------
# DASH APP
# ---------------------------------------------------------------
app = dash.Dash(__name__)

MOD_COLORS = {'BPSK': '#ff3b30', 'QPSK': '#ff9500', '16QAM': '#34c759'}

APPLE_FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif"
APPLE_MONO = "'SF Mono', 'Menlo', 'Monaco', 'Courier New', monospace"

app.index_string = f'''
<!DOCTYPE html>
<html>
    <head>
        {{%metas%}}
        <title>Adaptive BER System</title>
        {{%favicon%}}
        {{%css%}}
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                background: #f2f2f7;
                font-family: {APPLE_FONT};
                margin: 0;
            }}
            .dash-radio-items label,
            .dash-checklist label,
            label {{ color: #1d1d1f !important; font-size: 13px !important; }}
            .rc-slider-mark-text {{
                color: #aeaeb2 !important;
                font-size: 10px !important;
                font-family: {APPLE_FONT} !important;
            }}
            .rc-slider-tooltip-inner {{
                background: #ffffff !important;
                color: #1d1d1f !important;
                border: 1px solid #e5e5ea !important;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1) !important;
                font-family: {APPLE_FONT} !important;
                font-size: 12px !important;
                border-radius: 6px !important;
            }}
            .dash-tab {{ border: none !important; }}
        </style>
    </head>
    <body>
        {{%app_entry%}}
        <footer>
            {{%config%}}
            {{%scripts%}}
            {{%renderer%}}
        </footer>
    </body>
</html>
'''

# ---------------------------------------------------------------
# LAYOUT HELPERS
# ---------------------------------------------------------------
def section_label(text):
    return html.Div(text, style={
        'color': '#aeaeb2', 'fontSize': '10px', 'fontWeight': '600',
        'letterSpacing': '0.8px', 'textTransform': 'uppercase', 'marginBottom': '12px',
    })


def card(children, padding='20px', radius='14px', extra=None):
    style = {
        'background': '#ffffff',
        'borderRadius': radius,
        'boxShadow': '0 1px 3px rgba(0,0,0,0.06), 0 4px 16px rgba(0,0,0,0.04)',
        'padding': padding,
    }
    if extra:
        style.update(extra)
    return html.Div(style=style, children=children)


def stat_chip(label, elem_id, color='#1d1d1f'):
    return html.Div([
        html.Div(label, style={
            'color': '#aeaeb2', 'fontSize': '9px', 'fontWeight': '600',
            'letterSpacing': '0.8px', 'textTransform': 'uppercase', 'marginBottom': '5px',
        }),
        html.Div(id=elem_id, style={
            'color': color, 'fontSize': '20px', 'fontWeight': '700',
            'fontFamily': APPLE_MONO,
        })
    ], style={
        'background': '#ffffff', 'padding': '14px 12px', 'borderRadius': '12px',
        'textAlign': 'center', 'flex': '1',
        'boxShadow': '0 1px 3px rgba(0,0,0,0.05)',
    })


def binary_card(label, elem_id):
    return html.Div([
        html.Div(label, style={
            'color': '#aeaeb2', 'fontSize': '9px', 'fontWeight': '600',
            'letterSpacing': '0.8px', 'textTransform': 'uppercase', 'marginBottom': '10px',
        }),
        html.Pre(id=elem_id, style={
            'color': '#1d1d1f', 'fontSize': '10px', 'fontFamily': APPLE_MONO,
            'margin': 0, 'wordBreak': 'break-all', 'whiteSpace': 'pre-wrap',
            'lineHeight': '1.6',
        })
    ], style={
        'background': '#f9f9f9', 'padding': '14px', 'borderRadius': '10px',
        'border': '1px solid #e5e5ea',
    })


def _diff_spans(text, reference):
    if not text or text in ('—',):
        return text
    spans = []
    for i, ch in enumerate(text):
        if i < len(reference) and ch != reference[i]:
            spans.append(html.Span(ch, style={'color': '#ff3b30', 'fontWeight': '700'}))
        else:
            spans.append(html.Span(ch))
    return spans


def flow_box(label, elem_id, accent_color, text_color='#1d1d1f'):
    return html.Div([
        html.Div(label, style={
            'color': accent_color, 'fontSize': '9px', 'fontWeight': '700',
            'letterSpacing': '1px', 'textTransform': 'uppercase', 'marginBottom': '12px',
        }),
        html.Div(id=elem_id, style={
            'color': text_color, 'fontSize': '28px', 'fontWeight': '700',
            'wordBreak': 'break-all', 'minHeight': '40px', 'lineHeight': '1.2',
        })
    ], style={
        'flex': 1, 'background': '#ffffff', 'padding': '18px 20px',
        'borderRadius': '12px',
        'borderTop': f'3px solid {accent_color}',
        'boxShadow': '0 1px 3px rgba(0,0,0,0.06)',
    })


def flow_arrow(label):
    return html.Div([
        html.Div(label, style={
            'color': '#c7c7cc', 'fontSize': '9px', 'letterSpacing': '0.5px',
            'marginBottom': '4px', 'textTransform': 'uppercase',
        }),
        html.Div("→", style={'color': '#c7c7cc', 'fontSize': '22px', 'lineHeight': '1'}),
    ], style={
        'display': 'flex', 'flexDirection': 'column', 'alignItems': 'center',
        'justifyContent': 'center', 'padding': '0 12px', 'flexShrink': 0,
    })


TAB_STYLE = {
    'backgroundColor': 'transparent', 'color': '#aeaeb2',
    'fontFamily': APPLE_FONT, 'fontSize': '13px', 'fontWeight': '500',
    'padding': '10px 22px', 'border': 'none', 'borderBottom': '2px solid transparent',
}
TAB_SELECTED_STYLE = {
    'backgroundColor': 'transparent', 'color': '#0071e3',
    'fontFamily': APPLE_FONT, 'fontSize': '13px', 'fontWeight': '600',
    'padding': '10px 22px', 'border': 'none',
    'borderTop': 'none', 'borderBottom': '2px solid #0071e3',
}

PLOT_BASE = dict(
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='#f7f7f9',
    font=dict(color='#3a3a3c', family=APPLE_FONT, size=11),
)
AXIS_STYLE = dict(
    gridcolor='#e8e8e8', linecolor='#8e8e93', zerolinecolor='#d1d1d6',
    tickcolor='#6e6e73', showline=True, showgrid=True, showticklabels=True,
    tickfont=dict(color='#3a3a3c', size=11),
    title_font=dict(color='#1d1d1f', size=12), automargin=True,
)

TP_CURVES = [
    {'label': 'BPSK — No FEC',   'value': 'bpsk_nofec'},
    {'label': 'QPSK — No FEC',   'value': 'qpsk_nofec'},
    {'label': '16QAM — No FEC',  'value': '16qam_nofec'},
    {'label': 'QPSK + HAM(7,4)', 'value': 'qpsk_ham'},
    {'label': 'QPSK + REP3X',    'value': 'qpsk_rep3x'},
    {'label': 'QPSK + CONV',     'value': 'qpsk_conv'},
    {'label': 'Live (adaptive)',  'value': 'live'},
]
TP_ALL = [c['value'] for c in TP_CURVES]

# ---------------------------------------------------------------
# APP LAYOUT
# ---------------------------------------------------------------
app.layout = html.Div(
    style={
        'backgroundColor': '#f2f2f7', 'fontFamily': APPLE_FONT,
        'padding': '16px 24px', 'minHeight': '100vh', 'color': '#1d1d1f',
    },
    children=[

        # ── TITLE ROW ─────────────────────────────────────────
        html.Div(
            style={'display': 'flex', 'alignItems': 'center',
                   'justifyContent': 'space-between', 'marginBottom': '14px'},
            children=[
                html.Div([
                    html.Div("Adaptive Communication System with BER Optimization",
                             style={'fontSize': '25px', 'fontWeight': '700',
                                    'color': '#1d1d1f', 'letterSpacing': '-0.3px'}),
                    html.Div("Standalone RX — all telemetry relayed via Bluetooth through TX",
                             style={'fontSize': '12px', 'color': '#aeaeb2', 'marginTop': '2px'}),
                ]),
                html.Div(style={'display': 'flex', 'gap': '8px'}, children=[
                    html.Div(id='conn-rx',
                             style={'padding': '5px 12px', 'borderRadius': '20px',
                                    'fontSize': '11px', 'fontWeight': '500'}),
                    html.Div(id='conn-tx',
                             style={'padding': '5px 12px', 'borderRadius': '20px',
                                    'fontSize': '11px', 'fontWeight': '500'}),
                ]),
            ]
        ),

        # ── HERO STRIP ────────────────────────────────────────
        html.Div(
            style={'display': 'grid',
                   'gridTemplateColumns': '2fr 1.2fr 1.8fr 1fr',
                   'gap': '12px', 'marginBottom': '14px'},
            children=[
                card([
                    section_label("Bit Error Rate"),
                    html.Div(id='hero-ber', style={
                        'fontSize': '44px', 'fontWeight': '700',
                        'letterSpacing': '-1.5px', 'lineHeight': '1', 'fontFamily': APPLE_MONO,
                    }),
                    html.Div(id='hero-ber-zone', style={
                        'fontSize': '11px', 'fontWeight': '500',
                        'marginTop': '6px', 'color': '#aeaeb2',
                    }),
                ]),
                card([
                    section_label("Link Status"),
                    html.Div(id='hero-status', style={'marginTop': '4px'}),
                    html.Div(id='hero-fec', style={
                        'fontSize': '12px', 'color': '#aeaeb2',
                        'marginTop': '6px', 'fontWeight': '500',
                    }),
                ]),
                card([
                    section_label("Modulation"),
                    html.Div(style={'display': 'flex', 'gap': '8px', 'marginTop': '4px'},
                             children=[
                                 html.Div(id='mod-ind-BPSK',  children='BPSK'),
                                 html.Div(id='mod-ind-QPSK',  children='QPSK'),
                                 html.Div(id='mod-ind-16QAM', children='16QAM'),
                             ]),
                    html.Div("Auto-selected by RX controller",
                             style={'fontSize': '11px', 'color': '#c7c7cc', 'marginTop': '8px'}),
                ]),
                card([
                    section_label("Simulated SNR"),
                    html.Div(id='hero-snr', style={
                        'fontSize': '36px', 'fontWeight': '700',
                        'color': '#0071e3', 'letterSpacing': '-1px',
                        'lineHeight': '1', 'fontFamily': APPLE_MONO,
                    }),
                    html.Div("dB", style={
                        'fontSize': '13px', 'color': '#aeaeb2',
                        'fontWeight': '500', 'marginTop': '2px',
                    }),
                ]),
            ]
        ),

        # ── TABS ─────────────────────────────────────────────
        dcc.Tabs(
            value='tab-status',
            colors={"border": "#e5e5ea", "primary": "#0071e3", "background": "#f2f2f7"},
            style={'marginBottom': '14px'},
            children=[

                # TAB 1 — LIVE STATUS
                dcc.Tab(label='Live Status', value='tab-status',
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE,
                        children=[html.Div(style={'marginTop': '14px'}, children=[
                            html.Div(style={'display': 'flex', 'gap': '14px'}, children=[
                                card([
                                    section_label("Environment"),
                                    html.Div("Channel Model", style={
                                        'fontSize': '12px', 'color': '#6e6e73',
                                        'fontWeight': '500', 'marginBottom': '8px',
                                    }),
                                    dcc.RadioItems(
                                        id='ch-selector',
                                        options=[{'label': '  AWGN',     'value': 'CH:AWG'},
                                                 {'label': '  Rayleigh', 'value': 'CH:RAY'}],
                                        value='CH:AWG',
                                        labelStyle={'display': 'block', 'margin': '5px 0',
                                                    'fontSize': '13px'},
                                    ),
                                    html.Div(style={'height': '1px', 'background': '#f2f2f7',
                                                    'margin': '16px -20px'}),
                                    html.Div("Simulated SNR (dB)", style={
                                        'fontSize': '12px', 'color': '#6e6e73',
                                        'fontWeight': '500', 'marginBottom': '12px',
                                    }),
                                    dcc.Slider(
                                        id='snr-slider', min=1, max=25, step=1, value=15,
                                        marks={v: {'label': str(v),
                                                   'style': {'color': '#aeaeb2', 'fontSize': '10px'}}
                                               for v in [1, 5, 10, 15, 20, 25]},
                                        tooltip={"placement": "bottom", "always_visible": True}
                                    ),
                                    html.Div(style={'height': '1px', 'background': '#f2f2f7',
                                                    'margin': '16px -20px'}),
                                    html.Div("FEC Scheme", style={
                                        'fontSize': '12px', 'color': '#6e6e73',
                                        'fontWeight': '500', 'marginBottom': '8px',
                                    }),
                                    dcc.RadioItems(
                                        id='fec-selector',
                                        options=[{'label': '  Hamming (7,4)',       'value': 'FEC:HAM(7,4)'},
                                                 {'label': '  Convolutional (½R)',  'value': 'FEC:CONV'},
                                                 {'label': '  Repetition (3x)',     'value': 'FEC:REP3X'},
                                                 {'label': '  None',                'value': 'FEC:NONE'}],
                                        value='FEC:HAM(7,4)',
                                        labelStyle={'display': 'block', 'margin': '5px 0',
                                                    'fontSize': '13px'},
                                    ),
                                    html.Div(style={'height': '1px', 'background': '#f2f2f7',
                                                    'margin': '16px -20px'}),
                                    html.Div([
                                        html.Div("Keypad", style={
                                            'fontSize': '12px', 'color': '#6e6e73',
                                            'fontWeight': '500', 'marginBottom': '6px',
                                        }),
                                        html.Div("Type  →  # to send  →  * to clear",
                                                 style={'fontSize': '11px', 'color': '#aeaeb2',
                                                        'lineHeight': '1.5'}),
                                    ]),
                                ], extra={'width': '220px', 'flexShrink': 0}),

                                html.Div(
                                    style={'flex': 1, 'display': 'flex',
                                           'flexDirection': 'column', 'gap': '12px'},
                                    children=[
                                        card([
                                            section_label("Message Flow"),
                                            html.Div(
                                                style={'display': 'flex', 'alignItems': 'stretch',
                                                       'marginBottom': '16px'},
                                                children=[
                                                    flow_box("Transmitted",       'disp-tx-data',      '#34c759'),
                                                    flow_arrow("RF"),
                                                    flow_box("Received — raw",    'disp-rx-data',      '#ff9500'),
                                                    flow_arrow("FEC"),
                                                    flow_box("Decoded — after FEC",'disp-rx-corrected','#0071e3'),
                                                ]
                                            ),
                                            html.Div(
                                                style={'display': 'flex', 'alignItems': 'center',
                                                       'gap': '10px'},
                                                children=[
                                                    html.Div("Typing buffer", style={
                                                        'color': '#aeaeb2', 'fontSize': '11px',
                                                        'fontWeight': '500', 'flexShrink': 0,
                                                    }),
                                                    html.Div(id='disp-tx-typing', style={
                                                        'color': '#ff9500', 'fontSize': '14px',
                                                        'fontWeight': '600', 'fontFamily': APPLE_MONO,
                                                        'background': '#fff9f0', 'padding': '4px 12px',
                                                        'borderRadius': '6px',
                                                        'border': '1px solid #ffe0b2',
                                                        'flex': 1, 'minHeight': '28px',
                                                    })
                                                ]
                                            ),
                                        ]),
                                        html.Div(
                                            style={'display': 'flex', 'gap': '8px'},
                                            children=[
                                                stat_chip("TX Bits",   'disp-tx-bits',   '#1d1d1f'),
                                                stat_chip("RX Bits",   'disp-raw',       '#1d1d1f'),
                                                stat_chip("Err Bits",  'disp-errors',    '#ff3b30'),
                                                stat_chip("Corrected", 'disp-corrected', '#34c759'),
                                                stat_chip("Packets",   'disp-tx-pkts',   '#0071e3'),
                                                stat_chip("Active FEC",'disp-fec',       '#6e6e73'),
                                            ]
                                        ),
                                    ]
                                ),
                            ]),
                        ])]
                ),

                # TAB 2 — SIGNAL ANALYSIS
                dcc.Tab(label='Signal Analysis', value='tab-graphs',
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE,
                        children=[html.Div(style={'marginTop': '14px'}, children=[
                            card([
                                section_label("Bit Error Rate — Live"),
                                dcc.Graph(id='g-ber-graph', config={'displayModeBar': False})
                            ], extra={'marginBottom': '12px'}),
                            html.Div(style={'display': 'flex', 'gap': '12px'}, children=[
                                card([
                                    section_label("SNR vs BER — All Modulations & FEC Modes"),
                                    html.Div("Theoretical curves + live measurements (colour = mod zone)",
                                             style={'fontSize': '11px', 'color': '#c7c7cc',
                                                    'marginBottom': '8px'}),
                                    dcc.Graph(id='g-snr-ber-graph', config={'displayModeBar': False})
                                ], extra={'flex': '3'}),
                                card([
                                    section_label("Error Rate %"),
                                    dcc.Graph(id='g-error-graph', config={'displayModeBar': False})
                                ], extra={'flex': '2'}),
                            ]),
                            html.Div(style={'display': 'flex', 'gap': '12px',
                                            'marginTop': '12px'}, children=[
                                card([
                                    section_label("Throughput vs SNR"),
                                    dcc.Dropdown(
                                        id='tp-curve-select',
                                        options=TP_CURVES,
                                        value=TP_ALL,
                                        multi=True,
                                        clearable=False,
                                        placeholder='Select curves…',
                                        style={'fontSize': '11px', 'marginBottom': '10px',
                                               'fontFamily': APPLE_FONT}
                                    ),
                                    dcc.Graph(id='g-throughput', config={'displayModeBar': False})
                                ], extra={'flex': '1'}),
                                card([
                                    section_label("Adaptive Modulation Selection per SNR"),
                                    dcc.Graph(id='g-adaptive-mod', config={'displayModeBar': False})
                                ], extra={'flex': '1'}),
                            ]),
                            html.Div(style={'display': 'flex', 'gap': '12px',
                                            'marginTop': '12px'}, children=[
                                card([
                                    section_label("FEC Scheme Comparison — Post-FEC BER"),
                                    dcc.Graph(id='g-fec-compare', config={'displayModeBar': False})
                                ], extra={'flex': '1'}),
                                card([
                                    section_label("AWGN vs Rayleigh — Channel Impact on BER"),
                                    dcc.Graph(id='g-channel-impact', config={'displayModeBar': False})
                                ], extra={'flex': '1'}),
                            ]),
                            card([
                                section_label(
                                    "Constellation Diagrams — No FEC  /  Hamming (7,4)  /  Convolutional"
                                ),
                                html.Div("Active modulation with simulated noise per FEC scheme",
                                         style={'fontSize': '11px', 'color': '#c7c7cc',
                                                'marginBottom': '8px'}),
                                dcc.Graph(id='g-constellation', config={'displayModeBar': False})
                            ], extra={'marginTop': '12px'}),
                        ])]
                ),

                # TAB 3 — DIAGNOSTICS
                dcc.Tab(label='Diagnostics', value='tab-diag',
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE,
                        children=[html.Div(style={'marginTop': '14px'}, children=[
                            card([
                                section_label("Binary Frames"),
                                html.Div(
                                    style={'display': 'grid',
                                           'gridTemplateColumns': '1fr 1fr 1fr',
                                           'gap': '12px'},
                                    children=[
                                        binary_card("TX Raw Binary",         "disp-tx-binary"),
                                        binary_card("RX Binary — Before FEC","disp-rx-binary-before"),
                                        binary_card("RX Binary — After FEC", "disp-rx-binary-after"),
                                    ]
                                )
                            ], extra={'marginBottom': '12px'}),
                            card([
                                section_label("System Debug"),
                                html.Div(id='debug-out', style={
                                    'color': '#aeaeb2', 'fontSize': '11px',
                                    'fontFamily': APPLE_MONO, 'lineHeight': '1.6',
                                }),
                            ]),
                        ])]
                ),

                # TAB 4 — EVENT LOG
                dcc.Tab(label='Event Log', value='tab-events',
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE,
                        children=[html.Div(style={'marginTop': '14px'}, children=[
                            card([
                                html.Div(
                                    style={'display': 'flex', 'alignItems': 'center',
                                           'justifyContent': 'space-between',
                                           'marginBottom': '14px'},
                                    children=[
                                        html.Div(style={'display': 'flex', 'alignItems': 'center',
                                                        'gap': '6px'}, children=[
                                            html.Div("Filter:", style={
                                                'fontSize': '11px', 'color': '#8e8e93',
                                                'fontWeight': '500',
                                            }),
                                            dcc.RadioItems(
                                                id='event-filter',
                                                options=[
                                                    {'label': '  All',  'value': 'ALL'},
                                                    {'label': '  Info', 'value': 'INFO'},
                                                    {'label': '  Warn', 'value': 'WARN'},
                                                    {'label': '  Crit', 'value': 'CRIT'},
                                                ],
                                                value='ALL', inline=True,
                                                labelStyle={'fontSize': '12px', 'marginRight': '6px'},
                                            ),
                                        ]),
                                        html.Div(style={'display': 'flex', 'alignItems': 'center',
                                                        'gap': '12px'}, children=[
                                            html.Div(id='event-count', style={
                                                'fontSize': '11px', 'color': '#aeaeb2',
                                            }),
                                            html.Button('Clear log',
                                                id='event-clear', n_clicks=0,
                                                style={
                                                    'fontSize': '11px', 'padding': '5px 14px',
                                                    'borderRadius': '8px',
                                                    'border': '1px solid #e5e5ea',
                                                    'background': '#ffffff', 'color': '#ff3b30',
                                                    'cursor': 'pointer', 'fontWeight': '500',
                                                }),
                                        ]),
                                    ]
                                ),
                                html.Div(
                                    style={'display': 'flex', 'padding': '4px 10px',
                                           'borderBottom': '2px solid #e5e5ea',
                                           'marginBottom': '2px'},
                                    children=[
                                        html.Span("Time",     style={'minWidth': '64px', 'marginRight': '10px', 'fontSize': '9px', 'fontWeight': '600', 'color': '#aeaeb2', 'letterSpacing': '0.6px', 'textTransform': 'uppercase'}),
                                        html.Span("Severity", style={'minWidth': '52px', 'marginRight': '10px', 'fontSize': '9px', 'fontWeight': '600', 'color': '#aeaeb2', 'letterSpacing': '0.6px', 'textTransform': 'uppercase'}),
                                        html.Span("Category", style={'minWidth': '84px', 'marginRight': '10px', 'fontSize': '9px', 'fontWeight': '600', 'color': '#aeaeb2', 'letterSpacing': '0.6px', 'textTransform': 'uppercase'}),
                                        html.Span("Event",    style={'fontSize': '9px', 'fontWeight': '600', 'color': '#aeaeb2', 'letterSpacing': '0.6px', 'textTransform': 'uppercase'}),
                                    ]
                                ),
                                html.Div(id='event-log-container', style={
                                    'height': '520px', 'overflowY': 'auto',
                                    'background': '#fafafa', 'borderRadius': '8px',
                                    'border': '1px solid #f2f2f7',
                                }),
                            ]),
                        ])]
                ),
            ]
        ),

        dcc.Interval(id='graph-update',         interval=1000),
        dcc.Interval(id='constellation-update', interval=4000),
    ]
)

# ---------------------------------------------------------------
# GRAPH HELPERS
# ---------------------------------------------------------------
def _const_centers(mod):
    if mod == 'BPSK':
        return [(-1, 0), (1, 0)]
    if mod == 'QPSK':
        s = 1 / math.sqrt(2)
        return [(-s, -s), (s, -s), (-s, s), (s, s)]
    return [(x, y) for x in [-3, -1, 1, 3] for y in [-3, -1, 1, 3]]


def constellation_cloud(mod, sigma, n=220):
    centers = _const_centers(mod)
    xs, ys = [], []
    for _ in range(n):
        cx, cy = random.choice(centers)
        xs.append(cx + random.gauss(0, sigma))
        ys.append(cy + random.gauss(0, sigma))
    return xs, ys


def snr_to_sigma(snr_db, fec_gain_db=0):
    eff = snr_db + fec_gain_db
    return max(0.04, 1.2 / (1 + 0.18 * eff))



# ---------------------------------------------------------------
# CALLBACK
# ---------------------------------------------------------------
@app.callback(
    [
        Output('hero-ber',      'children'),
        Output('hero-ber',      'style'),
        Output('hero-ber-zone', 'children'),
        Output('hero-status',   'children'),
        Output('hero-snr',      'children'),
        Output('hero-fec',      'children'),
        Output('mod-ind-BPSK',  'style'),
        Output('mod-ind-QPSK',  'style'),
        Output('mod-ind-16QAM', 'style'),
        Output('conn-rx', 'children'),
        Output('conn-rx', 'style'),
        Output('conn-tx', 'children'),
        Output('conn-tx', 'style'),
        Output('disp-tx-data',      'children'),
        Output('disp-tx-typing',    'children'),
        Output('disp-rx-data',      'children'),
        Output('disp-rx-corrected', 'children'),
        Output('disp-tx-bits',   'children'),
        Output('disp-raw',       'children'),
        Output('disp-errors',    'children'),
        Output('disp-corrected', 'children'),
        Output('disp-tx-pkts',   'children'),
        Output('disp-fec',       'children'),
        Output('g-ber-graph',       'figure'),
        Output('g-error-graph',     'figure'),
        Output('g-snr-ber-graph',   'figure'),
        Output('g-throughput',      'figure'),
        Output('g-adaptive-mod',    'figure'),
        Output('g-fec-compare',     'figure'),
        Output('g-channel-impact',  'figure'),
        Output('debug-out',             'children'),
        Output('disp-tx-binary',        'children'),
        Output('disp-rx-binary-before', 'children'),
        Output('disp-rx-binary-after',  'children'),
        Output('event-log-container', 'children'),
        Output('event-count',         'children'),
    ],
    [
        Input('graph-update',    'n_intervals'),
        Input('snr-slider',      'value'),
        Input('ch-selector',     'value'),
        Input('fec-selector',    'value'),
        Input('tp-curve-select', 'value'),
        Input('event-filter',    'value'),
        Input('event-clear',     'n_clicks'),
    ]
)
def update_ui(n, snr_val, ch_val, fec_val, tp_curves, ev_filter, clear_clicks):

    global _prev_snr, _prev_fec_clean, _prev_ch_name, _last_clear_clks

    if clear_clicks and clear_clicks > _last_clear_clks:
        event_log.clear()
        _last_clear_clks = clear_clicks

    fec_value = fec_val.replace('FEC:', '')
    ch_name   = 'Rayleigh' if ch_val == 'CH:RAY' else 'AWGN'

    # Send commands only when the value changes — constant BT traffic causes
    # SoftwareSerial half-duplex collisions that corrupt data packets on RX1
    def _send_cmd(msg):
        if ser_tx:
            try: ser_tx.write(msg.encode()); ser_tx.flush()
            except Exception as e: print(f"[CMD ERR] {e}")

    if snr_val != _prev_snr:
        log_event('INFO', 'CONFIG', f'SNR: {_prev_snr} → {snr_val} dB')
        _prev_snr = snr_val
        _send_cmd(f"SNR:{snr_val}\n")
    if fec_value != _prev_fec_clean:
        log_event('INFO', 'CONFIG', f'FEC scheme: {_prev_fec_clean} → {fec_value}')
        _prev_fec_clean = fec_value
        _send_cmd(f"FEC:{fec_value}\n")
    if ch_name != _prev_ch_name:
        log_event('INFO', 'CONFIG', f'Channel model: {_prev_ch_name} → {ch_name}')
        _prev_ch_name = ch_name
        _send_cmd(f"{ch_val}\n")

    with lock:
        stats["current_snr"]     = snr_val
        stats["current_channel"] = "AWGN" if ch_val == "CH:AWG" else "Rayleigh"
        now = time.time()

        try:
            ber_float = float(stats["ber"])
        except ValueError:
            ber_float = 0.0

        if ber_float >= 0.018:
            ber_color = '#ff3b30'; ber_zone = 'High BER — BPSK zone'
        elif ber_float >= 0.006:
            ber_color = '#ff9500'; ber_zone = 'Moderate BER — QPSK zone'
        else:
            ber_color = '#34c759'; ber_zone = 'Low BER — 16QAM zone'

        hero_ber_style = {
            'fontSize': '44px', 'fontWeight': '700',
            'letterSpacing': '-1.5px', 'lineHeight': '1',
            'fontFamily': APPLE_MONO, 'color': ber_color,
        }

        active_mod = stats["mod"].strip()
        def pill_style(m):
            base = {'padding': '6px 16px', 'borderRadius': '20px',
                    'fontWeight': '600', 'fontSize': '12px',
                    'display': 'inline-block', 'letterSpacing': '0.2px'}
            if m == active_mod:
                c = MOD_COLORS.get(m, '#0071e3')
                base.update({'background': c, 'color': '#ffffff'})
            else:
                base.update({'background': '#f2f2f7', 'color': '#aeaeb2'})
            return base

        is_online  = stats["status"] not in ("OFFLINE", "WAITING...")
        stat_color = '#34c759' if is_online else '#ff3b30'
        stat_dot   = '●' if is_online else '○'
        status_elem = html.Div([
            html.Span(f'{stat_dot}  ', style={'color': stat_color, 'fontSize': '16px'}),
            html.Span(stats["status"], style={
                'color': '#1d1d1f', 'fontWeight': '700', 'fontSize': '20px',
            }),
        ])

        # Connection pills
        rx_age = now - stats["rx_last_seen"] if stats["rx_last_seen"] > 0 else 999
        tx_age = now - stats["tx_last_seen"] if stats["tx_last_seen"] > 0 else 999
        tx_alive = ser_tx is not None and tx_age < 8

        # RX state based purely on forwarded-data freshness (no direct serial port)
        if rx_age < 2:
            rx_state = "LIVE"
        elif rx_age < 15:
            rx_state = "IDLE"
        else:
            rx_state = "OFFLINE"

        _pill_base = {'padding': '5px 12px', 'borderRadius': '20px',
                      'fontSize': '11px', 'fontWeight': '500'}
        if rx_state == "LIVE":
            rx_pill_style = {**_pill_base, 'background': '#f0faf4',
                             'color': '#1c7a3e', 'border': '1px solid #c3e9d0'}
            rx_dot = "●"
        elif rx_state == "IDLE":
            rx_pill_style = {**_pill_base, 'background': '#f0f6ff',
                             'color': '#0050a0', 'border': '1px solid #b3d1ff'}
            rx_dot = "◉"
        else:
            rx_pill_style = {**_pill_base, 'background': '#fdf2f2',
                             'color': '#c0392b', 'border': '1px solid #f5c6c6'}
            rx_dot = "○"

        def conn_pill_style(alive):
            if alive:
                return {**_pill_base, 'background': '#f0faf4',
                        'color': '#1c7a3e', 'border': '1px solid #c3e9d0'}
            return {**_pill_base, 'background': '#fdf2f2',
                    'color': '#c0392b', 'border': '1px solid #f5c6c6'}

        age_str = f"{rx_age:.1f}s" if rx_age < 999 else "—"
        rx_text = f"{rx_dot} RX via BT  {age_str}"
        tx_text = (f"● TX {TX_PORT}  {tx_age:.1f}s"
                   if tx_alive else f"○ TX {TX_PORT}  offline")

        typing_display = (stats["tx_typing"] + " ▌") if stats["tx_typing"] else "—"

        # ── BER over time ──────────────────────────────────────
        ber_fig = go.Figure()
        ber_fig.add_trace(go.Scatter(
            y=list(raw_ber_history), name="Raw BER",
            line=dict(color='rgba(255,59,48,0.45)', width=1.2), mode='lines'
        ))
        ber_fig.add_trace(go.Scatter(
            y=list(avg_ber_history), name="Smoothed BER",
            line=dict(color='#ff3b30', width=2.5), mode='lines'
        ))
        for y, color, lbl, pos in [
            (0.018, '#ff3b30', '→ BPSK',      'top left'),
            (0.012, '#ff6b30', '← Exit BPSK', 'top right'),
            (0.006, '#ff9500', '→ QPSK',      'top left'),
            (0.003, '#ffbb00', '← Exit QPSK', 'top right'),
        ]:
            ber_fig.add_hline(y=y, line_dash="dot", line_color=color,
                              annotation_text=lbl, annotation_position=pos,
                              annotation_font_size=10)
        ber_fig.update_layout(
            **PLOT_BASE, height=360,
            margin=dict(l=65, r=20, t=30, b=55),
            yaxis=dict(**AXIS_STYLE, range=[0, 0.05], title="BER"),
            xaxis=dict(**AXIS_STYLE, title="Samples"),
            legend=dict(orientation='h', y=1.12, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── Error rate % ───────────────────────────────────────
        error_fig = go.Figure()
        error_fig.add_trace(go.Scatter(
            y=list(error_rate_history), name="Error Rate %",
            line=dict(color='#ff9500', width=2),
            mode='lines+markers', marker=dict(size=3, color='#ff9500')
        ))
        error_fig.update_layout(
            **PLOT_BASE, height=390,
            margin=dict(l=65, r=20, t=30, b=55),
            yaxis=dict(**AXIS_STYLE, range=[0, 10], title="Error %"),
            xaxis=dict(**AXIS_STYLE, title="Samples"),
            legend=dict(font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── SNR vs BER ─────────────────────────────────────────
        snr_ber_fig = go.Figure()
        snr_ber_fig.add_hrect(y0=0.018, y1=0.055,
                               fillcolor='rgba(255,59,48,0.05)', line_width=0)
        snr_ber_fig.add_hrect(y0=0.006, y1=0.018,
                               fillcolor='rgba(255,149,0,0.05)', line_width=0)
        snr_ber_fig.add_hrect(y0=0, y1=0.006,
                               fillcolor='rgba(52,199,89,0.05)', line_width=0)
        for y, color, label in [
            (0.038, 'rgba(255,59,48,0.4)',  'BPSK zone'),
            (0.012, 'rgba(255,149,0,0.4)',  'QPSK zone'),
            (0.003, 'rgba(52,199,89,0.4)',  '16QAM zone'),
        ]:
            snr_ber_fig.add_annotation(x=0.5, y=y, text=label, showarrow=False,
                                       font=dict(color=color, size=10), xref='paper')

        snr_range_g = list(range(1, 26))

        def _erfc_approx(x):
            if x <= 0: return 1.0
            t = 1.0 / (1.0 + 0.3275911 * x)
            return (0.254829592*t - 0.284496736*t**2 + 1.421413741*t**3
                    - 1.453152027*t**4 + 1.061405429*t**5) * math.exp(-x * x)

        def _theo_ber(snr_db, mod, fec_gain_db=0):
            eff = snr_db + fec_gain_db
            lin = 10 ** (eff / 10)
            if mod == '16QAM':
                return max(1e-6, min(0.5, 0.75 * _erfc_approx(math.sqrt(max(0, 0.1 * lin)))))
            return max(1e-6, min(0.5, _erfc_approx(math.sqrt(max(0, lin)))))

        for mod, col in [('BPSK', '#ff3b30'), ('QPSK', '#ff9500'), ('16QAM', '#34c759')]:
            snr_ber_fig.add_trace(go.Scatter(
                x=snr_range_g, y=[_theo_ber(s, mod) for s in snr_range_g],
                mode='lines', name=f'{mod} No FEC',
                line=dict(color=col, width=1.4, dash='dot'),
            ))
        for fec_name, gain_db, col, dash in [
            ('HAM(7,4)', 3.5, '#0071e3', 'dash'),
            ('REP3X',    2.0, '#5856d6', 'dashdot'),
            ('CONV',     5.5, '#32ade6', 'longdashdot'),
        ]:
            snr_ber_fig.add_trace(go.Scatter(
                x=snr_range_g,
                y=[_theo_ber(s, 'BPSK', gain_db) for s in snr_range_g],
                mode='lines', name=f'BPSK+{fec_name}',
                line=dict(color=col, width=1.6, dash=dash),
            ))
        if snr_ber_pairs:
            pairs  = list(snr_ber_pairs)
            xs     = [p[0] for p in pairs]
            ys     = [p[1] for p in pairs]
            colors = ['#ff3b30' if b >= 0.018 else '#ff9500' if b >= 0.006 else '#34c759'
                      for b in ys]
            snr_ber_fig.add_trace(go.Scatter(
                x=xs, y=ys, mode='lines',
                line=dict(color='rgba(0,113,227,0.08)', width=1), showlegend=False,
            ))
            snr_ber_fig.add_trace(go.Scatter(
                x=xs, y=ys, mode='markers', name='Live (colour=mod zone)',
                marker=dict(color=colors, size=7, opacity=0.9,
                            line=dict(width=1, color='rgba(255,255,255,0.8)')),
            ))
        snr_ber_fig.update_layout(
            **PLOT_BASE, height=420,
            margin=dict(l=65, r=20, t=40, b=55),
            xaxis=dict(**AXIS_STYLE, title="SNR (dB)", range=[0, 26], dtick=2),
            yaxis=dict(**AXIS_STYLE, title="BER", range=[0, 0.055]),
            legend=dict(orientation='h', y=1.22, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── Throughput vs SNR ──────────────────────────────────
        tp_fig = go.Figure()
        snr_range_th = list(range(1, 26))
        sel = tp_curves or TP_ALL
        for key, mod_name, mb, col in [
            ('bpsk_nofec', 'BPSK', 1, '#ff3b30'),
            ('qpsk_nofec', 'QPSK', 2, '#ff9500'),
            ('16qam_nofec','16QAM',4, '#34c759'),
        ]:
            if key in sel:
                tp_fig.add_trace(go.Scatter(
                    x=snr_range_th,
                    y=[mb * (1 - _theo_ber(s, mod_name)) for s in snr_range_th],
                    mode='lines', name=f'{mod_name} No FEC',
                    line=dict(color=col, width=1.6, dash='dot'),
                ))
        for key, fec_name, rate, gain_db, col, dash in [
            ('qpsk_ham',   'HAM(7,4)', 4/7, 3.5, '#0071e3', 'dash'),
            ('qpsk_rep3x', 'REP3X',    1/3, 2.0, '#5856d6', 'dashdot'),
            ('qpsk_conv',  'CONV',     1/2, 5.5, '#32ade6', 'longdashdot'),
        ]:
            if key in sel:
                tp_fig.add_trace(go.Scatter(
                    x=snr_range_th,
                    y=[rate * 2 * (1 - _theo_ber(s, 'QPSK', gain_db)) for s in snr_range_th],
                    mode='lines', name=f'QPSK+{fec_name}',
                    line=dict(color=col, width=1.6, dash=dash),
                ))
        if 'live' in sel and throughput_data:
            td = list(throughput_data)
            tp_fig.add_trace(go.Scatter(
                x=[p[0] for p in td], y=[p[1] for p in td],
                mode='lines+markers', name='Live (adaptive)',
                line=dict(color='#1d1d1f', width=2.2),
                marker=dict(size=4, color='#1d1d1f'),
            ))
        tp_fig.update_layout(
            **PLOT_BASE, height=350,
            margin=dict(l=70, r=20, t=40, b=55),
            xaxis=dict(**AXIS_STYLE, title="SNR (dB)", range=[0, 26]),
            yaxis=dict(**AXIS_STYLE, title="Normalised Throughput"),
            legend=dict(orientation='h', y=1.22, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── Adaptive Modulation Selection ──────────────────────
        adapt_fig = go.Figure()
        mod_order = {'BPSK': 1, 'QPSK': 2, '16QAM': 3}
        for x_th, lbl, col in [(8, 'BPSK→QPSK', 'rgba(255,149,0,0.45)'),
                                (15, 'QPSK→16QAM', 'rgba(52,199,89,0.45)')]:
            adapt_fig.add_vline(x=x_th, line_dash='dot', line_color=col,
                                annotation_text=lbl,
                                annotation_font=dict(size=9, color=col),
                                annotation_position='top right')
        adapt_fig.add_vrect(x0=0,  x1=8,  fillcolor='rgba(255,59,48,0.04)',  line_width=0)
        adapt_fig.add_vrect(x0=8,  x1=15, fillcolor='rgba(255,149,0,0.04)',  line_width=0)
        adapt_fig.add_vrect(x0=15, x1=26, fillcolor='rgba(52,199,89,0.04)',  line_width=0)
        if snr_ber_detail:
            detail = list(snr_ber_detail)
            for mod_name, col in [('BPSK', '#ff3b30'), ('QPSK', '#ff9500'), ('16QAM', '#34c759')]:
                pts = [(d[0], mod_order[mod_name]) for d in detail if d[2] == mod_name]
                if pts:
                    adapt_fig.add_trace(go.Scatter(
                        x=[p[0] for p in pts], y=[p[1] for p in pts],
                        mode='markers', name=mod_name,
                        marker=dict(color=col, size=7, opacity=0.8),
                    ))
        adapt_fig.update_layout(
            **PLOT_BASE, height=350,
            margin=dict(l=70, r=20, t=40, b=55),
            xaxis=dict(**AXIS_STYLE, title="SNR (dB)", range=[0, 26]),
            yaxis=dict(**AXIS_STYLE, title="Scheme",
                       tickvals=[1, 2, 3], ticktext=['BPSK', 'QPSK', '16QAM']),
            legend=dict(orientation='h', y=1.12, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── FEC Scheme Comparison ──────────────────────────────
        fec_fig = go.Figure()
        fec_colors    = {'HAM(7,4)': '#0071e3', 'CONV': '#32ade6', 'REP3X': '#5856d6', 'NONE': '#ff3b30'}
        fec_gains_map = {'HAM(7,4)': 3.5, 'CONV': 5.5, 'REP3X': 2.0, 'NONE': 0}
        mod_symbols   = {'BPSK': 'circle', 'QPSK': 'square', '16QAM': 'diamond'}
        for fec_name, col in fec_colors.items():
            gain = fec_gains_map.get(fec_name, 0)
            fec_fig.add_trace(go.Scatter(
                x=snr_range_g,
                y=[_theo_ber(s, 'BPSK', gain) for s in snr_range_g],
                mode='lines', name=f'{fec_name} theory',
                line=dict(color=col, width=1.4, dash='dash'),
            ))
        if snr_ber_detail:
            detail = list(snr_ber_detail)
            for fec_name, col in fec_colors.items():
                for mod_name, sym in mod_symbols.items():
                    pts = [(d[0], d[1]) for d in detail
                           if d[3] == fec_name and d[2] == mod_name]
                    if pts:
                        fec_fig.add_trace(go.Scatter(
                            x=[p[0] for p in pts], y=[p[1] for p in pts],
                            mode='markers', name=f'{fec_name}/{mod_name}',
                            marker=dict(color=col, size=6, opacity=0.75, symbol=sym),
                        ))
        fec_fig.update_layout(
            **PLOT_BASE, height=350,
            margin=dict(l=70, r=20, t=40, b=55),
            xaxis=dict(**AXIS_STYLE, title="SNR (dB)", range=[0, 26]),
            yaxis=dict(**AXIS_STYLE, title="Post-FEC BER"),
            legend=dict(orientation='h', y=1.22, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        # ── AWGN vs Rayleigh ───────────────────────────────────
        ch_fig = go.Figure()
        ch_fig.add_trace(go.Scatter(
            x=snr_range_g,
            y=[_theo_ber(s, 'BPSK') for s in snr_range_g],
            mode='lines', name='AWGN theory (BPSK)',
            line=dict(color='rgba(52,199,89,0.55)', width=1.5, dash='dot'),
        ))
        ch_fig.add_trace(go.Scatter(
            x=snr_range_g,
            y=[max(1e-6, 0.5 * (1 - math.sqrt(10**(s/10) / (1 + 10**(s/10)))))
               for s in snr_range_g],
            mode='lines', name='Rayleigh theory (BPSK)',
            line=dict(color='rgba(255,149,0,0.55)', width=1.5, dash='dot'),
        ))
        ch_colors = {'AWGN': '#34c759', 'Rayleigh': '#ff9500'}
        if snr_ber_detail:
            detail = list(snr_ber_detail)
            for ch_name_k, col in ch_colors.items():
                pts = [(d[0], d[1]) for d in detail if d[4] == ch_name_k]
                if pts:
                    ch_fig.add_trace(go.Scatter(
                        x=[p[0] for p in pts], y=[p[1] for p in pts],
                        mode='markers', name=f'{ch_name_k} live',
                        marker=dict(color=col, size=6, opacity=0.8),
                    ))
        ch_fig.update_layout(
            **PLOT_BASE, height=350,
            margin=dict(l=70, r=20, t=40, b=55),
            xaxis=dict(**AXIS_STYLE, title="SNR (dB)", range=[0, 26]),
            yaxis=dict(**AXIS_STYLE, title="Post-FEC BER"),
            legend=dict(orientation='h', y=1.18, font=dict(size=10), bgcolor='rgba(0,0,0,0)')
        )

        debug = (f"SNR={snr_val}dB  ·  {ch_val}  ·  {fec_val}  ·  "
                 f"Mod={stats['mod']}  ·  BER={stats['ber']}  ·  "
                 f"RX {age_str}  TX {tx_age:.1f}s")

        return (
            stats["ber"], hero_ber_style, ber_zone,
            status_elem, f"{snr_val}", stats["fec_type"],
            pill_style('BPSK'), pill_style('QPSK'), pill_style('16QAM'),
            rx_text, rx_pill_style,
            tx_text, conn_pill_style(tx_alive),
            stats["tx_data"], typing_display,
            stats["rx_data"], stats["rx_data_corrected"],
            stats["tx_raw_bits"], stats["raw_bits"],
            stats["error_bits"], stats["corrected_bits"],
            stats["tx_packets"], stats["fec_type"],
            ber_fig, error_fig, snr_ber_fig, tp_fig,
            adapt_fig, fec_fig, ch_fig,
            debug,
            stats["tx_binary"] or "—",
            _diff_spans(stats["rx_binary_before"] or "—", stats["rx_binary_after"] or "—"),
            _diff_spans(stats["rx_binary_after"] or "—", stats["rx_binary_before"] or "—"),
            render_event_log(ev_filter or 'ALL'),
            f"{len(event_log)} event{'s' if len(event_log) != 1 else ''}",
        )


@app.callback(
    Output('g-constellation', 'figure'),
    [Input('constellation-update', 'n_intervals'),
     Input('snr-slider', 'value')]
)
def update_constellation(n, snr_val):
    with lock:
        active_mod = stats["mod"].strip() or '16QAM'

    const_fig = make_subplots(rows=1, cols=3,
                               subplot_titles=["No FEC", "Hamming (7,4)", "Convolutional"])
    panel_sigmas = [
        snr_to_sigma(snr_val, fec_gain_db=0),
        snr_to_sigma(snr_val, fec_gain_db=3.5),
        snr_to_sigma(snr_val, fec_gain_db=5.5),
    ]
    for col_idx, sigma in enumerate(panel_sigmas, start=1):
        xs, ys = constellation_cloud(active_mod, sigma)
        const_fig.add_trace(
            go.Scatter(x=xs, y=ys, mode='markers',
                       marker=dict(size=4, color='#0071e3', opacity=0.5),
                       showlegend=False),
            row=1, col=col_idx
        )
        cx_list, cy_list = zip(*_const_centers(active_mod))
        const_fig.add_trace(
            go.Scatter(x=list(cx_list), y=list(cy_list), mode='markers',
                       marker=dict(size=9, color='#ff3b30', symbol='x',
                                   line=dict(width=2, color='#ff3b30')),
                       showlegend=False),
            row=1, col=col_idx
        )
    const_fig.update_layout(**PLOT_BASE, height=400,
                             margin=dict(l=55, r=20, t=50, b=55))
    for i in range(1, 4):
        const_fig.update_xaxes(**AXIS_STYLE, row=1, col=i)
        const_fig.update_yaxes(**AXIS_STYLE, row=1, col=i)
    return const_fig


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("ADAPTIVE LINK — STANDALONE RX MODE")
    print(f"TX port : {TX_PORT}  (RX telemetry relayed via Bluetooth)")
    print("Dashboard : http://127.0.0.1:8050")
    print("=" * 60 + "\n")

    threading.Thread(
        target=lambda: (time.sleep(1.2), webbrowser.open('http://127.0.0.1:8050')),
        daemon=True
    ).start()

    app.run(debug=False, port=8050, host='127.0.0.1')
