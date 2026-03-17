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

SCAN_INTERVAL_SECONDS = 60
COOLDOWN_SECONDS = 1800  # aynı paritede 30 dk tekrar sinyal verme
REQUEST_TIMEOUT = 20

LAST_SIGNAL = {}         # {symbol: {"direction": "...", "time": epoch}}
LATEST_STATUS = {
    "last_scan_ny": None,
    "last_scan_utc": None,
    "last_session": None,
    "last_group": None,
    "last_error": None,
    "last_signal": None,
    "signals_sent_today": 0,
    "bot_started_ny": None,
}

# =========================
# INDICATOR SETTINGS
# =========================
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STDDEV = 2.0

# Sinyal eşiği — çok katı değil, tamamen gevşek de değil
MIN_CONFIDENCE = 72

# Session bazlı minimum confidence
SESSION_MIN_CONFIDENCE = {
    "TOKYO": 76,
    "TOKYO + LONDON": 73,
    "NEW YORK": 72,
    "LOW LIQUIDITY": 82,
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
    except Exception:
        return default

def clamp(n, smallest, largest):
    return max(smallest, min(n, largest))

def mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)

def stddev(values):
    if not values:
        return 0.0
    m = mean(values)
    variance = sum((x - m) ** 2 for x in values) / len(values)
    return math.sqrt(variance)

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram env eksik. Mesaj gönderilemedi.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        print(f"[ERROR] Telegram gönderilemedi: {r.status_code} - {r.text}")
        return False
    except Exception as e:
        print(f"[ERROR] Telegram exception: {e}")
        return False

def get_session_name(hour_ny: int):
    """
    New York saatine göre session belirleme.
    Kullanıcının özellikle istediği mantığa yakın kuruldu:
      19:00 – 23:59  Tokyo
      00:00 – 11:00  Tokyo + London
      12:00 – 16:00  NY
      Diğerleri       Low Liquidity
    """
    if 19 <= hour_ny <= 23:
        return "TOKYO"
    elif 0 <= hour_ny <= 11:
        return "TOKYO + LONDON"
    elif 12 <= hour_ny <= 16:
        return "NEW YORK"
    else:
        return "LOW LIQUIDITY"

def get_active_pairs_for_session(session_name):
    """
    Session'a göre grup dönüşümlü çalışır.
    Burada tüm grup sistemi korunuyor; hangi saatteysek ona göre
    tarama grubu seçiyoruz ki aynı anda her şeyi boğmayalım.
    """
    minute_bucket = now_ny().minute // 20  # 0,1,2
    group_index = minute_bucket % len(PAIR_GROUPS)
    LATEST_STATUS["last_group"] = group_index + 1
    return PAIR_GROUPS[group_index]

def symbol_to_twelvedata(symbol):
    # TwelveData forex sembollerini slash ile bekliyor: EUR/USD, GBP/JPY vb.
    return symbol.strip()

# =========================
# MARKET DATA
# =========================
def fetch_twelvedata_candles(symbol, interval="5min", outputsize=120):
    """
    TwelveData time_series endpoint
    """
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY eksik")

    td_symbol = symbol_to_twelvedata(symbol)
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON"
    }

    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"TwelveData JSON parse hatası: HTTP {r.status_code} - {r.text[:500]}")

    if r.status_code != 200:
        raise RuntimeError(f"TwelveData HTTP error for {symbol}: {r.status_code} - {data}")

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"TwelveData error for {symbol}: {data.get('message')}")

    if "values" not in data or not data.get("values"):
        raise RuntimeError(f"TwelveData values yok: {symbol} -> {data}")

    values = list(reversed(data["values"]))  # eski -> yeni
    candles = []
    for row in values:
        candles.append({
            "datetime": row.get("datetime"),
            "open": safe_float(row.get("open")),
            "high": safe_float(row.get("high")),
            "low": safe_float(row.get("low")),
            "close": safe_float(row.get("close")),
        })

    candles = [c for c in candles if None not in (c["open"], c["high"], c["low"], c["close"])]

    if not candles:
        raise RuntimeError(f"TwelveData geçerli candle dönmedi: {symbol}")

    return candles

# =========================
# INDICATORS
# =========================
def ema(values, period):
    if len(values) < period:
        return []
    result = []
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    result.extend([None] * (period - 1))
    result.append(sma)
    prev = sma
    for price in values[period:]:
        current = (price * k) + (prev * (1 - k))
        result.append(current)
        prev = current
    return result

def rsi(values, period=14):
    if len(values) < period + 1:
        return []

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    rsis = [None] * period
    if avg_loss == 0:
        rsis.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsis.append(100 - (100 / (1 + rs)))

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

        if avg_loss == 0:
            rsis.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))

    return rsis

def bollinger_bands(values, period=20, std_multiplier=2.0):
    if len(values) < period:
        return [], [], []

    middle = []
    upper = []
    lower = []

    for i in range(len(values)):
        if i < period - 1:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            continue

        window = values[i - period + 1:i + 1]
        m = mean(window)
        sd = stddev(window)
        middle.append(m)
        upper.append(m + std_multiplier * sd)
        lower.append(m - std_multiplier * sd)

    return middle, upper, lower

# =========================
# SIGNAL ENGINE
# =========================
def analyze_symbol(symbol):
    candles = fetch_twelvedata_candles(symbol, interval="5min", outputsize=120)
    if len(candles) < 60:
        raise RuntimeError(f"Yetersiz veri: {symbol}")

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    bb_mid, bb_upper, bb_lower = bollinger_bands(closes, BB_PERIOD, BB_STDDEV)

    i = len(closes) - 1
    if any(arr[i] is None for arr in [ema_fast, ema_slow, rsi_vals, bb_mid, bb_upper, bb_lower]):
        raise RuntimeError(f"Göstergeler hazır değil: {symbol}")

    close = closes[i]
    prev_close = closes[i - 1]
    current_rsi = rsi_vals[i]
    prev_rsi = rsi_vals[i - 1]
    ema_fast_now = ema_fast[i]
    ema_slow_now = ema_slow[i]
    bb_u = bb_upper[i]
    bb_l = bb_lower[i]
    bb_m = bb_mid[i]

    # Mum karakteri
    candle_body = abs(closes[i] - opens[i])
    candle_range = highs[i] - lows[i] if highs[i] - lows[i] != 0 else 1e-9
    upper_wick = highs[i] - max(opens[i], closes[i])
    lower_wick = min(opens[i], closes[i]) - lows[i]
    body_ratio = candle_body / candle_range

    # Trend
    uptrend = ema_fast_now > ema_slow_now and close > ema_fast_now
    downtrend = ema_fast_now < ema_slow_now and close < ema_fast_now

    # Momentum
    rsi_rising = current_rsi > prev_rsi
    rsi_falling = current_rsi < prev_rsi

    # Band konumu
    near_upper = close >= (bb_u - (abs(bb_u - bb_l) * 0.10))
    near_lower = close <= (bb_l + (abs(bb_u - bb_l) * 0.10))

    # Basit yorgunluk işaretleri
    small_body = body_ratio < 0.35
    upper_rejection = upper_wick > candle_body * 1.2
    lower_rejection = lower_wick > candle_body * 1.2

    direction = None
    confidence = 0
    reason_parts = []

    # BUY setup
    if uptrend and rsi_rising:
        confidence += 30
        reason_parts.append("trend up")
        reason_parts.append("RSI rising")

        if near_upper:
            confidence += 18
            reason_parts.append("price near upper band")
        if close > prev_close:
            confidence += 10
            reason_parts.append("bullish continuation")
        if body_ratio > 0.45:
            confidence += 10
            reason_parts.append("healthy candle body")
        if lower_rejection:
            confidence += 6
            reason_parts.append("lower wick support")
        if upper_rejection and small_body:
            confidence -= 14
            reason_parts.append("possible exhaustion")

        direction = "UP"

    # SELL setup
    elif downtrend and rsi_falling:
        confidence += 30
        reason_parts.append("trend down")
        reason_parts.append("RSI falling")

        if near_lower:
            confidence += 18
            reason_parts.append("price near lower band")
        if close < prev_close:
            confidence += 10
            reason_parts.append("bearish continuation")
        if body_ratio > 0.45:
            confidence += 10
            reason_parts.append("healthy candle body")
        if upper_rejection:
            confidence += 6
            reason_parts.append("upper wick pressure")
        if lower_rejection and small_body:
            confidence -= 14
            reason_parts.append("possible exhaustion")

        direction = "DOWN"

    else:
        return {
            "symbol": symbol,
            "signal": None,
            "confidence": 0,
            "reason": "Net trend + momentum yok",
            "price": close,
            "rsi": round(current_rsi, 2),
            "ema_fast": round(ema_fast_now, 5),
            "ema_slow": round(ema_slow_now, 5),
        }

    # RSI aşırı bölge yorgunluk filtresi
    if direction == "UP" and current_rsi > 72:
        confidence -= 10
        reason_parts.append("RSI too hot")
    if direction == "DOWN" and current_rsi < 28:
        confidence -= 10
        reason_parts.append("RSI too stretched")

    # Orta banda çok yakınsa kararsız say
    band_width = abs(bb_u - bb_l)
    if band_width > 0:
        dist_to_mid = abs(close - bb_m)
        if dist_to_mid < band_width * 0.08:
            confidence -= 12
            reason_parts.append("too close to mid band")

    confidence = clamp(confidence, 0, 99)

    return {
        "symbol": symbol,
        "signal": direction,
        "confidence": confidence,
        "reason": ", ".join(reason_parts),
        "price": close,
        "rsi": round(current_rsi, 2),
        "ema_fast": round(ema_fast_now, 5),
        "ema_slow": round(ema_slow_now, 5),
    }

def is_on_cooldown(symbol, direction):
    data = LAST_SIGNAL.get(symbol)
    if not data:
        return False

    elapsed = time.time() - data["time"]
    if elapsed < COOLDOWN_SECONDS and data["direction"] == direction:
        return True
    return False

def mark_signal(symbol, direction):
    LAST_SIGNAL[symbol] = {
        "direction": direction,
        "time": time.time()
    }

def build_signal_message(result, session_name):
    arrow = "⬆️" if result["signal"] == "UP" else "⬇️"
    direction_tr = "YUKARI" if result["signal"] == "UP" else "AŞAĞI"

    text = (
        f"🚨 SİNYAL\n"
        f"Parite: {result['symbol']}\n"
        f"Yön: {arrow} {direction_tr}\n"
        f"Güven: %{result['confidence']}\n"
        f"Session: {session_name}\n"
        f"Fiyat: {result['price']}\n"
        f"RSI: {result['rsi']}\n"
        f"EMA{EMA_FAST}: {result['ema_fast']}\n"
        f"EMA{EMA_SLOW}: {result['ema_slow']}\n"
        f"Neden: {result['reason']}\n"
        f"Saat: {fmt_ny()}"
    )
    return text

# =========================
# SCAN LOOP
# =========================
def scan_market_once():
    ny = now_ny()
    utc = now_utc()
    hour_ny = ny.hour
    session_name = get_session_name(hour_ny)

    LATEST_STATUS["last_scan_ny"] = fmt_ny(ny)
    LATEST_STATUS["last_scan_utc"] = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    LATEST_STATUS["last_session"] = session_name
    LATEST_STATUS["last_error"] = None

    pairs = get_active_pairs_for_session(session_name)
    min_conf = SESSION_MIN_CONFIDENCE.get(session_name, MIN_CONFIDENCE)

    print("=" * 80)
    print(f"[SCAN] NY TIME: {fmt_ny(ny)}")
    print(f"[SCAN] UTC TIME: {utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"[SCAN] SESSION: {session_name}")
    print(f"[SCAN] GROUP: {LATEST_STATUS['last_group']}")
    print(f"[SCAN] PAIRS: {pairs}")
    print(f"[SCAN] MIN CONFIDENCE: {min_conf}")

    best_result = None

    for symbol in pairs:
        try:
            result = analyze_symbol(symbol)
            print(f"[{symbol}] -> {result}")

            if result["signal"] is None:
                continue

            if result["confidence"] < min_conf:
                continue

            if is_on_cooldown(symbol, result["signal"]):
                print(f"[{symbol}] cooldown aktif, tekrar gönderilmeyecek.")
                continue

            if (best_result is None) or (result["confidence"] > best_result["confidence"]):
                best_result = result

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    if best_result:
        msg = build_signal_message(best_result, session_name)
        sent = send_telegram_message(msg)
        if sent:
            mark_signal(best_result["symbol"], best_result["signal"])
            LATEST_STATUS["last_signal"] = {
                "symbol": best_result["symbol"],
                "direction": best_result["signal"],
                "confidence": best_result["confidence"],
                "time_ny": fmt_ny(),
                "session": session_name,
            }
            LATEST_STATUS["signals_sent_today"] += 1
            print(f"[SENT] {best_result['symbol']} {best_result['signal']} %{best_result['confidence']}")
        else:
            print("[WARN] Signal bulundu ama Telegram gönderimi başarısız.")
    else:
        print("[SCAN] Uygun sinyal bulunamadı.")

def scan_loop():
    while True:
        try:
            scan_market_once()
        except Exception as e:
            LATEST_STATUS["last_error"] = str(e)
            print(f"[FATAL SCAN ERROR] {e}")

        time.sleep(SCAN_INTERVAL_SECONDS)

# =========================
# FLASK ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    ny = now_ny()
    session_name = get_session_name(ny.hour)

    return jsonify({
        "status": "ok",
        "message": "Signal bot is running",
        "ny_time": fmt_ny(ny),
        "utc_time": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session": session_name,
        "last_scan_ny": LATEST_STATUS["last_scan_ny"],
        "last_signal": LATEST_STATUS["last_signal"],
        "last_error": LATEST_STATUS["last_error"],
        "group": LATEST_STATUS["last_group"],
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "bot_started_ny": LATEST_STATUS["bot_started_ny"],
        "last_scan_ny": LATEST_STATUS["last_scan_ny"],
        "last_scan_utc": LATEST_STATUS["last_scan_utc"],
        "last_session": LATEST_STATUS["last_session"],
        "last_group": LATEST_STATUS["last_group"],
        "last_error": LATEST_STATUS["last_error"],
        "last_signal": LATEST_STATUS["last_signal"],
        "signals_sent_today": LATEST_STATUS["signals_sent_today"],
        "env": {
            "TWELVEDATA_API_KEY_SET": bool(TWELVEDATA_API_KEY),
            "TELEGRAM_TOKEN_SET": bool(TELEGRAM_TOKEN),
            "TELEGRAM_CHAT_ID_SET": bool(TELEGRAM_CHAT_ID),
        }
    })

@app.route("/test-time", methods=["GET"])
def test_time():
    ny = now_ny()
    return jsonify({
        "ny_time_full": fmt_ny(ny),
        "hour": ny.hour,
        "minute": ny.minute,
        "session": get_session_name(ny.hour),
    })

@app.route("/test-scan", methods=["GET"])
def test_scan():
    try:
        scan_market_once()
        return jsonify({
            "ok": True,
            "message": "Manual scan completed",
            "last_signal": LATEST_STATUS["last_signal"],
            "last_error": LATEST_STATUS["last_error"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

# =========================
# STARTUP
# =========================
def startup_message():
    text = (
        f"✅ Bot başlatıldı\n"
        f"NY Time: {fmt_ny()}\n"
        f"Session: {get_session_name(now_ny().hour)}\n"
        f"Scan interval: {SCAN_INTERVAL_SECONDS}s\n"
        f"Cooldown: {COOLDOWN_SECONDS}s"
    )
    send_telegram_message(text)

def start_background_scanner():
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    LATEST_STATUS["bot_started_ny"] = fmt_ny()
    print("=" * 80)
    print("[BOOT] Bot starting...")
    print(f"[BOOT] NY TIME: {fmt_ny()}")
    print(f"[BOOT] Session: {get_session_name(now_ny().hour)}")
    print(f"[BOOT] PORT: {PORT}")
    print(f"[BOOT] TWELVEDATA_API_KEY_SET: {bool(TWELVEDATA_API_KEY)}")
    print(f"[BOOT] TELEGRAM_TOKEN_SET: {bool(TELEGRAM_TOKEN)}")
    print(f"[BOOT] TELEGRAM_CHAT_ID_SET: {bool(TELEGRAM_CHAT_ID)}")
    print("=" * 80)

    startup_message()
    start_background_scanner()
    app.run(host="0.0.0.0", port=PORT)
