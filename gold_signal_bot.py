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
SWING_LEN = 7
ATR_LEN = 14
SL_ATR_MULT = 1.5
TP_RR = {"tp1": 1.5, "tp2": 2.5, "tp3": 4.0}
EQUAL_TOLERANCE_PCT = 0.15  # هامش تقارب القمم/القيعان (يُستخدم فقط إذا USE_EQUAL_LEVEL=True)

# اشتراط تقارب القمم/القيعان (Equal Highs/Lows) يناسب الأسواق المتذبذبة
# (كالجلسة الآسيوية) لكنه يرفض أغلب الفرص في الأسواق الاتجاهية السريعة
# (لندن/نيويورك). عطّلناه هنا لالتقاط فرص من كل الجلسات - أي قمة/قاع
# سيولة حقيقي يُعتبر منطقة صالحة بغض النظر عن التماثل.
USE_EQUAL_LEVEL = False

# فحص أمان: إذا كان سعر الدخول المحسوب بعيداً جداً عن آخر سعر حالي فعلي
# (يعني الإشارة قديمة/غير متزامنة مع السوق)، لا نرسلها
MAX_ENTRY_DEVIATION_PCT = 0.3  # نسبة مئوية كحد أقصى للفارق المسموح به

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
# ملاحظة مهمة: نطلق الإشارة فقط إذا كانت الشمعة الأخيرة المغلقة
# نفسها هي التي أكدت الـ CHoCH (وليس أي شمعة قديمة من الماضي) -
# هذا يضمن أن سعر الدخول = السعر الحالي دائماً، ويمنع تكرار نفس
# الإشارة القديمة في كل فحص دوري.
# ============================================================
LOOKBACK_FOR_SWEEP = 30  # كم شمعة للخلف نبحث فيها عن sweep قبل الشمعة الحالية


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

    atr = calc_atr(candles, ATR_LEN)
    if atr is None:
        return None

    last_idx = len(candles) - 1
    last_candle = candles[last_idx]  # الشمعة الأخيرة المغلقة فقط

    # نطاق البحث عن sweep: بين آخر swing والشمعة الحالية (باستثناء الحالية نفسها)
    scan_start = max(last_high_idx, last_low_idx) + 1
    scan_start = max(scan_start, last_idx - LOOKBACK_FOR_SWEEP)
    window = candles[scan_start:last_idx]  # لا يشمل last_candle

    signal = None

    # SELL: هل حدث sweep لقمة سيولة في النافذة الأخيرة، والشمعة الحالية
    # (فقط) هي أول من يغلق تحت آخر قاع داخلي (CHoCH فوري)؟
    if equal_high_ok:
        had_sweep = any(c["high"] > last_high_price and c["close"] < last_high_price for c in window)
        fresh_choch = last_candle["close"] < last_low_price
        # تأكد أن الشمعة *قبل* الحالية لم تكن قد أكدت الكسر مسبقاً (لتفادي التكرار)
        prev_already_broke = len(window) > 0 and window[-1]["close"] < last_low_price
        if had_sweep and fresh_choch and not prev_already_broke:
            entry = last_candle["close"]
            sl = last_high_price + atr * SL_ATR_MULT
            risk = sl - entry
            if risk > 0:
                signal = {
                    "side": "SELL",
                    "entry": entry,
                    "sl": sl,
                    "tp1": entry - risk * TP_RR["tp1"],
                    "tp2": entry - risk * TP_RR["tp2"],
                    "tp3": entry - risk * TP_RR["tp3"],
                    "time": last_candle["time"],
                }

    # BUY: نفس المنطق بالعكس
    if signal is None and equal_low_ok:
        had_sweep = any(c["low"] < last_low_price and c["close"] > last_low_price for c in window)
        fresh_choch = last_candle["close"] > last_high_price
        prev_already_broke = len(window) > 0 and window[-1]["close"] > last_high_price
        if had_sweep and fresh_choch and not prev_already_broke:
            entry = last_candle["close"]
            sl = last_low_price - atr * SL_ATR_MULT
            risk = entry - sl
            if risk > 0:
                signal = {
                    "side": "BUY",
                    "entry": entry,
                    "sl": sl,
                    "tp1": entry + risk * TP_RR["tp1"],
                    "tp2": entry + risk * TP_RR["tp2"],
                    "tp3": entry + risk * TP_RR["tp3"],
                    "time": last_candle["time"],
                }

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


def format_message(tf: str, signal: dict, current_price: float) -> str:
    emoji = "🟢" if signal["side"] == "BUY" else "🔴"
    side_ar = "شراء (BUY)" if signal["side"] == "BUY" else "بيع (SELL)"
    return (
        f"{emoji} <b>إشارة جديدة - XAU/USD</b>\n"
        f"الفريم: {tf}\n"
        f"الاتجاه: <b>{side_ar}</b>\n\n"
        f"💰 السعر الحالي وقت الإرسال: {current_price:.2f}\n"
        f"📍 الدخول: <b>{signal['entry']:.2f}</b>\n"
        f"🛑 وقف الخسارة: <b>{signal['sl']:.2f}</b>\n"
        f"🎯 الهدف 1: {signal['tp1']:.2f}\n"
        f"🎯 الهدف 2: {signal['tp2']:.2f}\n"
        f"🎯 الهدف 3: {signal['tp3']:.2f}\n\n"
        f"⏱ {signal['time']}\n\n"
        f"⚠️ إشارة آلية لأغراض تعليمية وليست نصيحة استثمارية. "
        f"قارن دائماً السعر الحالي أعلاه بشارتك قبل التنفيذ."
    )


# ============================================================
# حلقة الفحص الدورية
# ============================================================
def polling_loop():
    logger.info("بدء حلقة مراقبة الذهب...")

    # رسالة اختبار تُرسل مرة واحدة عند بدء تشغيل البوت للتأكد من عمل الاتصال بتيليجرام
    test_msg = (
        "✅ <b>بوت إشارات الذهب يعمل الآن</b>\n\n"
        f"يراقب: XAU/USD على فريمي {', '.join(TIMEFRAMES)}\n"
        "سيصلك تنبيه تلقائي فور تحقق شروط الدخول (Liquidity Sweep + CHoCH).\n\n"
        "⚠️ هذه رسالة اختبار فقط وليست إشارة تداول."
    )
    if send_to_telegram(test_msg):
        logger.info("تم إرسال رسالة الاختبار بنجاح")
    else:
        logger.error("فشل إرسال رسالة الاختبار - تحقق من TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID")

    while True:
        for tf in TIMEFRAMES:
            try:
                candles = fetch_candles(tf)
                if not candles:
                    continue
                current_price = candles[-1]["close"]
                signal = analyze(candles)
                if signal and signal["time"] != last_signal_time[tf]:
                    # فحص أمان: تجاهل الإشارة إذا كان الدخول بعيداً جداً عن السعر الحالي
                    deviation_pct = abs(signal["entry"] - current_price) / current_price * 100
                    if deviation_pct > MAX_ENTRY_DEVIATION_PCT:
                        logger.warning(
                            f"تم تجاهل إشارة {signal['side']} على {tf}: "
                            f"الدخول {signal['entry']:.2f} بعيد عن السعر الحالي {current_price:.2f} "
                            f"(فارق {deviation_pct:.2f}%)"
                        )
                        last_signal_time[tf] = signal["time"]
                        continue
                    msg = format_message(tf, signal, current_price)
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


@app.route("/price", methods=["GET"])
def price_debug():
    """يعرض آخر سعر جلبه البوت فعلياً لكل فريم - لمقارنته مباشرة بشارت TradingView"""
    result = {}
    for tf in TIMEFRAMES:
        candles = fetch_candles(tf, outputsize=2)
        if candles:
            last = candles[-1]
            result[tf] = {
                "time": last["time"],
                "open": last["open"],
                "high": last["high"],
                "low": last["low"],
                "close": last["close"],
            }
        else:
            result[tf] = {"error": "فشل جلب البيانات"}
    return jsonify(result), 200


if __name__ == "__main__":
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
