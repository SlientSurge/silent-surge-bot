from flask import Flask, jsonify
import requests
import os
import time
import threading
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================
# ENV
# =========================
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

# =========================
# TIMEZONE
# =========================
NY_TZ = ZoneInfo("America/New_York")

# =========================
# BOT SETTINGS
# =========================
PAIR_GROUPS = [
    ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"],
    ["USD/CAD", "NZD/USD", "EUR/JPY", "GBP/JPY"],
    ["AUD/JPY", "CAD/JPY", "EUR/GBP", "GBP/CHF"],
]

ALL_PAIRS = [p for g in PAIR_GROUPS for p in g]

SCAN_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 1800
REQUEST_TIMEOUT = 20

PRIMARY_INTERVAL = "5min"
PRIMARY_OUTPUTSIZE = 100
MICRO_INTERVAL = "1min"
MICRO_OUTPUTSIZE = 50

PAIR_CACHE = {}
CACHE_LOCK = threading.Lock()

LAST_SIGNAL = {}
LATEST_STATUS = {
    "last_scan_ny": None,
    "last_scan_utc": None,
    "last_session": None,
    "last_group": None,
    "last_error": None,
    "last_signal": None,
    "signals_sent_today": 0,
    "bot_started_ny": None,
    "scanner_started": False,
    "scanner_heartbeat": None,
}

# =========================
# INDICATOR SETTINGS
# =========================
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 7

MIN_CONFIDENCE = 72

SESSION_MIN_CONFIDENCE = {
    "TOKYO": 78,
    "TOKYO + LONDON": 74,
    "NEW YORK": 72,
    "LOW LIQUIDITY": 84,
}

ATR_MIN_BY_SYMBOL = {
    "EUR/USD": 0.00045,
    "GBP/USD": 0.00065,
    "USD/JPY": 0.045,
    "AUD/USD": 0.00045,
    "USD/CAD": 0.00055,
    "NZD/USD": 0.00045,
    "EUR/JPY": 0.055,
    "GBP/JPY": 0.080,
    "AUD/JPY": 0.055,
    "CAD/JPY": 0.045,
    "EUR/GBP": 0.00030,
    "GBP/CHF": 0.00070,
}

# =========================
# HELPERS
# =========================
def now_ny():
    return datetime.now(NY_TZ)

def now_utc():
    return datetime.now(timezone.utc)

def fmt_ny(dt=None):
    if dt is None:
        dt = now_ny()
    return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")

def safe_float(x, default=None):
    try:
        return float(x)
    except:
        return default

def clamp(n, smallest, largest):
    return max(smallest, min(n, largest))

def mean(values):
    return sum(values) / len(values) if values else 0.0

def stddev(values):
    if not values:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=REQUEST_TIMEOUT)
        return r.status_code == 200
    except:
        return False

def get_session_name(hour):
    if 19 <= hour <= 23:
        return "TOKYO"
    elif 0 <= hour <= 11:
        return "TOKYO + LONDON"
    elif 12 <= hour <= 16:
        return "NEW YORK"
    else:
        return "LOW LIQUIDITY"

def cooldown_remaining(symbol, direction):
    d = LAST_SIGNAL.get(symbol)
    if not d or d["direction"] != direction:
        return 0
    return max(0, int(COOLDOWN_SECONDS - (time.time() - d["time"])))

# =========================
# DATA
# =========================
def fetch_candles(symbol, interval="5min", outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    data = r.json()

    values = list(reversed(data["values"]))
    return [{
        "open": float(v["open"]),
        "high": float(v["high"]),
        "low": float(v["low"]),
        "close": float(v["close"])
    } for v in values]

# =========================
# INDICATORS
# =========================
def ema(vals, p):
    k = 2/(p+1)
    out = [None]*(p-1)
    sma = sum(vals[:p])/p
    out.append(sma)
    prev = sma
    for v in vals[p:]:
        prev = v*k + prev*(1-k)
        out.append(prev)
    return out

def rsi(vals, p=14):
    gains, losses = [], []
    for i in range(1, p+1):
        d = vals[i]-vals[i-1]
        gains.append(max(d,0))
        losses.append(max(-d,0))

    ag, al = sum(gains)/p, sum(losses)/p
    rs = ag/al if al else 0
    out = [None]*p + [100-(100/(1+rs))]

    for i in range(p+1, len(vals)):
        d = vals[i]-vals[i-1]
        ag = (ag*(p-1)+max(d,0))/p
        al = (al*(p-1)+max(-d,0))/p
        rs = ag/al if al else 0
        out.append(100-(100/(1+rs)))
    return out

def bollinger(vals, p=20, s=2):
    mid, up, lo = [], [], []
    for i in range(len(vals)):
        if i < p-1:
            mid.append(None); up.append(None); lo.append(None)
            continue
        w = vals[i-p+1:i+1]
        m = mean(w); sd = stddev(w)
        mid.append(m)
        up.append(m+s*sd)
        lo.append(m-s*sd)
    return mid, up, lo

def atr(h, l, c, p=7):
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1,len(c))]
    out = [None]*p + [sum(tr[:p])/p]
    for t in tr[p:]:
        out.append((out[-1]*(p-1)+t)/p)
    return [None]+out

# =========================
# SIGNAL
# =========================
def analyze(symbol):
    data = fetch_candles(symbol)
    c = [x["close"] for x in data]
    h = [x["high"] for x in data]
    l = [x["low"] for x in data]

    ema9 = ema(c,9)
    ema21 = ema(c,21)
    r = rsi(c)
    _, u, lo = bollinger(c)
    a = atr(h,l,c)

    i = len(c)-1

    if None in (ema9[i], ema21[i], r[i], u[i], lo[i], a[i]):
        return None

    up = ema9[i] > ema21[i] and c[i] > ema9[i]
    down = ema9[i] < ema21[i] and c[i] < ema9[i]

    conf = 0
    direction = None

    if up:
        direction = "UP"
        conf += 30
        if r[i] > 50: conf += 10
        if c[i] > c[i-1]: conf += 10

    elif down:
        direction = "DOWN"
        conf += 30
        if r[i] < 50: conf += 10
        if c[i] < c[i-1]: conf += 10

    if a[i] < ATR_MIN_BY_SYMBOL.get(symbol,0.0005):
        conf -= 20

    return {"symbol":symbol,"signal":direction,"confidence":conf}

# =========================
# LOOP
# =========================
def scan():
    best = None
    session = get_session_name(now_ny().hour)

    for s in ALL_PAIRS:
        try:
            r = analyze(s)
            if not r or not r["signal"]:
                continue

            if r["confidence"] < SESSION_MIN_CONFIDENCE.get(session,72):
                continue

            if cooldown_remaining(s, r["signal"]) > 0:
                continue

            if not best or r["confidence"] > best["confidence"]:
                best = r

        except Exception as e:
            print("ERR:", e)

    if best:
        msg = f"{best['symbol']} {best['signal']} %{best['confidence']}"
        if send_telegram_message(msg):
            LAST_SIGNAL[best["symbol"]] = {"direction":best["signal"],"time":time.time()}

def loop():
    while True:
        scan()
        time.sleep(SCAN_INTERVAL_SECONDS)

# =========================
# API
# =========================
@app.route("/")
def home():
    return jsonify({"status":"running","time":fmt_ny()})

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
