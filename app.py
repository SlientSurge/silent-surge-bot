from flask import Flask
import requests
import os
import time
import threading
import math
from datetime import datetime, timezone

app = Flask(__name__)

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PAIRS_A = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "NZD/USD"]
PAIRS_B = ["EUR/JPY", "GBP/JPY", "AUD/JPY", "CAD/JPY", "EUR/GBP", "GBP/CHF"]

LAST_SIGNAL = {}
SCAN_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 1800  # aynı parite/yön için 30 dk bekle


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_candles(symbol: str, interval: str = "1min", outputsize: int = 60):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON"
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"Fetch error for {symbol} {interval}: {e}")
        return None

    if "values" not in data:
        print(f"Bad data for {symbol} {interval}: {data}")
        return None

    values = list(reversed(data["values"]))  # oldest -> newest
    closes = [float(v["close"]) for v in values]
    return closes


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def stddev(values, period):
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    return math.sqrt(variance)


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def bollinger(values, period=20, num_std=2):
    mid = sma(values, period)
    sd = stddev(values, period)
    if mid is None or sd is None:
        return None, None, None
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    return upper, mid, lower


def get_expiry_and_confidence(direction, current, prev, upper, lower, rsi_value, sma_fast, sma_slow):
    confidence = 50

    if direction == "BUY":
        band_distance = abs(current - lower)

        if rsi_value < 30:
            confidence += 12
        elif rsi_value < 35:
            confidence += 7

        if current > prev:
            confidence += 8

        if sma_fast >= sma_slow:
            confidence += 10
        elif sma_fast >= sma_slow * 0.998:
            confidence += 5

        if band_distance / max(current, 1e-9) < 0.0008:
            confidence += 8

    elif direction == "SELL":
        band_distance = abs(current - upper)

        if rsi_value > 70:
            confidence += 12
        elif rsi_value > 65:
            confidence += 7

        if current < prev:
            confidence += 8

        if sma_fast <= sma_slow:
            confidence += 10
        elif sma_fast <= sma_slow * 1.002:
            confidence += 5

        if band_distance / max(current, 1e-9) < 0.0008:
            confidence += 8

    confidence = max(50, min(confidence, 95))

    if confidence >= 82:
        expiry = "1 min"
        quality = "Strong"
    elif confidence >= 68:
        expiry = "2 min"
        quality = "Good"
    else:
        expiry = "3 min"
        quality = "Moderate"

    return expiry, confidence, quality


def signal_for_symbol(symbol: str):
    closes_1m = fetch_candles(symbol, "1min", 60)
    closes_5m = fetch_candles(symbol, "5min", 30)

    if not closes_1m or len(closes_1m) < 25:
        return None

    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    current_rsi = rsi(closes_1m, 14)
    sma_fast = sma(closes_1m, 5)
    sma_slow = sma(closes_1m, 20)

    if None in (upper, mid, lower, current_rsi, sma_fast, sma_slow):
        return None

    trend_filter = None
    if closes_5m and len(closes_5m) >= 20:
        sma5_fast = sma(closes_5m, 5)
        sma5_slow = sma(closes_5m, 20)
        if sma5_fast and sma5_slow:
            if sma5_fast > sma5_slow:
                trend_filter = "UP"
            elif sma5_fast < sma5_slow:
                trend_filter = "DOWN"

    buy_condition = (
        current <= lower * 1.002 and
        current_rsi < 32 and
        current > prev and
        sma_fast >= sma_slow * 0.998
    )

    sell_condition = (
        current >= upper * 0.998 and
        current_rsi > 68 and
        current < prev and
        sma_fast <= sma_slow * 1.002
    )

    if buy_condition:
        expiry, confidence, quality = get_expiry_and_confidence(
            "BUY", current, prev, upper, lower, current_rsi, sma_fast, sma_slow
        )
        return {
            "symbol": symbol,
            "direction": "BUY",
            "price": round(current, 5),
            "rsi": round(current_rsi, 2),
            "upper": round(upper, 5),
            "mid": round(mid, 5),
            "lower": round(lower, 5),
            "expiry": expiry,
            "confidence": confidence,
            "quality": quality,
            "trend_5m": trend_filter or "N/A"
        }

    if sell_condition:
        expiry, confidence, quality = get_expiry_and_confidence(
            "SELL", current, prev, upper, lower, current_rsi, sma_fast, sma_slow
        )
        return {
            "symbol": symbol,
            "direction": "SELL",
            "price": round(current, 5),
            "rsi": round(current_rsi, 2),
            "upper": round(upper, 5),
            "mid": round(mid, 5),
            "lower": round(lower, 5),
            "expiry": expiry,
            "confidence": confidence,
            "quality": quality,
            "trend_5m": trend_filter or "N/A"
        }

    return None


def should_send(signal):
    key = f"{signal['symbol']}:{signal['direction']}"
    now = time.time()
    last_time = LAST_SIGNAL.get(key, 0)

    if now - last_time < COOLDOWN_SECONDS:
        return False

    LAST_SIGNAL[key] = now
    return True


def build_message(signal):
    return (
        f"⚡ <b>SILENT SURGE PRO</b>\n\n"
        f"💱 <b>PAIR:</b> {signal['symbol']}\n\n"
        f"🎯 <b>DIRECTION:</b> <b>{signal['direction']}</b>\n"
        f"⏱ <b>EXPIRY:</b> <b>{signal['expiry']}</b>\n"
        f"📊 <b>CONFIDENCE:</b> <b>{signal['confidence']}%</b>\n"
        f"🔥 <b>QUALITY:</b> {signal['quality']}\n"
        f"🧭 <b>5M TREND:</b> {signal['trend_5m']}\n\n"
        f"💰 <b>PRICE:</b> {signal['price']}\n"
        f"📈 <b>RSI:</b> {signal['rsi']}\n"
        f"📉 <b>BB UPPER:</b> {signal['upper']}\n"
        f"➖ <b>BB MID:</b> {signal['mid']}\n"
        f"📉 <b>BB LOWER:</b> {signal['lower']}\n\n"
        f"🕒 <b>TIME:</b> {datetime.now().strftime('%H:%M:%S')}"
    )


def scan_loop():
    time.sleep(10)

    while True:
        try:
            minute = datetime.now(timezone.utc).minute
            group = PAIRS_A if minute % 2 == 0 else PAIRS_B

            print(f"Scanning group: {group}")

            for symbol in group:
                signal = signal_for_symbol(symbol)
                if signal and should_send(signal):
                    message = build_message(signal)
                    print(f"Sending signal for {symbol}: {signal['direction']} | {signal['expiry']} | {signal['confidence']}%")
                    send_telegram(message)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print(f"Scanner loop error: {e}")
            time.sleep(30)


@app.route("/", methods=["GET"])
def home():
    return "Silent Surge Forex Scanner Running"


def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()


start_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
