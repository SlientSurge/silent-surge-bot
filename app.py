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
        "text": message
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_candles(symbol: str, interval: str = "1min", outputsize: int = 40):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON"
    }

    r = requests.get(url, params=params, timeout=20)
    data = r.json()

    if "values" not in data:
        print(f"Bad data for {symbol}: {data}")
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

def signal_for_symbol(symbol: str):
    closes = fetch_candles(symbol, "1min", 40)
    if not closes or len(closes) < 25:
        return None

    current = closes[-1]
    prev = closes[-2]

    upper, mid, lower = bollinger(closes, 20, 2)
    current_rsi = rsi(closes, 14)
    sma_fast = sma(closes, 5)
    sma_slow = sma(closes, 20)

    if None in (upper, mid, lower, current_rsi, sma_fast, sma_slow):
        return None

    # Basit v1 mantığı:
    # BUY: alt banda temas/çok yakın + RSI düşük + son mum toparlanıyor + kısa ortalama yavaş ortalamanın üstüne dönmeye çalışıyor
    # SELL: üst banda temas/çok yakın + RSI yüksek + son mum zayıflıyor + kısa ortalama yavaş ortalamanın altına dönmeye çalışıyor

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
        return {
            "symbol": symbol,
            "direction": "BUY",
            "price": round(current, 5),
            "rsi": round(current_rsi, 2),
            "upper": round(upper, 5),
            "mid": round(mid, 5),
            "lower": round(lower, 5),
        }

    if sell_condition:
        return {
            "symbol": symbol,
            "direction": "SELL",
            "price": round(current, 5),
            "rsi": round(current_rsi, 2),
            "upper": round(upper, 5),
            "mid": round(mid, 5),
            "lower": round(lower, 5),
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

def scan_loop():
    time.sleep(10)  # deploy sonrası küçük bekleme

    while True:
        try:
            minute = datetime.now(timezone.utc).minute
            group = PAIRS_A if minute % 2 == 0 else PAIRS_B

            print(f"Scanning group: {group}")

            for symbol in group:
                signal = signal_for_symbol(symbol)
                if signal and should_send(signal):
                    message = (
                        f"Silent Surge V1 Signal\n"
                        f"Pair: {signal['symbol']}\n"
                        f"Direction: {signal['direction']}\n"
                        f"Price: {signal['price']}\n"
                        f"RSI: {signal['rsi']}\n"
                        f"BB Upper: {signal['upper']}\n"
                        f"BB Mid: {signal['mid']}\n"
                        f"BB Lower: {signal['lower']}\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    print(f"Sending signal: {message}")
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
