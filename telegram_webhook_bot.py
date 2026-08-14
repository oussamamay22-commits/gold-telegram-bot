"""
بوت استقبال إشارات الذهب من TradingView وإرسالها إلى تيليجرام
================================================================
الفكرة:
1) مؤشر Pine Script (gold_liquidity_signals.pine) يعمل على شارت الذهب
   على فريم 15 أو 30 دقيقة داخل TradingView، ويولّد تنبيهات (Alerts)
   بصيغة JSON تحتوي على: side, symbol, timeframe, entry, sl, tp1, tp2, tp3.
2) عند حدوث Alert، يقوم TradingView بإرسال طلب Webhook (POST) إلى هذا
   السيرفر.
3) هذا السيرفر يستقبل البيانات، يهيئها كرسالة منسقة، ويرسلها فوراً إلى
   قناة/مجموعة تيليجرام عبر Bot API.

المتطلبات (requirements.txt):
    flask
    requests
    python-dotenv

التشغيل محلياً:
    1) أنشئ بوت تيليجرام عبر @BotFather واحصل على TELEGRAM_BOT_TOKEN
    2) احصل على TELEGRAM_CHAT_ID (معرف القناة/المجموعة/الحساب الذي سيستقبل الإشارات)
    3) عدّل ملف .env أو متغيرات البيئة بالقيم المناسبة
    4) شغل: python telegram_webhook_bot.py
    5) لجعل الرابط متاحاً من الإنترنت (لأن TradingView لا يصل لجهازك
       المحلي مباشرة)، استخدم ngrok مؤقتاً للتجربة:
           ngrok http 5000
       ثم استخدم الرابط الذي يعطيك إياه ngrok كعنوان الـ Webhook في
       تنبيهات TradingView. للتشغيل الدائم يُفضّل نشر السيرفر على
       VPS أو منصة استضافة (Render, Railway, PythonAnywhere...).
    6) في TradingView: افتح شاشة إعداد التنبيه (Alert) على المؤشر،
       فعّل خيار Webhook URL وضع فيه: https://YOUR_DOMAIN/webhook
"""

import os
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# سر بسيط للتحقق من أن الطلب قادم فعلاً من TradingView (اختياري لكن يُنصح به)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold-signal-bot")


def send_to_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"فشل إرسال الرسالة إلى تيليجرام: {e}")
        return False


def format_signal_message(data: dict) -> str:
    side = str(data.get("side", "")).upper()
    symbol = data.get("symbol", "XAUUSD")
    timeframe = data.get("timeframe", "")
    entry = data.get("entry", "N/A")
    sl = data.get("sl", "N/A")
    tp1 = data.get("tp1", "N/A")
    tp2 = data.get("tp2", "N/A")
    tp3 = data.get("tp3", "N/A")
    time_str = data.get("time", "")

    emoji = "🟢" if side == "BUY" else "🔴"
    side_ar = "شراء (BUY)" if side == "BUY" else "بيع (SELL)"

    msg = (
        f"{emoji} <b>إشارة جديدة - {symbol}</b>\n"
        f"الفريم: {timeframe}\n"
        f"الاتجاه: <b>{side_ar}</b>\n\n"
        f"📍 الدخول: <b>{entry}</b>\n"
        f"🛑 وقف الخسارة: <b>{sl}</b>\n"
        f"🎯 الهدف 1: {tp1}\n"
        f"🎯 الهدف 2: {tp2}\n"
        f"🎯 الهدف 3: {tp3}\n\n"
        f"⏱ {time_str}\n\n"
        f"⚠️ هذه إشارة آلية لأغراض تعليمية وليست نصيحة استثمارية. "
        f"التزم بإدارة رأس المال الخاصة بك."
    )
    return msg


@app.route("/webhook", methods=["POST"])
def webhook():
    # التحقق من السر إن كان مفعّلاً (يُرسل كـ query param: ?secret=xxx)
    if WEBHOOK_SECRET:
        provided = request.args.get("secret", "")
        if provided != WEBHOOK_SECRET:
            logger.warning("محاولة وصول غير مصرح بها إلى الـ webhook")
            return jsonify({"status": "unauthorized"}), 401

    # TradingView يرسل البيانات كنص أحياناً وليس JSON خالص، لذا نحاول القراءتين
    data = request.get_json(silent=True)
    if data is None:
        try:
            import json
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            logger.error(f"تعذر تحليل البيانات الواردة: {request.data}")
            return jsonify({"status": "invalid_payload"}), 400

    logger.info(f"إشارة واردة: {data}")

    message = format_signal_message(data)
    sent = send_to_telegram(message)

    if sent:
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "telegram_send_failed"}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
