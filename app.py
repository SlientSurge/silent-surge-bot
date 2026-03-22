from flask import Flask, jsonify
import requests
import os
import time
import threading
import math
from datetime import datetime, timezone, timedelta
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
MAX_TELEGRAM_TEXT = 3900
MAX_LOG_ITEMS = 1500
PRIMARY_OUTPUTSIZE = 140

STATE_LOCK = threading.Lock()

LAST_SIGNAL = {}
PAIR_STATS = {}
SIGNAL_LOG = []

PERFORMANCE_DB = {
    "by_pair": {},
    "by_setup": {},
    "by_session": {},
    "by_pair_setup": {},
    "by_pair_session": {},
    "by_setup_session": {},
    "by_conf_bucket": {},
}

LATEST_STATUS = {
    "bot_started_ny": None,
    "scanner_started": False,
    "resolver_started": False,
    "scanner_heartbeat_utc": None,
    "resolver_heartbeat_utc": None,
    "last_scan_ny": None,
    "last_scan_utc": None,
    "last_session": None,
    "last_group": None,
    "last_error": None,
    "last_signal": None,
    "signals_sent_today": 0,
    "market_open": None,
    "market_status": None,
}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "SilentSurgeLearn/4.0"})

# =========================
# SESSION / PAIR MAP
# =========================
PAIR_PRIORITY_BY_SESSION = {
    "TOKYO": [
        "USD/JPY", "EUR/JPY", "AUD/JPY", "CAD/JPY", "AUD/USD"
    ],
    "TOKYO + LONDON": [
        "EUR/USD", "GBP/USD", "EUR/JPY", "GBP/JPY", "USD/CAD", "USD/JPY", "AUD/USD"
    ],
    "NEW YORK": [
        "EUR/USD", "GBP/USD", "USD/CAD", "AUD/USD", "USD/JPY"
    ],
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

def iso_utc():
    return now_utc().isoformat()

def clamp(n, smallest, largest):
    return max(smallest, min(n, largest))

def mean(values):
    return sum(values) / len(values) if values else 0.0

def stddev(values):
    if not values:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))

def parse_td_datetime(dt_str):
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def parse_expiry_minutes(expiry_text):
    txt = (expiry_text or "").strip().lower()
    if txt.startswith("1"):
        return 1
    if txt.startswith("2"):
        return 2
    if txt.startswith("5"):
        return 5
    return 3

def confidence_bucket(conf):
    lo = int(conf // 10) * 10
    lo = clamp(lo, 40, 90)
    hi = lo + 9
    return f"{lo}-{hi}"

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.", flush=True)
        return False

    if len(text) > MAX_TELEGRAM_TEXT:
        text = text[:MAX_TELEGRAM_TEXT]

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = HTTP.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=REQUEST_TIMEOUT
        )
        if r.status_code != 200:
            print(f"Telegram HTTP error {r.status_code}: {r.text}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False

# =========================
# MARKET OPEN FILTER
# =========================
def get_market_status():
    now = now_ny()
    weekday = now.weekday()
    hour = now.hour

    # Monday = 0 ... Sunday = 6

    if weekday == 5:
        return False, "CLOSED_SATURDAY"

    if weekday == 6 and hour < 17:
        return False, "CLOSED_SUNDAY_PREOPEN"

    if weekday == 4 and hour >= 17:
        return False, "CLOSED_FRIDAY_POSTCLOSE"

    return True, "OPEN"

def is_market_open_now():
    return get_market_status()[0]

# =========================
# SESSION ENGINE
# =========================
def get_session_name(hour):
    if 22 <= hour <= 23:
        return "TOKYO"
    elif 0 <= hour <= 11:
        return "TOKYO + LONDON"
    elif 12 <= hour <= 16:
        return "NEW YORK"
    return "LOW LIQUIDITY"

def is_tradeable_session(session_name):
    return session_name in {"TOKYO", "TOKYO + LONDON", "NEW YORK"}

def session_min_rank(session_name):
    if session_name == "TOKYO":
        return ["A+", "A"]
    if session_name == "TOKYO + LONDON":
        return ["A+", "A"]
    if session_name == "NEW YORK":
        return ["A+", "A", "B"]
    return []

def get_pairs_for_session(session_name):
    return PAIR_PRIORITY_BY_SESSION.get(session_name, [])

# =========================
# DATA
# =========================
def fetch_candles(symbol, interval="1min", outputsize=140):
    if not TWELVEDATA_API_KEY:
        raise ValueError("TWELVEDATA_API_KEY missing")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON",
    }

    r = HTTP.get(url, params=params, timeout=REQUEST_TIMEOUT)
    data = r.json()

    if "values" not in data:
        raise ValueError(f"Bad API response for {symbol} {interval}: {data}")

    values = list(reversed(data["values"]))
    candles = []
    for v in values:
        candles.append({
            "datetime": v.get("datetime"),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        })
    return candles

def build_5m_from_1m(candles_1m):
    if not candles_1m or len(candles_1m) < 5:
        return None

    buckets = {}
    for c in candles_1m:
        dt = parse_td_datetime(c["datetime"])
        if dt is None:
            continue

        minute_floor = dt.minute - (dt.minute % 5)
        bucket_dt = dt.replace(minute=minute_floor, second=0, microsecond=0)
        key = bucket_dt.isoformat()

        if key not in buckets:
            buckets[key] = {
                "datetime": bucket_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
        else:
            buckets[key]["high"] = max(buckets[key]["high"], c["high"])
            buckets[key]["low"] = min(buckets[key]["low"], c["low"])
            buckets[key]["close"] = c["close"]

    out = list(buckets.values())
    out.sort(key=lambda x: x["datetime"])
    return out if out else None

def fetch_latest_price(symbol):
    candles = fetch_candles(symbol, interval="1min", outputsize=2)
    if not candles:
        return None
    return candles[-1]["close"]

def closes_from_ohlc(candles):
    return [c["close"] for c in candles]

# =========================
# INDICATORS
# =========================
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
    return prev

def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(-period, 0):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def bollinger(values, period=20, num_std=2):
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    m = mean(window)
    sd = stddev(window)
    upper = m + num_std * sd
    lower = m - num_std * sd
    return upper, m, lower

def atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period

def candle_range(candle):
    return candle["high"] - candle["low"]

def candle_body(candle):
    return abs(candle["close"] - candle["open"])

def upper_wick(candle):
    return candle["high"] - max(candle["open"], candle["close"])

def lower_wick(candle):
    return min(candle["open"], candle["close"]) - candle["low"]

def body_to_range_ratio(candle):
    rng = candle_range(candle)
    if rng <= 0:
        return 0.0
    return candle_body(candle) / rng

def upper_wick_ratio(candle):
    rng = candle_range(candle)
    if rng <= 0:
        return 0.0
    return upper_wick(candle) / rng

def lower_wick_ratio(candle):
    rng = candle_range(candle)
    if rng <= 0:
        return 0.0
    return lower_wick(candle) / rng

def get_5m_trend(closes_5m):
    if len(closes_5m) < 20:
        return "N/A"
    fast = sma(closes_5m, 5)
    slow = sma(closes_5m, 20)
    if fast is None or slow is None:
        return "N/A"
    if fast > slow:
        return "UP"
    if fast < slow:
        return "DOWN"
    return "FLAT"

# =========================
# FILTERS
# =========================
def detect_market_regime(price, upper, mid, lower, rsi_1m, atr_1m):
    if None in (price, upper, mid, lower, rsi_1m, atr_1m):
        return "UNKNOWN"

    band_width = upper - lower
    rel_band = band_width / max(price, 1e-9)
    rel_atr = atr_1m / max(price, 1e-9)

    if rel_atr < 0.00020:
        return "DEAD"
    if rel_atr > 0.0015:
        return "EXPLOSIVE"
    if rel_band < 0.0007:
        return "RANGE"
    if rsi_1m >= 58 or rsi_1m <= 42:
        return "TREND"
    return "RANGE"

def atr_filter(atr_1m, price):
    if atr_1m is None or price <= 0:
        return False
    return (atr_1m / price) >= 0.00020

def expansion_filter(candles_1m, atr_1m):
    if not candles_1m or len(candles_1m) < 5 or atr_1m is None:
        return False

    last_range = candle_range(candles_1m[-1])
    prev_ranges = [candle_range(c) for c in candles_1m[-5:-1]]
    avg_prev = mean(prev_ranges)

    if last_range < atr_1m * 0.40:
        return False

    if avg_prev > 0 and last_range < avg_prev * 0.70:
        return False

    return True

def trend_exhaustion_filter(direction, closes_1m, rsi_1m):
    if len(closes_1m) < 4:
        return False

    c2, c3, c4 = closes_1m[-3], closes_1m[-2], closes_1m[-1]

    if direction == "BUY":
        if c4 < c3 < c2 and rsi_1m < 22:
            return True

    if direction == "SELL":
        if c4 > c3 > c2 and rsi_1m > 78:
            return True

    return False

def hard_no_trade_filter(price, regime, rsi_1m, atr_1m, ema_fast, ema_slow, upper, lower):
    if None in (price, regime, rsi_1m, atr_1m, ema_fast, ema_slow, upper, lower):
        return True

    if regime == "DEAD":
        return True
    if regime == "EXPLOSIVE":
        return True
    if (upper - lower) < price * 0.0005:
        return True
    if 48 < rsi_1m < 52:
        return True
    if abs(ema_fast - ema_slow) < price * 0.00005:
        return True
    return False

def small_body_filter(candles_1m):
    if not candles_1m:
        return True
    return body_to_range_ratio(candles_1m[-1]) < 0.30

def wick_rejection_filter(direction, candles_1m):
    if not candles_1m:
        return False
    last = candles_1m[-1]

    if direction == "BUY":
        return upper_wick_ratio(last) >= 0.50
    elif direction == "SELL":
        return lower_wick_ratio(last) >= 0.50
    return False

def late_entry_guard(direction, current, upper, lower, atr_1m):
    if None in (current, upper, lower, atr_1m):
        return True

    if direction == "BUY":
        overshoot = current - upper
        if overshoot > atr_1m * 0.45:
            return True

    if direction == "SELL":
        overshoot = lower - current
        if overshoot > atr_1m * 0.45:
            return True

    return False

def fake_breakout_filter(direction, candles_1m, upper, lower, mid, atr_1m):
    if not candles_1m or len(candles_1m) < 3:
        return False

    c2 = candles_1m[-2]
    c3 = candles_1m[-1]
    rng2 = candle_range(c2)
    rng3 = candle_range(c3)

    if direction == "BUY":
        if c2["close"] > upper:
            if body_to_range_ratio(c2) < 0.35:
                return True
            if upper_wick_ratio(c2) > 0.45:
                return True
            if c3["close"] < c2["close"] and upper_wick_ratio(c3) > 0.45:
                return True
            if (c2["close"] - upper) > atr_1m * 0.60:
                return True
            if rng2 > 0 and rng3 > 0 and rng3 < rng2 * 0.45 and c3["close"] <= c2["close"]:
                return True

    if direction == "SELL":
        if c2["close"] < lower:
            if body_to_range_ratio(c2) < 0.35:
                return True
            if lower_wick_ratio(c2) > 0.45:
                return True
            if c3["close"] > c2["close"] and lower_wick_ratio(c3) > 0.45:
                return True
            if (lower - c2["close"]) > atr_1m * 0.60:
                return True
            if rng2 > 0 and rng3 > 0 and rng3 < rng2 * 0.45 and c3["close"] >= c2["close"]:
                return True

    return False

def session_pair_alignment_filter(symbol, session_name):
    return symbol in get_pairs_for_session(session_name)

# =========================
# PERFORMANCE MEMORY / LEARNING
# =========================
def ensure_perf_bucket(section, key):
    if key not in PERFORMANCE_DB[section]:
        PERFORMANCE_DB[section][key] = {
            "signals": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "win_rate": 0.0
        }

def update_perf_bucket(section, key, result):
    ensure_perf_bucket(section, key)
    bucket = PERFORMANCE_DB[section][key]
    bucket["signals"] += 1
    if result == "WIN":
        bucket["wins"] += 1
    elif result == "LOSS":
        bucket["losses"] += 1
    elif result == "DRAW":
        bucket["draws"] += 1

    decisive = bucket["wins"] + bucket["losses"]
    bucket["win_rate"] = round((bucket["wins"] / decisive) * 100, 2) if decisive else 0.0

def update_performance_memory(entry, result):
    pair = entry["pair"]
    setup = entry["setup"]
    session = entry["session"]
    conf_bucket = confidence_bucket(entry["confidence"])

    update_perf_bucket("by_pair", pair, result)
    update_perf_bucket("by_setup", setup, result)
    update_perf_bucket("by_session", session, result)
    update_perf_bucket("by_pair_setup", f"{pair}|{setup}", result)
    update_perf_bucket("by_pair_session", f"{pair}|{session}", result)
    update_perf_bucket("by_setup_session", f"{setup}|{session}", result)
    update_perf_bucket("by_conf_bucket", conf_bucket, result)

def performance_adjustment(symbol, setup, session_name, confidence):
    adj = 0

    with STATE_LOCK:
        ps = PERFORMANCE_DB["by_pair_setup"].get(f"{symbol}|{setup}")
        ss = PERFORMANCE_DB["by_setup_session"].get(f"{setup}|{session_name}")
        confb = PERFORMANCE_DB["by_conf_bucket"].get(confidence_bucket(confidence))
        pairsess = PERFORMANCE_DB["by_pair_session"].get(f"{symbol}|{session_name}")

    def calc_local(bucket):
        if not bucket:
            return 0
        decisive = bucket["wins"] + bucket["losses"]
        if decisive < 5:
            return 0
        wr = bucket["win_rate"]
        if wr >= 70:
            return 3
        if wr >= 60:
            return 1
        if wr <= 35:
            return -4
        if wr <= 45:
            return -2
        return 0

    adj += calc_local(ps)
    adj += calc_local(ss)
    adj += calc_local(confb)
    adj += calc_local(pairsess)

    return clamp(adj, -8, 6)

def is_blacklist_candidate(symbol, setup, session_name):
    with STATE_LOCK:
        ps = PERFORMANCE_DB["by_pair_setup"].get(f"{symbol}|{setup}")
        pss = PERFORMANCE_DB["by_pair_session"].get(f"{symbol}|{session_name}")

    for bucket in (ps, pss):
        if not bucket:
            continue
        decisive = bucket["wins"] + bucket["losses"]
        if decisive >= 6 and bucket["win_rate"] <= 30:
            return True

    return False

def top_bucket_items(section, limit=10, min_decisive=3):
    with STATE_LOCK:
        items = list(PERFORMANCE_DB[section].items())

    scored = []
    for key, val in items:
        decisive = val["wins"] + val["losses"]
        if decisive < min_decisive:
            continue
        scored.append({
            "key": key,
            "signals": val["signals"],
            "wins": val["wins"],
            "losses": val["losses"],
            "draws": val["draws"],
            "win_rate": val["win_rate"],
            "decisive": decisive
        })

    scored.sort(key=lambda x: (x["win_rate"], x["decisive"]), reverse=True)
    return scored[:limit]

def bottom_bucket_items(section, limit=10, min_decisive=3):
    with STATE_LOCK:
        items = list(PERFORMANCE_DB[section].items())

    scored = []
    for key, val in items:
        decisive = val["wins"] + val["losses"]
        if decisive < min_decisive:
            continue
        scored.append({
            "key": key,
            "signals": val["signals"],
            "wins": val["wins"],
            "losses": val["losses"],
            "draws": val["draws"],
            "win_rate": val["win_rate"],
            "decisive": decisive
        })

    scored.sort(key=lambda x: (x["win_rate"], -x["decisive"]))
    return scored[:limit]

# =========================
# SCORING / EXPIRY
# =========================
def classify_market_state(setup_type, direction, rsi_1m, trend_5m, ema_fast, ema_slow, current, mid):
    if setup_type == "EXHAUSTION_REVERSAL":
        return "REVERSAL"

    if setup_type == "BREAKOUT_RETEST":
        if direction == "BUY" and trend_5m == "UP" and ema_fast >= ema_slow and current > mid:
            return "STRONG_TREND"
        if direction == "SELL" and trend_5m == "DOWN" and ema_fast <= ema_slow and current < mid:
            return "STRONG_TREND"
        return "WEAK_TREND"

    if setup_type == "MOMENTUM_PULLBACK":
        if direction == "BUY":
            if trend_5m == "UP" and rsi_1m >= 42 and ema_fast >= ema_slow:
                return "STRONG_TREND"
            return "WEAK_TREND"
        if direction == "SELL":
            if trend_5m == "DOWN" and rsi_1m <= 58 and ema_fast <= ema_slow:
                return "STRONG_TREND"
            return "WEAK_TREND"

    return "WEAK_TREND"

def expiry_from_market_state(market_state):
    if market_state == "REVERSAL":
        return "1 min"
    if market_state == "STRONG_TREND":
        return "5 min"
    return "2 min"

def get_confidence_quality_rank(direction, current, prev, upper, lower,
                                rsi_1m, ema_fast, ema_slow,
                                trend_5m, atr_1m, setup_type, market_state):
    confidence = 50

    if setup_type == "EXHAUSTION_REVERSAL":
        confidence += 6
    elif setup_type == "BREAKOUT_RETEST":
        confidence += 10
    elif setup_type == "MOMENTUM_PULLBACK":
        confidence += 8

    if market_state == "STRONG_TREND":
        confidence += 6
    elif market_state == "REVERSAL":
        confidence += 4

    if direction == "BUY":
        band_distance = abs(current - lower)

        if rsi_1m < 25:
            confidence += 16
        elif rsi_1m < 30:
            confidence += 10
        elif rsi_1m < 35:
            confidence += 5

        if current > prev:
            confidence += 8

        if ema_fast >= ema_slow:
            confidence += 10
        elif ema_fast >= ema_slow * 0.998:
            confidence += 4

        if trend_5m in ["UP", "FLAT"]:
            confidence += 12
        elif trend_5m == "DOWN":
            confidence -= 12

        if band_distance / max(current, 1e-9) < 0.0006:
            confidence += 8

    elif direction == "SELL":
        band_distance = abs(current - upper)

        if rsi_1m > 75:
            confidence += 16
        elif rsi_1m > 70:
            confidence += 10
        elif rsi_1m > 65:
            confidence += 5

        if current < prev:
            confidence += 8

        if ema_fast <= ema_slow:
            confidence += 10
        elif ema_fast <= ema_slow * 1.002:
            confidence += 4

        if trend_5m in ["DOWN", "FLAT"]:
            confidence += 12
        elif trend_5m == "UP":
            confidence -= 12

        if band_distance / max(current, 1e-9) < 0.0006:
            confidence += 8

    if atr_1m is not None:
        rel_atr = atr_1m / max(current, 1e-9)
        if rel_atr > 0.0007:
            confidence += 5
        elif rel_atr > 0.0005:
            confidence += 2

    confidence = max(40, min(confidence, 95))

    if confidence >= 92:
        quality = "Strong"
        rank = "A+"
    elif confidence >= 80:
        quality = "Good"
        rank = "A"
    else:
        quality = "Moderate"
        rank = "B"

    return confidence, quality, rank

def build_signal(symbol, setup, direction, current, prev, rsi_1m,
                 upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name):
    market_state = classify_market_state(
        setup, direction, rsi_1m, trend_5m, ema_fast, ema_slow, current, mid
    )
    expiry = expiry_from_market_state(market_state)
    confidence, quality, rank = get_confidence_quality_rank(
        direction, current, prev, upper, lower,
        rsi_1m, ema_fast, ema_slow, trend_5m, atr_1m, setup, market_state
    )

    learn_adj = performance_adjustment(symbol, setup, session_name, confidence)
    confidence = clamp(confidence + learn_adj, 40, 95)

    if confidence >= 92:
        quality = "Strong"
        rank = "A+"
    elif confidence >= 80:
        quality = "Good"
        rank = "A"
    else:
        quality = "Moderate"
        rank = "B"

    return {
        "setup": setup,
        "market_state": market_state,
        "symbol": symbol,
        "direction": direction,
        "price": round(current, 5),
        "rsi_1m": round(rsi_1m, 2),
        "upper": round(upper, 5),
        "mid": round(mid, 5),
        "lower": round(lower, 5),
        "atr_1m": round(atr_1m, 5),
        "expiry": expiry,
        "confidence": confidence,
        "quality": quality,
        "rank": rank,
        "trend_5m": trend_5m,
        "signal_time_utc": iso_utc(),
        "signal_time_ny": fmt_ny(),
        "learn_adjustment": learn_adj
    }

# =========================
# SETUPS
# =========================
def check_exhaustion_reversal(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    ema_fast = ema(closes_1m, 9)
    ema_slow = ema(closes_1m, 21)
    trend_5m = get_5m_trend(closes_5m)

    if None in (upper, mid, lower, rsi_1m, ema_fast, ema_slow):
        return None

    buy_trigger = (
        current <= lower * 1.0015 and
        rsi_1m <= 25 and
        current > prev and
        ema_fast >= ema_slow * 0.997
    )

    sell_trigger = (
        current >= upper * 0.9985 and
        rsi_1m >= 75 and
        current < prev and
        ema_fast <= ema_slow * 1.003
    )

    if buy_trigger and trend_5m in ["UP", "FLAT"]:
        if trend_exhaustion_filter("BUY", closes_1m, rsi_1m):
            return None
        if wick_rejection_filter("BUY", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        return build_signal(
            symbol, "EXHAUSTION_REVERSAL", "BUY", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    if sell_trigger and trend_5m in ["DOWN", "FLAT"]:
        if trend_exhaustion_filter("SELL", closes_1m, rsi_1m):
            return None
        if wick_rejection_filter("SELL", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        return build_signal(
            symbol, "EXHAUSTION_REVERSAL", "SELL", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    return None

def check_breakout_retest(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    ema_fast = ema(closes_1m, 9)
    ema_slow = ema(closes_1m, 21)
    trend_5m = get_5m_trend(closes_5m)

    if None in (upper, mid, lower, rsi_1m, ema_fast, ema_slow):
        return None

    buy_trigger = (
        trend_5m == "UP" and
        current > mid * 1.0001 and
        prev <= mid * 1.0008 and
        52 <= rsi_1m <= 70 and
        ema_fast >= ema_slow * 1.00005
    )

    sell_trigger = (
        trend_5m == "DOWN" and
        current < mid * 0.9999 and
        prev >= mid * 0.9992 and
        30 <= rsi_1m <= 48 and
        ema_fast <= ema_slow * 0.99995
    )

    if buy_trigger:
        if wick_rejection_filter("BUY", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        if late_entry_guard("BUY", current, upper, lower, atr_1m):
            return None
        if fake_breakout_filter("BUY", candles_1m, upper, lower, mid, atr_1m):
            return None
        return build_signal(
            symbol, "BREAKOUT_RETEST", "BUY", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    if sell_trigger:
        if wick_rejection_filter("SELL", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        if late_entry_guard("SELL", current, upper, lower, atr_1m):
            return None
        if fake_breakout_filter("SELL", candles_1m, upper, lower, mid, atr_1m):
            return None
        return build_signal(
            symbol, "BREAKOUT_RETEST", "SELL", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    return None

def check_momentum_pullback(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name):
    current = closes_1m[-1]
    prev = closes_1m[-2]

    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    ema_fast = ema(closes_1m, 9)
    ema_slow = ema(closes_1m, 21)
    trend_5m = get_5m_trend(closes_5m)

    if None in (upper, mid, lower, rsi_1m, ema_fast, ema_slow):
        return None

    buy_trigger = (
        trend_5m == "UP" and
        42 <= rsi_1m <= 49 and
        current > prev and
        current > mid * 0.9995 and
        ema_fast >= ema_slow
    )

    sell_trigger = (
        trend_5m == "DOWN" and
        51 <= rsi_1m <= 58 and
        current < prev and
        current < mid * 1.0005 and
        ema_fast <= ema_slow
    )

    if buy_trigger:
        if wick_rejection_filter("BUY", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        return build_signal(
            symbol, "MOMENTUM_PULLBACK", "BUY", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    if sell_trigger:
        if wick_rejection_filter("SELL", candles_1m):
            return None
        if small_body_filter(candles_1m):
            return None
        return build_signal(
            symbol, "MOMENTUM_PULLBACK", "SELL", current, prev, rsi_1m,
            upper, mid, lower, atr_1m, ema_fast, ema_slow, trend_5m, session_name
        )

    return None

# =========================
# ENGINE
# =========================
def signal_for_symbol(symbol, session_name):
    if not session_pair_alignment_filter(symbol, session_name):
        return None

    candles_1m = fetch_candles(symbol, interval="1min", outputsize=PRIMARY_OUTPUTSIZE)
    if not candles_1m or len(candles_1m) < 30:
        return None

    candles_5m = build_5m_from_1m(candles_1m)
    if not candles_5m or len(candles_5m) < 20:
        return None

    closes_1m = closes_from_ohlc(candles_1m)
    closes_5m = closes_from_ohlc(candles_5m)

    current = closes_1m[-1]
    upper, mid, lower = bollinger(closes_1m, 20, 2)
    rsi_1m = rsi(closes_1m, 14)
    ema_fast = ema(closes_1m, 9)
    ema_slow = ema(closes_1m, 21)
    atr_1m = atr(candles_1m, 14)

    if None in (current, upper, mid, lower, rsi_1m, ema_fast, ema_slow, atr_1m):
        return None

    regime = detect_market_regime(current, upper, mid, lower, rsi_1m, atr_1m)

    if not atr_filter(atr_1m, current):
        return None

    if not expansion_filter(candles_1m, atr_1m):
        return None

    if hard_no_trade_filter(current, regime, rsi_1m, atr_1m, ema_fast, ema_slow, upper, lower):
        return None

    signal = check_exhaustion_reversal(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name)
    if signal:
        signal["regime"] = regime
        return signal

    signal = check_breakout_retest(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name)
    if signal:
        signal["regime"] = regime
        return signal

    signal = check_momentum_pullback(symbol, closes_1m, closes_5m, candles_1m, atr_1m, session_name)
    if signal:
        signal["regime"] = regime
        return signal

    return None

def should_send(signal):
    key = f"{signal['symbol']}:{signal['direction']}:{signal['setup']}"
    now_ts = time.time()
    with STATE_LOCK:
        last_time = LAST_SIGNAL.get(key, 0)
    return (now_ts - last_time) >= COOLDOWN_SECONDS

def mark_signal_sent(signal):
    key = f"{signal['symbol']}:{signal['direction']}:{signal['setup']}"
    with STATE_LOCK:
        LAST_SIGNAL[key] = time.time()

def ensure_pair_stats(symbol):
    with STATE_LOCK:
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
    decisive = stats["wins"] + stats["losses"]
    stats["win_rate"] = round((stats["wins"] / decisive) * 100, 2) if decisive else 0.0

def log_signal(signal, session_name):
    entry = {
        "id": f"{signal['symbol']}|{signal['setup']}|{signal['direction']}|{signal['signal_time_utc']}",
        "logged_at": iso_utc(),
        "pair": signal["symbol"],
        "session": session_name,
        "setup": signal["setup"],
        "regime": signal.get("regime", "UNKNOWN"),
        "market_state": signal["market_state"],
        "direction": signal["direction"],
        "expiry": signal["expiry"],
        "confidence": signal["confidence"],
        "quality": signal["quality"],
        "rank": signal["rank"],
        "trend_5m": signal["trend_5m"],
        "entry_price": signal["price"],
        "rsi_1m": signal["rsi_1m"],
        "atr_1m": signal["atr_1m"],
        "bb_upper": signal["upper"],
        "bb_mid": signal["mid"],
        "bb_lower": signal["lower"],
        "signal_time_utc": signal["signal_time_utc"],
        "signal_time_ny": signal["signal_time_ny"],
        "learn_adjustment": signal.get("learn_adjustment", 0),
        "resolve_after_utc": (now_utc() + timedelta(minutes=parse_expiry_minutes(signal["expiry"]))).isoformat(),
        "status": "OPEN",
        "result": None,
        "resolved_price": None,
        "resolved_at_utc": None
    }

    with STATE_LOCK:
        SIGNAL_LOG.append(entry)
        if len(SIGNAL_LOG) > MAX_LOG_ITEMS:
            del SIGNAL_LOG[0]

def build_message(signal, session_name):
    la = signal.get("learn_adjustment", 0)
    la_text = f"{la:+d}"
    return (
        f"⚡ <b>SILENT SURGE LEARN</b>\n\n"
        f"💱 <b>PAIR:</b> {signal['symbol']}\n"
        f"🕒 <b>SESSION:</b> {session_name}\n\n"
        f"🧩 <b>SETUP:</b> {signal['setup']}\n"
        f"🌐 <b>REGIME:</b> {signal.get('regime', 'UNKNOWN')}\n"
        f"📍 <b>MARKET STATE:</b> {signal['market_state']}\n"
        f"🏅 <b>RANK:</b> <b>{signal['rank']}</b>\n"
        f"🎯 <b>DIRECTION:</b> <b>{signal['direction']}</b>\n"
        f"⏱ <b>EXPIRY:</b> <b>{signal['expiry']}</b>\n"
        f"📊 <b>CONFIDENCE:</b> <b>{signal['confidence']}%</b>\n"
        f"🧠 <b>LEARN ADJ:</b> {la_text}\n"
        f"🔥 <b>QUALITY:</b> {signal['quality']}\n"
        f"🧭 <b>5M TREND:</b> {signal['trend_5m']}\n\n"
        f"💰 <b>PRICE:</b> {signal['price']}\n"
        f"📈 <b>RSI 1M:</b> {signal['rsi_1m']}\n"
        f"🌊 <b>ATR 1M:</b> {signal['atr_1m']}\n"
        f"📉 <b>BB UPPER:</b> {signal['upper']}\n"
        f"➖ <b>BB MID:</b> {signal['mid']}\n"
        f"📉 <b>BB LOWER:</b> {signal['lower']}\n\n"
        f"🕒 <b>TIME:</b> {fmt_ny()}"
    )

def resolve_signal_results():
    LATEST_STATUS["resolver_started"] = True
    while True:
        try:
            LATEST_STATUS["resolver_heartbeat_utc"] = iso_utc()
            now_dt = now_utc()

            with STATE_LOCK:
                open_entries = [e for e in SIGNAL_LOG if e["status"] == "OPEN"]

            for entry in open_entries:
                try:
                    resolve_after = datetime.fromisoformat(entry["resolve_after_utc"])
                except Exception:
                    continue

                if now_dt < resolve_after:
                    continue

                latest_price = fetch_latest_price(entry["pair"])
                if latest_price is None:
                    continue

                entry_price = entry["entry_price"]
                direction = entry["direction"]

                if direction == "BUY":
                    result = "WIN" if latest_price > entry_price else "LOSS" if latest_price < entry_price else "DRAW"
                else:
                    result = "WIN" if latest_price < entry_price else "LOSS" if latest_price > entry_price else "DRAW"

                with STATE_LOCK:
                    if entry["status"] != "OPEN":
                        continue
                    entry["status"] = "CLOSED"
                    entry["result"] = result
                    entry["resolved_price"] = round(latest_price, 5)
                    entry["resolved_at_utc"] = iso_utc()

                    update_pair_stats(entry["pair"], result)
                    update_performance_memory(entry, result)

                print(
                    f"Resolved {entry['pair']} | {entry['setup']} | {entry['direction']} | {entry['expiry']} => {result}",
                    flush=True
                )

            time.sleep(20)
        except Exception as e:
            LATEST_STATUS["last_error"] = f"resolver: {str(e)}"
            print(f"Resolver loop error: {e}", flush=True)
            time.sleep(20)

# =========================
# LOOP
# =========================
def scan():
    ny = now_ny()
    utc = now_utc()
    session_name = get_session_name(ny.hour)
    market_open, market_status = get_market_status()

    LATEST_STATUS["last_scan_ny"] = fmt_ny(ny)
    LATEST_STATUS["last_scan_utc"] = utc.isoformat()
    LATEST_STATUS["last_session"] = session_name
    LATEST_STATUS["scanner_heartbeat_utc"] = utc.isoformat()
    LATEST_STATUS["market_open"] = market_open
    LATEST_STATUS["market_status"] = market_status

    if not market_open:
        print(f"Market closed: {market_status}", flush=True)
        return

    if not is_tradeable_session(session_name):
        print(f"Session blocked: {session_name}", flush=True)
        return

    pairs = get_pairs_for_session(session_name)
    LATEST_STATUS["last_group"] = pairs

    best_signal = None

    for symbol in pairs:
        try:
            signal = signal_for_symbol(symbol, session_name)
            if not signal:
                continue

            if signal["rank"] not in session_min_rank(session_name):
                continue

            if not should_send(signal):
                continue

            if is_blacklist_candidate(symbol, signal["setup"], session_name):
                print(f"Blacklist candidate skipped: {symbol} {signal['setup']} {session_name}", flush=True)
                continue

            if (best_signal is None) or (signal["confidence"] > best_signal["confidence"]):
                best_signal = signal

        except Exception as e:
            LATEST_STATUS["last_error"] = f"{symbol}: {str(e)}"
            print(f"Error on {symbol}: {e}", flush=True)

    if best_signal:
        message = build_message(best_signal, session_name)
        sent = send_telegram_message(message)
        if sent:
            mark_signal_sent(best_signal)
            log_signal(best_signal, session_name)

            LATEST_STATUS["last_signal"] = {
                "symbol": best_signal["symbol"],
                "setup": best_signal["setup"],
                "direction": best_signal["direction"],
                "confidence": best_signal["confidence"],
                "rank": best_signal["rank"],
                "session": session_name,
                "learn_adjustment": best_signal.get("learn_adjustment", 0),
                "time_ny": fmt_ny()
            }
            LATEST_STATUS["signals_sent_today"] += 1

def loop():
    LATEST_STATUS["bot_started_ny"] = fmt_ny()
    LATEST_STATUS["scanner_started"] = True

    time.sleep(8)

    while True:
        try:
            scan()
        except Exception as e:
            LATEST_STATUS["last_error"] = f"scan: {str(e)}"
            print(f"Scan loop error: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)

# =========================
# API
# =========================
@app.route("/", methods=["GET"])
def home():
    market_open, market_status = get_market_status()
    return jsonify({
        "status": "running",
        "time_ny": fmt_ny(),
        "market_open": market_open,
        "market_status": market_status,
        "session": get_session_name(now_ny().hour),
        "scanner_started": LATEST_STATUS["scanner_started"],
        "resolver_started": LATEST_STATUS["resolver_started"],
        "last_signal": LATEST_STATUS["last_signal"]
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify(LATEST_STATUS)

@app.route("/signals", methods=["GET"])
def signals():
    with STATE_LOCK:
        items = SIGNAL_LOG[-100:]
        count = len(SIGNAL_LOG)
    return jsonify({"count": count, "items": items})

@app.route("/stats", methods=["GET"])
def stats():
    with STATE_LOCK:
        snapshot = dict(PAIR_STATS)
    return jsonify(snapshot)

@app.route("/performance", methods=["GET"])
def performance():
    with STATE_LOCK:
        snapshot = {
            "by_pair": dict(PERFORMANCE_DB["by_pair"]),
            "by_setup": dict(PERFORMANCE_DB["by_setup"]),
            "by_session": dict(PERFORMANCE_DB["by_session"]),
            "by_pair_setup": dict(PERFORMANCE_DB["by_pair_setup"]),
            "by_pair_session": dict(PERFORMANCE_DB["by_pair_session"]),
            "by_setup_session": dict(PERFORMANCE_DB["by_setup_session"]),
            "by_conf_bucket": dict(PERFORMANCE_DB["by_conf_bucket"]),
        }
    return jsonify(snapshot)

@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    return jsonify({
        "top_pairs": top_bucket_items("by_pair", limit=8),
        "top_setups": top_bucket_items("by_setup", limit=8),
        "top_sessions": top_bucket_items("by_session", limit=8),
        "bottom_pairs": bottom_bucket_items("by_pair", limit=8),
        "bottom_pair_setup": bottom_bucket_items("by_pair_setup", limit=8),
        "conf_buckets": top_bucket_items("by_conf_bucket", limit=8, min_decisive=2),
    })

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=resolve_signal_results, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
