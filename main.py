import os, json, requests
from fastapi import FastAPI, Request

app = FastAPI()   # ✅ FastAPI 앱 정의 추가

@app.get("/")
def root():
    return {"ok": True, "msg": "bot running"}

@app.get('/')
def root():
return {'ok': True, 'msg': 'bot running'}


@app.post('/webhook')
async def webhook(req: Request):
data = await req.json()
if data.get('passphrase') != PASS:
return {'ok': False, 'error': 'bad passphrase'}


symbol = normalize_symbol(data.get('symbol', 'BTCUSDT'))
side = data.get('side', 'long').lower() # long/short
action = data.get('action', 'entry').lower() # entry/add/exit
step = int(data.get('step', 1))


ticker = exchange.fetch_ticker(symbol)
last = float(ticker['last'])


with lock:
state = read_state()
info = state.get(symbol, {'side': None, 'qty': 0.0, 'avg': None, 'tp_id': None})


if action in ['exit', 'close', 'tp']:
qty = float(info.get('qty') or 0)
if qty > 0:
try:
if info.get('side') == 'long':
exchange.create_market_sell_order(symbol, qty, {'reduceOnly': True})
elif info.get('side') == 'short':
exchange.create_market_buy_order(symbol, qty, {'reduceOnly': True})
except Exception as e:
notify(f"[EXIT ERR] {symbol} {e}")
state[symbol] = {'side': None, 'qty': 0.0, 'avg': None, 'tp_id': None}
write_state(state)
notify(f"[{symbol}] EXIT ALL")
return {'ok': True, 'result': 'closed'}


# entry/add (분할)
try_set_leverage(symbol, LEVERAGE)
ratio = DCA.get(step, 0.0)
if ratio <= 0:
return {'ok': False, 'error': 'bad step'}


intended = 'long' if side in ['long', 'buy'] else 'short'
if info['side'] and info['side'] != intended and (info['qty'] or 0) > 0:
return {'ok': False, 'error': 'existing opposite position'}


notional = CAPITAL * ratio * LEVERAGE
qty = round(notional / last, 6) # 심볼에 따라 정밀도 다름(간단화)


# 시장가 진입
if intended == 'long':
exchange.create_market_buy_order(symbol, qty)
else:
exchange.create_market_sell_order(symbol, qty)


# 평균단가/수량 갱신
prev_qty = float(info.get('qty') or 0)
prev_avg = float(info.get('avg') or 0) if info.get('avg') else None
new_qty = prev_qty + qty
if prev_avg:
new_avg = (prev_avg * prev_qty + last * qty) / new_qty
else:
new_avg = last


tp_price = new_avg * (1 + TP_PCT) if intended == 'long' else new_avg * (1 - TP_PCT)
# 소수점 간단 반올림(거래소/심볼별 세부 틱사이즈는 추후 보정)
tp_price = round(tp_price, 2)


# 새 TP(익절) 걸기
try:
place_tp(symbol, intended, new_qty, tp_price)
except Exception as e:
notify(f"[TP ERR] {symbol} {e}")


state[symbol] = {'side': intended, 'qty': new_qty, 'avg': new_avg, 'tp_id': None}
write_state(state)


notify(f"[{symbol}] {intended.upper()} STEP {step} qty~{qty} @ {last:.2f} | TP {tp_price:.2f}")
return {'ok': True, 'qty': new_qty, 'avg': new_avg, 'tp': tp_price}
