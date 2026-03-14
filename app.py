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

PAIRS_A = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "NZD/USD"]
PAIRS_B = ["EUR/JPY", "GBP/JPY", "AUD/JPY", "CAD/JPY", "EUR/GBP", "GBP/CHF"]

LAST_SIGNAL = {}
SCAN_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 1800
NY_TZ = ZoneInfo("America/New_York")

SIGNAL_LOG = []
MAX_LOG_ITEMS = 1000

PAIR_STATS = {}
MAX_TELEGRAM_TEXT = 3900


# --------------------------
# Time helpers
# --------------------------
def utc_now():
    return datetime.now(timezone.utc)


def ny_now():
    return datetime.now(NY_TZ)


def iso_now():
    return utc_now().isoformat()


def parse_expiry_minutes(expiry_text: str) -> int:
    if expiry_text.startswith("1"):
        return 1
    if expiry_text.startswith("2"):
        return 2
    return 3


# --------------------------
# Telegram
# --------------------------
def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing.")
        return

    if len(message) > MAX_TELEGRAM_TEXT:
        message = message[:MAX_TELEGRAM_TEXT]

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


# --------------------------
# Data fetch
# --------------------------
def fetch_ohlc(symbol: str, interval: str = "1min", outputsize: int = 100):
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

    values = list(reversed(data["values"]))
    candles = []
    for v in values:
        candles.append({
            "datetime": v.get("datetime"),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
            "volume": float(v["volume"]) if "volume" in v and v["volume"] not in (None, "") else None
        })
    return candles


def fetch_latest_price(symbol: str):
    candles = fetch_ohlc(symbol, "1min", 2)
    if not candles:
        return None
    return candles[-1]["close"]


def closes_from_ohlc(candles):
    return [c["close"] for c in candles]


# --------------------------
# Indicators
# --------------------------
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


def atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def candle_range(candle):
    return candle["high"] - candle["low"]


def get_5m_trend(closes_5m):
    if not closes_5m or len(closes_5m) < 20:
        return "N/A", None, None

    sma5_fast = sma(closes_5m, 5)
    sma5_slow = sma(closes_5m, 20)

    if sma5_fast is None or sma5_slow is None:
        return "N/A", None, None

    if sma5_fast > sma5_slow:
        return "UP", sma5_fast, sma5_slow
    elif sma5_fast < sma5_slow:
        return "DOWN", sma5_fast, sma5_slow
    return "FLAT", sma5_fast, sma5_slow


# --------------------------
# Filters
# --------------------------
def session_filter():
    now_ny = ny_now()
    weekday = now_ny.weekday()
    hour = now_ny.hour

    if weekday == 5:
        return False, "Saturday"

    if weekday == 6 and hour < 17:
        return False, "Sunday pre-open"

    if weekday == 4 and hour >= 17:
        return False, "Friday post-close"

    if 3 <= hour < 16:
        return True, "Active session"

    return False, "Low-liquidity session"


def atr_filter(atr_1m, price):
    if atr_1m is None or price <= 0:
        return False

    rel_atr = atr_1m / price
    if rel_atr < 0.00035:
        return False

    return True


def trend_exhaustion_filter(direction, closes_1m, rsi_1m):
    if len(closes_1m) < 4:
        return False

    c2, c3, c4 = closes_1m[-3], closes_1m[-2], closes_1m[-1]

    if direction == "BUY":
        if c4 < c3 < c2 and rsi_1m < 28:
            return True

    if direction == "SELL":
        if c4 > c3 > c2 and rsi_1m > 72:
            return True

    return False


def expansion_filter(candles_1m, atr_1m):
    """
    Volume yerine daha güvenilir forex expansion filtresi:
    son mumun range'i ATR'nin belirli yüzdesinden büyük mü?
    """
    if not candles_1m or len(candles_1m) < 5 or atr_1m is None:
        return False

    last_range = candle_range(candles_1m[-1])
    prev_ranges = [candle_range(c) for c in candles_1m[-5:-1]]
    avg_prev_range = sum(prev_ranges) / len(prev_ranges) if prev_ranges else 0

    # son mum biraz canlı olacak
    if last_range < atr_1m * 0.65:
        return False

    if avg_prev_range > 0 and last_range < avg_prev_range * 0.9:
        return False

    return True


# --------------------------
# Performance tracking
# --------------------------
def ensure_pair_stats(symbol):
    if symbol not in PAIR_STATS:
        PAIR_STATS[symbol] = {
            "signals": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "win_rate": 0.0
        }


def update_pair_stats(symbol, result):
    ensure_pair_stats(symbol)
    stats = PAIR_STATS[symbol]
    stats["signals"] += 1

    if result == "WIN":
        stats["wins"] += 1
    elif result == "LOSS":
        stats["losses"] += 1
    elif result == "DRAW":
        stats["draws"] += 1

    total_decisive = stats["wins"] + stats["losses"]
    if total_decisive > 0:
        stats["win_rate"] = round((stats["wins"] / total_decisive) * 100, 2)
    else:
        stats["win_rate"] = 0.0


# --------------------------
# Scoring / ranking
# --------------------------
def get_expiry_confidence_rank(direction, current, prev, upper, lower,
                               rsi_1m, rsi_3m, sma_fast, sma_slow,
                               trend_5m, atr_1m, setup_type):
    confidence = 50

    if setup_type == "EXHAUSTION_REVERSAL":
        confidence += 6
    elif setup_type == "BREAKOUT_RETEST":
        confidence += 10
    elif setup_type == "MOMENTUM_PULLBACK":
        confidence += 8

    if direction == "BUY":
        band_distance = abs(current - lower)

        if rsi_1m < 28:
            confidence += 14
        elif rsi_1m < 32:
            confidence += 10
        elif rsi_1m < 35:
            confidence += 6

        if rsi_3m is not None:
            if rsi_3m < 32:
                confidence += 12
            elif rsi_3m < 40:
                confidence += 7

        if current > prev:
            confidence += 8

        if sma_fast >= sma_slow:
            confidence += 10
        elif sma_fast >= sma_slow * 0.998:
            confidence += 5

        if trend_5m in ["UP", "FLAT"]:
            confidence += 12
        elif trend_5m == "DOWN":
            confidence -= 10

        if band_distance / max(current, 1e-9) < 0.0008:
            confidence += 8

    elif direction == "SELL":
        band_distance = abs(current - upper)

        if rsi_1m > 72:
            confidence += 14
        elif rsi_1m > 68:
            confidence += 10
        elif rsi_1m > 65:
            confidence += 6

        if rsi_3m is not None:
            if rsi_3m > 68:
                confidence += 12
            elif rsi_3m > 60:
                confidence += 7

        if current < prev:
            confidence += 8

        if sma_fast <= sma_slow:
            confidence += 10
        elif sma_fast <= sma_slow * 1.002:
            confidence += 5

        if trend_5m in ["DOWN", "FLAT"]:
            confidence += 12
        elif trend_5m == "UP":
            confidence -= 10

        if band_distance / max(current, 1e-9) < 0.0008:
            confidence += 8

    if atr_1m is not None:
        rel_atr = atr_1m / max(current, 1e-9)
        if rel_atr > 0.0007:
            confidence += 6
        elif rel_atr > 0.0005:
            confidence += 3

    confidence = max(40, min(confidence, 95))

    if confidence >= 86:
        expiry = "1 min"
        quality = "Strong"
        rank = "A+"
    elif confidence >= 74:
        expiry = "2 min"
        quality = "Good"
        rank = "A"
    else:
        expiry = "3 min"
        quality = "Moderate"
        rank = "B"

    return expiry, confidence, quality, rank


# --------------------------
# Setups
# --------------------------
def check_exhaustion_reversal(symbol, closes_1m, closes_3m, closes_5m, atr_1m):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    sma_fast = sma(closes_1m, 5)
    sma_slow = sma(closes_1m, 20)

    if None in (upper, mid, lower, rsi_1m, sma_fast, sma_slow):
        return None

    rsi_3m = rsi(closes_3m, 14) if closes_3m and len(closes_3m) >= 20 else None
    trend_5m, _, _ = get_5m_trend(closes_5m)

    buy_trigger = (
        current <= lower * 1.002 and
        rsi_1m < 32 and
        current > prev and
        sma_fast >= sma_slow * 0.998
    )

    sell_trigger = (
        current >= upper * 0.998 and
        rsi_1m > 68 and
        current < prev and
        sma_fast <= sma_slow * 1.002
    )

    if buy_trigger and (rsi_3m is None or rsi_3m < 42) and trend_5m in ["UP", "FLAT"]:
        if trend_exhaustion_filter("BUY", closes_1m, rsi_1m):
            return None

        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "BUY", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "EXHAUSTION_REVERSAL"
        )

        return build_signal(
            symbol, "EXHAUSTION_REVERSAL", "BUY", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    if sell_trigger and (rsi_3m is None or rsi_3m > 58) and trend_5m in ["DOWN", "FLAT"]:
        if trend_exhaustion_filter("SELL", closes_1m, rsi_1m):
            return None

        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "SELL", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "EXHAUSTION_REVERSAL"
        )

        return build_signal(
            symbol, "EXHAUSTION_REVERSAL", "SELL", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    return None


def check_breakout_retest(symbol, closes_1m, closes_3m, closes_5m, atr_1m):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    sma_fast = sma(closes_1m, 5)
    sma_slow = sma(closes_1m, 20)

    if None in (upper, mid, lower, rsi_1m, sma_fast, sma_slow):
        return None

    rsi_3m = rsi(closes_3m, 14) if closes_3m and len(closes_3m) >= 20 else None
    trend_5m, _, _ = get_5m_trend(closes_5m)

    buy_trigger = (
        trend_5m == "UP" and
        current > mid and
        prev <= mid * 1.001 and
        rsi_1m > 52 and
        sma_fast >= sma_slow
    )

    sell_trigger = (
        trend_5m == "DOWN" and
        current < mid and
        prev >= mid * 0.999 and
        rsi_1m < 48 and
        sma_fast <= sma_slow
    )

    if buy_trigger and (rsi_3m is None or rsi_3m > 50):
        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "BUY", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "BREAKOUT_RETEST"
        )
        return build_signal(
            symbol, "BREAKOUT_RETEST", "BUY", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    if sell_trigger and (rsi_3m is None or rsi_3m < 50):
        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "SELL", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "BREAKOUT_RETEST"
        )
        return build_signal(
            symbol, "BREAKOUT_RETEST", "SELL", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    return None


def check_momentum_pullback(symbol, closes_1m, closes_3m, closes_5m, atr_1m):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    sma_fast = sma(closes_1m, 5)
    sma_slow = sma(closes_1m, 20)

    if None in (upper, mid, lower, rsi_1m, sma_fast, sma_slow):
        return None

    rsi_3m = rsi(closes_3m, 14) if closes_3m and len(closes_3m) >= 20 else None
    trend_5m, _, _ = get_5m_trend(closes_5m)

    buy_trigger = (
        trend_5m == "UP" and
        38 <= rsi_1m <= 48 and
        current > prev and
        current > mid * 0.998
    )

    sell_trigger = (
        trend_5m == "DOWN" and
        52 <= rsi_1m <= 62 and
        current < prev and
        current < mid * 1.002
    )

    if buy_trigger and (rsi_3m is None or rsi_3m > 45):
        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "BUY", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "MOMENTUM_PULLBACK"
        )
        return build_signal(
            symbol, "MOMENTUM_PULLBACK", "BUY", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    if sell_trigger and (rsi_3m is None or rsi_3m < 55):
        expiry, confidence, quality, rank = get_expiry_confidence_rank(
            "SELL", current, prev, upper, lower, rsi_1m, rsi_3m,
            sma_fast, sma_slow, trend_5m, atr_1m, "MOMENTUM_PULLBACK"
        )
        return build_signal(
            symbol, "MOMENTUM_PULLBACK", "SELL", current,
            rsi_1m, rsi_3m, upper, mid, lower, atr_1m,
            expiry, confidence, quality, rank, trend_5m
        )

    return None


def build_signal(symbol, setup, direction, price, rsi_1m, rsi_3m, upper, mid, lower,
                 atr_1m, expiry, confidence, quality, rank, trend_5m):
    return {
        "setup": setup,
        "symbol": symbol,
        "direction": direction,
        "price": round(price, 5),
        "rsi_1m": round(rsi_1m, 2),
        "rsi_3m": round(rsi_3m, 2) if rsi_3m is not None else "N/A",
        "upper": round(upper, 5),
        "mid": round(mid, 5),
        "lower": round(lower, 5),
        "atr_1m": round(atr_1m, 5),
        "expiry": expiry,
        "confidence": confidence,
        "quality": quality,
        "rank": rank,
        "trend_5m": trend_5m,
        "signal_time_utc": iso_now(),
        "signal_time_ny": ny_now().strftime("%Y-%m-%d %H:%M:%S")
    }


# --------------------------
# Engine
# --------------------------
def signal_for_symbol(symbol: str):
    candles_1m = fetch_ohlc(symbol, "1min", 100)
    candles_3m = fetch_ohlc(symbol, "3min", 60)
    candles_5m = fetch_ohlc(symbol, "5min", 50)

    if not candles_1m or len(candles_1m) < 25:
        return None

    closes_1m = closes_from_ohlc(candles_1m)
    closes_3m = closes_from_ohlc(candles_3m) if candles_3m else None
    closes_5m = closes_from_ohlc(candles_5m) if candles_5m else None

    current = closes_1m[-1]
    atr_1m = atr(candles_1m, 14)

    if atr_1m is None or not atr_filter(atr_1m, current):
        return None

    if not expansion_filter(candles_1m, atr_1m):
        return None

    signal = check_exhaustion_reversal(symbol, closes_1m, closes_3m, closes_5m, atr_1m)
    if signal:
        return signal

    signal = check_breakout_retest(symbol, closes_1m, closes_3m, closes_5m, atr_1m)
    if signal:
        return signal

    signal = check_momentum_pullback(symbol, closes_1m, closes_3m, closes_5m, atr_1m)
    if signal:
        return signal

    return None


def should_send(signal):
    key = f"{signal['symbol']}:{signal['direction']}:{signal['setup']}"
    now_ts = time.time()
    last_time = LAST_SIGNAL.get(key, 0)

    if now_ts - last_time < COOLDOWN_SECONDS:
        return False

    LAST_SIGNAL[key] = now_ts
    return True


def log_signal(signal):
    entry = {
        "id": f"{signal['symbol']}|{signal['setup']}|{signal['direction']}|{signal['signal_time_utc']}",
        "logged_at": iso_now(),
        "pair": signal["symbol"],
        "setup": signal["setup"],
        "direction": signal["direction"],
        "expiry": signal["expiry"],
        "confidence": signal["confidence"],
        "quality": signal["quality"],
        "rank": signal["rank"],
        "trend_5m": signal["trend_5m"],
        "entry_price": signal["price"],
        "rsi_1m": signal["rsi_1m"],
        "rsi_3m": signal["rsi_3m"],
        "atr_1m": signal["atr_1m"],
        "bb_upper": signal["upper"],
        "bb_mid": signal["mid"],
        "bb_lower": signal["lower"],
        "signal_time_utc": signal["signal_time_utc"],
        "signal_time_ny": signal["signal_time_ny"],
        "resolve_after_utc": (utc_now() + timedelta(minutes=parse_expiry_minutes(signal["expiry"]))).isoformat(),
        "status": "OPEN",
        "result": None,
        "resolved_price": None,
        "resolved_at_utc": None
    }

    SIGNAL_LOG.append(entry)
    if len(SIGNAL_LOG) > MAX_LOG_ITEMS:
        del SIGNAL_LOG[0]


def resolve_signal_results():
    while True:
        try:
            now_dt = utc_now()

            for entry in SIGNAL_LOG:
                if entry["status"] != "OPEN":
                    continue

                resolve_after = datetime.fromisoformat(entry["resolve_after_utc"])
                if now_dt < resolve_after:
                    continue

                latest_price = fetch_latest_price(entry["pair"])
                if latest_price is None:
                    continue

                entry_price = entry["entry_price"]
                direction = entry["direction"]

                if direction == "BUY":
                    if latest_price > entry_price:
                        result = "WIN"
                    elif latest_price < entry_price:
                        result = "LOSS"
                    else:
                        result = "DRAW"
                else:
                    if latest_price < entry_price:
                        result = "WIN"
                    elif latest_price > entry_price:
                        result = "LOSS"
                    else:
                        result = "DRAW"

                entry["status"] = "CLOSED"
                entry["result"] = result
                entry["resolved_price"] = round(latest_price, 5)
                entry["resolved_at_utc"] = iso_now()

                update_pair_stats(entry["pair"], result)
                print(f"Resolved {entry['pair']} {entry['direction']} {entry['expiry']} => {result}")

            time.sleep(20)

        except Exception as e:
            print(f"Resolver loop error: {e}")
            time.sleep(20)


def build_message(signal):
    return (
        f"⚡ <b>SILENT SURGE PRO MAX</b>\n\n"
        f"💱 <b>PAIR:</b> {signal['symbol']}\n\n"
        f"🧩 <b>SETUP:</b> {signal['setup']}\n"
        f"🏅 <b>RANK:</b> <b>{signal['rank']}</b>\n"
        f"🎯 <b>DIRECTION:</b> <b>{signal['direction']}</b>\n"
        f"⏱ <b>EXPIRY:</b> <b>{signal['expiry']}</b>\n"
        f"📊 <b>CONFIDENCE:</b> <b>{signal['confidence']}%</b>\n"
        f"🔥 <b>QUALITY:</b> {signal['quality']}\n"
        f"🧭 <b>5M TREND:</b> {signal['trend_5m']}\n\n"
        f"💰 <b>PRICE:</b> {signal['price']}\n"
        f"📈 <b>RSI 1M:</b> {signal['rsi_1m']}\n"
        f"📈 <b>RSI 3M:</b> {signal['rsi_3m']}\n"
        f"🌊 <b>ATR 1M:</b> {signal['atr_1m']}\n"
        f"📉 <b>BB UPPER:</b> {signal['upper']}\n"
        f"➖ <b>BB MID:</b> {signal['mid']}\n"
        f"📉 <b>BB LOWER:</b> {signal['lower']}\n\n"
        f"🕒 <b>TIME:</b> {ny_now().strftime('%H:%M:%S')} NY"
    )


# --------------------------
# Loops
# --------------------------
def scan_loop():
    time.sleep(10)

    while True:
        try:
            can_trade, reason = session_filter()

            if not can_trade:
                print(f"Session blocked: {reason}")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            minute = utc_now().minute
            group = PAIRS_A if minute % 2 == 0 else PAIRS_B

            print(f"Scanning group: {group}")

            for symbol in group:
                signal = signal_for_symbol(symbol)
                if signal and should_send(signal):
                    if signal["rank"] not in ["A+", "A"]:
                        print(f"Skipping low-rank signal for {symbol}: {signal['rank']}")
                        continue

                    log_signal(signal)
                    message = build_message(signal)
                    print(
                        f"Sending signal for {symbol}: "
                        f"{signal['setup']} | {signal['direction']} | "
                        f"{signal['expiry']} | {signal['confidence']}% | {signal['rank']}"
                    )
                    send_telegram(message)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print(f"Scanner loop error: {e}")
            time.sleep(30)


# --------------------------
# Routes
# --------------------------
@app.route("/", methods=["GET"])
def home():
    return "Silent Surge Forex Scanner Running"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time_utc": iso_now(),
        "time_ny": ny_now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_count": len(SIGNAL_LOG)
    })


@app.route("/signals", methods=["GET"])
def signals():
    return jsonify({
        "count": len(SIGNAL_LOG),
        "items": SIGNAL_LOG[-100:]
    })


@app.route("/stats", methods=["GET"])
def stats():
    return jsonify(PAIR_STATS)


# --------------------------
# Start
# --------------------------
def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()


def start_resolver():
    thread = threading.Thread(target=resolve_signal_results, daemon=True)
    thread.start()


start_scanner()
start_resolver()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
