import os
import json
import requests
import threading
import ccxt
from fastapi import FastAPI, Request

# FastAPI 앱 생성
app = FastAPI()

# ===== 환경 변수 / 설정 =====
PASS = os.getenv("WEBHOOK_PASS", "changeme")
CAPITAL = float(os.getenv("CAPITAL", 100))     # 기본 투자금
LEVERAGE = int(os.getenv("LEVERAGE", 10))      # 레버리지
TP_PCT = float(os.getenv("TP_PCT", 0.01))      # 익절 비율 (1% 기본)
STATE_FILE = "state.json"

# 분할 매수 비율 예시 (1~4차까지)
DCA = {
    1: 0.25,
    2: 0.25,
    3: 0.25,
    4: 0.25
}

# 락 (멀티스레드 보호용)
lock = threading.Lock()

# 거래소 (Binance 예시, 실제 키는 환경 변수에서 불러옴)
exchange = ccxt.binance({
    "apiKey": os.getenv("BINANCE_API_KEY"),
    "secret": os.getenv("BINANCE_API_SECRET"),
    "enableRateLimit": True,
})


# ===== 유틸 함수 =====
def read_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def notify(msg):
    print("[NOTIFY]", msg)  # 나중에 텔레그램/디스코드 알림 연동 가능

def normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol

def try_set_leverage(symbol, lev):
    try:
        exchange.set_leverage(lev, symbol)
    except Exception as e:
        notify(f"[LEV ERR] {symbol} {e}")

def place_tp(symbol, side, qty, price):
    params = {"reduceOnly": True}
    if side == "long":
        exchange.create_limit_sell_order(symbol, qty, price, params)
    else:
        exchange.create_limit_buy_order(symbol, qty, price, params)


# ===== FastAPI 라우트 =====
@app.get("/")
def root():
    return {"ok": True, "msg": "bot running"}


@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    
    # 패스프레이즈 확인
    if data.get("passphrase") != PASS:
        return {"ok": False, "error": "bad passphrase"}
    
    symbol = normalize_symbol(data.get("symbol", "BTCUSDT"))
    side = data.get("side", "long").lower()   # long/short
    action = data.get("action", "entry").lower()  # entry/add/exit
    step = int(data.get("step", 1))

    ticker = exchange.fetch_ticker(symbol)
    last = float(ticker["last"])

    with lock:
        state = read_state()
        info = state.get(symbol, {"side": None, "qty": 0.0, "avg": None, "tp_id": None})

        # ===== EXIT =====
        if action in ["exit", "close", "tp"]:
            qty = float(info.get("qty") or 0)
            if qty > 0:
                try:
                    if info.get("side") == "long":
                        exchange.create_market_sell_order(symbol, qty, {"reduceOnly": True})
                    elif info.get("side") == "short":
                        exchange.create_market_buy_order(symbol, qty, {"reduceOnly": True})
                except Exception as e:
                    notify(f"[EXIT ERR] {symbol} {e}")
                state[symbol] = {"side": None, "qty": 0.0, "avg": None, "tp_id": None}
                write_state(state)
                notify(f"[{symbol}] EXIT ALL")
                return {"ok": True, "result": "closed"}

        # ===== ENTRY / ADD =====
        try_set_leverage(symbol, LEVERAGE)
        ratio = DCA.get(step, 0.0)
        if ratio <= 0:
            return {"ok": False, "error": "bad step"}

        intended = "long" if side in ["long", "buy"] else "short"
        if info["side"] and info["side"] != intended and (info["qty"] or 0) > 0:
            return {"ok": False, "error": "existing opposite position"}

        notional = CAPITAL * ratio * LEVERAGE
        qty = round(notional / last, 6)

        # 시장가 진입
        if intended == "long":
            exchange.create_market_buy_order(symbol, qty)
        else:
            exchange.create_market_sell_order(symbol, qty)

        # 평균단가/수량 갱신
        prev_qty = float(info.get("qty") or 0)
        prev_avg = float(info.get("avg") or 0) if info.get("avg") else None
        new_qty = prev_qty + qty
        if prev_avg:
            new_avg = (prev_avg * prev_qty + last * qty) / new_qty
        else:
            new_avg = last

        tp_price = new_avg * (1 + TP_PCT) if intended == "long" else new_avg * (1 - TP_PCT)
        tp_price = round(tp_price, 2)

        try:
            place_tp(symbol, intended, new_qty, tp_price)
        except Exception as e:
            notify(f"[TP ERR] {symbol} {e}")

        state[symbol] = {"side": intended, "qty": new_qty, "avg": new_avg, "tp_id": None}
        write_state(state)

        notify(f"[{symbol}] {intended.upper()} STEP {step} qty~{qty} @ {last:.2f} | TP {tp_price:.2f}")
        return {"ok": True, "qty": new_qty, "avg": new_avg, "tp": tp_price}
