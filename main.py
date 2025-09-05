import os
import json
import math
import ccxt
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- 환경 변수 가져오기 ---
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
CAPITAL = float(os.getenv("CAPITAL_USDT", 100))
EXCHANGE_NAME = os.getenv("EXCHANGE", "gateio")
LEVERAGE = int(os.getenv("LEVERAGE", 3))
MARKET_TYPE = os.getenv("MARKET_TYPE", "future")  # spot / future
PORT = int(os.getenv("PORT", 8000))
SANDBOX = os.getenv("SANDBOX", "true").lower() == "true"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TP_PCT = float(os.getenv("TP_PCT", 3.1)) / 100
WEBHOOK_PASS = os.getenv("WEBHOOK_PASS", "CHANGE_ME")

SPLIT = 4  # 분할매수 횟수


# --- 거래소 연결 ---
exchange_class = getattr(ccxt, EXCHANGE_NAME)
exchange = exchange_class({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": MARKET_TYPE,
        "createMarketBuyOrderRequiresPrice": False
    }
})

if SANDBOX:
    exchange.set_sandbox_mode(True)


# --- 상태 저장 ---
open_positions = {}  # {"BTC/USDT": {...}}


# --- 유틸 함수 ---
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload)
    except Exception as e:
        print("Telegram error:", str(e))


def entry(symbol, side, step):
    """분할 매수 진입"""
    ratio = 1 / SPLIT
    invest = CAPITAL * ratio * LEVERAGE  # USDT 기준 투자 금액

    price = exchange.fetch_ticker(symbol)["last"]
    amount = invest / price  # BTC 수량

    # Gateio 마켓 매수는 quote 단위가 안전하므로 따로 분기
    if side == "buy":
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=invest,   # <-- 투자 금액(USDT)
            params={"cost": invest}  # quote 단위 지정
        )
    else:
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount    # 매도는 수량 그대로
        )

    pos = open_positions.get(symbol, {"side": side, "amount": 0, "avg_price": 0, "tp_order": None})

    # 새 평단 계산
    new_amount = pos["amount"] + amount
    new_avg = (pos["avg_price"] * pos["amount"] + price * amount) / new_amount

    pos["amount"] = new_amount
    pos["avg_price"] = new_avg

    # 기존 TP 주문 취소
    if pos["tp_order"]:
        try:
            exchange.cancel_order(pos["tp_order"]["id"], symbol)
        except Exception:
            pass

    # 새 TP 주문
    tp_price = new_avg * (1 + TP_PCT if side == "buy" else 1 - TP_PCT)

    tp_order = exchange.create_order(
        symbol=symbol,
        type="limit",
        side="sell" if side == "buy" else "buy",
        amount=pos["amount"],
        price=exchange.price_to_precision(symbol, tp_price)
    )

    pos["tp_order"] = tp_order
    open_positions[symbol] = pos

    msg = f"[ENTRY] {symbol} {side.upper()} step {step}\n평단: {new_avg:.4f}, 수량: {new_amount:.4f}\nTP: {tp_price:.4f}"
    send_telegram(msg)

    return {"ok": True, "order": order, "tp_order": tp_order}

def close(symbol):
    """포지션 강제 종료"""
    pos = open_positions.get(symbol)
    if not pos:
        return {"ok": False, "error": "no position"}

    try:
        if pos["tp_order"]:
            exchange.cancel_order(pos["tp_order"]["id"], symbol)
    except Exception:
        pass

    side = "sell" if pos["side"] == "buy" else "buy"
    order = exchange.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=pos["amount"]
    )

    del open_positions[symbol]

    msg = f"[CLOSE] {symbol} {side.upper()} 포지션 종료 완료"
    send_telegram(msg)

    return {"ok": True, "order": order}


# --- Flask Webhook ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if data.get("passphrase") != WEBHOOK_PASS:
        return jsonify({"ok": False, "error": "invalid passphrase"})

    symbol = data.get("symbol", "BTC/USDT")
    side = data.get("side", "buy")
    action = data.get("action", "entry")
    step = int(data.get("step", 1))

    try:
        if action == "entry":
            result = entry(symbol, side, step)
        elif action == "close":
            result = close(symbol)
        else:
            result = {"ok": False, "error": "invalid action"}
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
