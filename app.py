from flask import Flask, jsonify
import requests
import os
import time
import threading
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PAIR_GROUPS = [
    ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"],
    ["USD/CAD", "NZD/USD", "EUR/JPY", "GBP/JPY"],
    ["AUD/JPY", "CAD/JPY", "EUR/GBP", "GBP/CHF"],
]

LAST_SIGNAL = {}
SCAN_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 1800
NY_TZ = ZoneInfo("America/New_York")

SIGNAL_LOG = []
MAX_LOG_ITEMS = 1000
PAIR_STATS = {}
MAX_TELEGRAM_TEXT = 3900

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "SilentSurgeBot/Final"})


# --------------------------
# TIME
# --------------------------
def utc_now():
    return datetime.now(timezone.utc)


def ny_now():
    return datetime.now(NY_TZ)


def iso_now():
    return utc_now().isoformat()


def parse_expiry_minutes(expiry_text):
    txt = (expiry_text or "").lower()
    if "1" in txt:
        return 1
    if "2" in txt:
        return 2
    if "5" in txt:
        return 5
    return 3


def parse_dt_safe(dt_str):
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except:
        return None


# --------------------------
# TELEGRAM
# --------------------------
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram ENV missing")
        return False

    if len(msg) > MAX_TELEGRAM_TEXT:
        msg = msg[:MAX_TELEGRAM_TEXT]

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        r = HTTP.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=15)

        return r.status_code == 200
    except Exception as e:
        print("Telegram error:", e)
        return False


# --------------------------
# DATA
# --------------------------
def fetch_ohlc(symbol, size=120):
    try:
        r = HTTP.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol,
            "interval": "1min",
            "outputsize": size,
            "apikey": TWELVEDATA_API_KEY
        }, timeout=20)

        data = r.json()
        if "values" not in data:
            return None

        values = list(reversed(data["values"]))

        candles = []
        for v in values:
            try:
                candles.append({
                    "datetime": v["datetime"],
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                })
            except:
                continue

        return candles
    except:
        return None


def build_5m(c1m):
    buckets = {}
    for c in c1m:
        dt = parse_dt_safe(c["datetime"])
        if not dt:
            continue

        m = dt.minute - dt.minute % 5
        key_dt = dt.replace(minute=m, second=0, microsecond=0)
        key = key_dt.isoformat()

        if key not in buckets:
            buckets[key] = {
                "datetime": key_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
        else:
            buckets[key]["high"] = max(buckets[key]["high"], c["high"])
            buckets[key]["low"] = min(buckets[key]["low"], c["low"])
            buckets[key]["close"] = c["close"]

    result = list(buckets.values())
    result.sort(key=lambda x: x["datetime"])
    return result


def closes(c):
    return [x["close"] for x in c]


# --------------------------
# INDICATORS
# --------------------------
def sma(v, p):
    if len(v) < p:
        return None
    return sum(v[-p:]) / p


def std(v, p):
    if len(v) < p:
        return None
    m = sum(v[-p:]) / p
    return math.sqrt(sum((x - m) ** 2 for x in v[-p:]) / p)


def rsi(v, p=14):
    if len(v) < p + 1:
        return None

    gains, losses = [], []
    for i in range(-p, 0):
        d = v[i] - v[i - 1]
        if d >= 0:
            gains.append(d)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(d))

    ag = sum(gains) / p
    al = sum(losses) / p

    if al == 0:
        return 100

    rs = ag / al
    return 100 - (100 / (1 + rs))


def bb(v, p=20):
    mid = sma(v, p)
    sd = std(v, p)
    if mid is None or sd is None:
        return None, None, None
    return mid + 2*sd, mid, mid - 2*sd


def atr(c, p=14):
    if len(c) < p + 1:
        return None

    trs = []
    for i in range(1, len(c)):
        h, l, pc = c[i]["high"], c[i]["low"], c[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))

    return sum(trs[-p:]) / p


# --------------------------
# SIGNAL CORE
# --------------------------
def signal_for(symbol):
    c1 = fetch_ohlc(symbol)
    if not c1 or len(c1) < 50:
        return None

    c5 = build_5m(c1)
    if not c5 or len(c5) < 20:
        return None

    cl1 = closes(c1)
    cl5 = closes(c5)

    price = cl1[-1]
    prev = cl1[-2]

    up, mid, low = bb(cl1)
    r = rsi(cl1)
    f = sma(cl1, 5)
    s = sma(cl1, 20)
    a = atr(c1)

    if None in (up, mid, low, r, f, s, a):
        return None

    trend5 = "UP" if sma(cl5,5) > sma(cl5,20) else "DOWN"

    # EXHAUSTION
    if price <= low*1.001 and r < 25 and price > prev:
        return build(symbol, "BUY", "REVERSAL", price, r, up, mid, low, a, trend5)

    if price >= up*0.999 and r > 75 and price < prev:
        return build(symbol, "SELL", "REVERSAL", price, r, up, mid, low, a, trend5)

    # BREAKOUT
    if trend5=="UP" and price>mid and prev<=mid:
        return build(symbol, "BUY", "TREND", price, r, up, mid, low, a, trend5)

    if trend5=="DOWN" and price<mid and prev>=mid:
        return build(symbol, "SELL", "TREND", price, r, up, mid, low, a, trend5)

    return None


def build(sym, direction, state, price, rsi_v, up, mid, low, atr_v, trend):
    return {
        "symbol": sym,
        "direction": direction,
        "state": state,
        "price": round(price,5),
        "rsi": round(rsi_v,2),
        "atr": round(atr_v,5),
        "upper": round(up,5),
        "mid": round(mid,5),
        "lower": round(low,5),
        "trend": trend,
        "expiry": "1 min" if state=="REVERSAL" else "2 min",
        "confidence": 85 if state=="TREND" else 78,
        "time": ny_now().strftime("%H:%M:%S")
    }


# --------------------------
# ENGINE
# --------------------------
def should_send(sig):
    key = sig["symbol"] + sig["direction"]
    now = time.time()
    last = LAST_SIGNAL.get(key,0)
    return now - last > COOLDOWN_SECONDS


def mark(sig):
    LAST_SIGNAL[sig["symbol"] + sig["direction"]] = time.time()


def msg(sig):
    return (
        f"⚡ <b>SIGNAL</b>\n\n"
        f"{sig['symbol']}\n"
        f"{sig['direction']} | {sig['expiry']}\n\n"
        f"Confidence: {sig['confidence']}%\n"
        f"Trend: {sig['trend']}\n"
        f"RSI: {sig['rsi']}\n"
        f"ATR: {sig['atr']}\n\n"
        f"Time: {sig['time']}"
    )


# --------------------------
# LOOP
# --------------------------
def scan():
    while True:
        try:
            group = PAIR_GROUPS[utc_now().minute % len(PAIR_GROUPS)]

            for sym in group:
                sig = signal_for(sym)
                if not sig:
                    continue

                if not should_send(sig):
                    continue

                if send_telegram(msg(sig)):
                    mark(sig)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print("SCAN ERROR:", e)
            time.sleep(10)


# --------------------------
# ROUTES
# --------------------------
@app.route("/")
def home():
    return "RUNNING"


@app.route("/health")
def health():
    return jsonify({"status":"ok"})


# --------------------------
# START
# --------------------------
threading.Thread(target=scan, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
