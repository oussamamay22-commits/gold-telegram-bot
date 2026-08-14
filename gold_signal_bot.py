"""
بوت إشارات الذهب - نسخة مستقلة بالكامل (بدون TradingView)
================================================================
يقوم هذا السكربت بـ:
1) جلب بيانات شموع الذهب (XAU/USD) من TwelveData على فريمي 15 و30 دقيقة
2) تحليل السيولة: كشف Liquidity Sweep ثم تأكيد CHoCH/BOS
3) حساب الدخول، وقف الخسارة، والأهداف 1-2-3 بناءً على ATR
4) إرسال الإشارة تلقائياً إلى قناة/مجموعة تيليجرام
5) تكرار هذا كل بضع دقائق باستمرار (مع خادم Flask بسيط لإبقاء الخدمة حية على Render)

متغيرات البيئة المطلوبة:
    TELEGRAM_BOT_TOKEN   - توكن بوت تيليجرام
    TELEGRAM_CHAT_ID     - معرف القناة/المجموعة
    TWELVEDATA_API_KEY   - مفتاح API من twelvedata.com
    PORT                 - المنفذ (يوفره Render تلقائياً)
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # كل 5 دقائق افتراضياً

SYMBOL = "XAU/USD"
TIMEFRAMES = ["15min", "30min"]

# إعدادات الاستراتيجية (نفس منطق مؤشر Pine Script)
SWING_LEN = 10
ATR_LEN = 14
SL_ATR_MULT = 1.2
TP_RR = {"tp1": 1.0, "tp2": 2.0, "tp3": 3.0}
EQUAL_TOLERANCE_PCT = 0.15  # هامش تقارب القمم/القيعان
USE_EQUAL_LEVEL = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gold-signal-bot")

app = Flask(__name__)

# لتتبع آخر شمعة أُرسلت لها إشارة (لتفادي التكرار) لكل فريم
last_signal_time = {tf: None for tf in TIMEFRAMES}


# ============================================================
# جلب بيانات الشموع من TwelveData
# ============================================================
def fetch_candles(interval: str, outputsize: int = 150):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "order": "ASC",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"خطأ في الاتصال بـ TwelveData: {e}")
        return None

    if "values" not in data:
        logger.error(f"استجابة غير متوقعة من TwelveData: {data}")
        return None

    candles = []
    for row in data["values"]:
        candles.append({
            "time": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })
    return candles


# ============================================================
# حساب ATR بسيط
# ============================================================
def calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    # متوسط بسيط لآخر period قيمة
    return sum(trs[-period:]) / period


# ============================================================
# كشف القمم والقيعان (Swing Points)
# ============================================================
def find_swing_points(candles, length=10):
    """يرجع قائمة (index, type, price) لكل نقطة swing مؤكدة"""
    swings = []
    n = len(candles)
    for i in range(length, n - length):
        window = candles[i - length:i + length + 1]
        high_i = candles[i]["high"]
        low_i = candles[i]["low"]
        if high_i == max(c["high"] for c in window):
            swings.append((i, "high", high_i))
        if low_i == min(c["low"] for c in window):
            swings.append((i, "low", low_i))
    return swings


# ============================================================
# المنطق الأساسي: كشف Liquidity Sweep + CHoCH وتوليد إشارة
# ============================================================
def analyze(candles):
    if len(candles) < SWING_LEN * 3 + 5:
        return None

    swings = find_swing_points(candles, SWING_LEN)
    swing_highs = [s for s in swings if s[1] == "high"]
    swing_lows = [s for s in swings if s[1] == "low"]

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    last_high_idx, _, last_high_price = swing_highs[-1]
    prev_high_price = swing_highs[-2][2]
    last_low_idx, _, last_low_price = swing_lows[-1]
    prev_low_price = swing_lows[-2][2]

    equal_high_ok = (not USE_EQUAL_LEVEL) or (
        abs(last_high_price - prev_high_price) / last_high_price * 100 <= EQUAL_TOLERANCE_PCT
    )
    equal_low_ok = (not USE_EQUAL_LEVEL) or (
        abs(last_low_price - prev_low_price) / last_low_price * 100 <= EQUAL_TOLERANCE_PCT
    )

    # نفحص آخر 5 شموع بعد آخر swing بحثاً عن sweep ثم choch
    check_start = max(last_high_idx, last_low_idx) + 1
    recent = candles[check_start:]

    atr = calc_atr(candles, ATR_LEN)
    if atr is None:
        return None

    signal = None

    # SELL: sweep لآخر قمة سيولة ثم إغلاق تحت آخر قاع داخلي (CHoCH)
    if equal_high_ok:
        for i, c in enumerate(recent):
            if c["high"] > last_high_price and c["close"] < last_high_price:
                # armed sell -> ابحث عن choch بعدها
                for c2 in recent[i + 1:]:
                    if c2["close"] < last_low_price:
                        entry = c2["close"]
                        sl = last_high_price + atr * SL_ATR_MULT * 0.3 + (c["high"] - last_high_price)
                        sl = max(sl, entry + atr * SL_ATR_MULT)
                        risk = sl - entry
                        signal = {
                            "side": "SELL",
                            "entry": entry,
                            "sl": sl,
                            "tp1": entry - risk * TP_RR["tp1"],
                            "tp2": entry - risk * TP_RR["tp2"],
                            "tp3": entry - risk * TP_RR["tp3"],
                            "time": candles[check_start + i + recent.index(c2) if False else -1]["time"],
                        }
                        signal["time"] = candles[-1]["time"]
                        break
                if signal:
                    break

    # BUY: sweep لآخر قاع سيولة ثم إغلاق فوق آخر قمة داخلية (CHoCH)
    if signal is None and equal_low_ok:
        for i, c in enumerate(recent):
            if c["low"] < last_low_price and c["close"] > last_low_price:
                for c2 in recent[i + 1:]:
                    if c2["close"] > last_high_price:
                        entry = c2["close"]
                        sl = last_low_price - atr * SL_ATR_MULT * 0.3 - (last_low_price - c["low"])
                        sl = min(sl, entry - atr * SL_ATR_MULT)
                        risk = entry - sl
                        signal = {
                            "side": "BUY",
                            "entry": entry,
                            "sl": sl,
                            "tp1": entry + risk * TP_RR["tp1"],
                            "tp2": entry + risk * TP_RR["tp2"],
                            "tp3": entry + risk * TP_RR["tp3"],
                            "time": candles[-1]["time"],
                        }
                        break
                if signal:
                    break

    return signal


# ============================================================
# إرسال الإشارة إلى تيليجرام
# ============================================================
def send_to_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"فشل إرسال الرسالة إلى تيليجرام: {e}")
        return False


def format_message(tf: str, signal: dict) -> str:
    emoji = "🟢" if signal["side"] == "BUY" else "🔴"
    side_ar = "شراء (BUY)" if signal["side"] == "BUY" else "بيع (SELL)"
    return (
        f"{emoji} <b>إشارة جديدة - XAU/USD</b>\n"
        f"الفريم: {tf}\n"
        f"الاتجاه: <b>{side_ar}</b>\n\n"
        f"📍 الدخول: <b>{signal['entry']:.2f}</b>\n"
        f"🛑 وقف الخسارة: <b>{signal['sl']:.2f}</b>\n"
        f"🎯 الهدف 1: {signal['tp1']:.2f}\n"
        f"🎯 الهدف 2: {signal['tp2']:.2f}\n"
        f"🎯 الهدف 3: {signal['tp3']:.2f}\n\n"
        f"⏱ {signal['time']}\n\n"
        f"⚠️ إشارة آلية لأغراض تعليمية وليست نصيحة استثمارية."
    )


# ============================================================
# حلقة الفحص الدورية
# ============================================================
def polling_loop():
    logger.info("بدء حلقة مراقبة الذهب...")
    while True:
        for tf in TIMEFRAMES:
            try:
                candles = fetch_candles(tf)
                if not candles:
                    continue
                signal = analyze(candles)
                if signal and signal["time"] != last_signal_time[tf]:
                    msg = format_message(tf, signal)
                    if send_to_telegram(msg):
                        last_signal_time[tf] = signal["time"]
                        logger.info(f"تم إرسال إشارة {signal['side']} على فريم {tf}")
            except Exception as e:
                logger.error(f"خطأ أثناء تحليل فريم {tf}: {e}")
        time.sleep(POLL_SECONDS)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "last_signals": last_signal_time,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }), 200


if __name__ == "__main__":
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
