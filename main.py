# main.py - Cleaned and deduplicated
import os
import sys
import time
import json
import logging
import traceback
import threading
import requests
import importlib
import subprocess
from dotenv import load_dotenv
import ccxt
import websocket
import asyncio

# Workaround for asyncio/aiodns compatibility on Windows
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Install and import required packages
def install_and_import(package, import_name=None):
    try:
        if import_name is None:
            import_name = package
        importlib.import_module(import_name)
    except ImportError:
        print(f"[Setup] Installing missing package: {package}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        importlib.invalidate_caches()
        importlib.import_module(import_name)

# Ensure required packages are installed
install_and_import('ccxt')
install_and_import('python-dotenv', 'dotenv')
install_and_import('websocket-client', 'websocket')

# Load environment variables from .env file
# load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE")

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Symbol mapping (Binance to Bitget format) - dynamic for all available USDT pairs
bitget = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_PASSPHRASE,
    "enableRateLimit": True,
})
bitget_markets = bitget.load_markets()
SYMBOL_MAP = {symbol.replace('/', ''): symbol for symbol in bitget_markets if symbol.endswith('/USDT')}

# Bitget spot and futures clients
bitget_spot = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_PASSPHRASE,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})
bitget_futures = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_PASSPHRASE,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},  # USDT-margined perpetual
})

PROCESSED_TRADES_FILE = "processed_trades_ccxt.txt"
processed_trades = set()
if os.path.exists(PROCESSED_TRADES_FILE):
    with open(PROCESSED_TRADES_FILE, "r") as f:
        processed_trades = set(line.strip() for line in f if line.strip())

def save_processed_trade(trade_id):
    processed_trades.add(str(trade_id))
    with open(PROCESSED_TRADES_FILE, "a") as f:
        f.write(f"{trade_id}\n")

def place_bitget_order(symbol, side, quantity, price=None):
    """Place a market order on Bitget to mirror Binance trade using ccxt. Uses real Bitget balance for SELL orders."""
    try:
        bitget_symbol = SYMBOL_MAP.get(symbol)
        if not bitget_symbol:
            print(f"❌ No Bitget symbol mapping for {symbol}")
            return False
        side = side.lower()
        params = {}
        if side == "buy":
            if price is None:
                # Fetch latest price from Bitget ticker for higher precision
                ticker = bitget.fetch_ticker(bitget_symbol)
                price = float(ticker['last'])
                print(f"⚠️ No price provided for market BUY, using latest Bitget price: {price}")
            # Buy the same base amount as on Binance
            amount = float(quantity)
            params["createMarketBuyOrderRequiresPrice"] = False
            print(f"[Bitget Debug] Placing BUY order: symbol={bitget_symbol}, amount={amount}, params={params}")
            order = bitget.create_order(
                symbol=bitget_symbol,
                type="market",
                side=side,
                amount=amount,  # amount in base currency
                params=params,
            )
        else:
            # For sell, check Bitget balance and only sell up to available
            base_coin = bitget_symbol.split("/")[0]
            balance = bitget_spot.fetch_balance()
            available = float(balance[base_coin]["free"]) if base_coin in balance and "free" in balance[base_coin] else 0.0
            sell_amount = min(float(quantity), available)
            if sell_amount <= 0:
                print(f"🚫❌ Bitget SELL order failed: No {base_coin} available to sell.")
                return False
            print(f"[Bitget Debug] Placing SELL order: symbol={bitget_symbol}, amount={sell_amount}, params={params}")
            order = bitget.create_order(
                symbol=bitget_symbol,
                type="market",
                side=side,
                amount=sell_amount,  # amount in base currency
                params=params,
            )
        print(f"✅ Successfully placed {side} order on Bitget for {quantity} {bitget_symbol} at market price")
        return True
    except Exception as e:
        # Check for insufficient balance error
        err_msg = str(e)
        if 'Insufficient balance' in err_msg or 'InsufficientFunds' in err_msg or 'code":"43012"' in err_msg:
            print(f"🚫❌ Bitget order failed: INSUFFICIENT BALANCE for {side.upper()} {quantity} {bitget_symbol}")
            print(f"   Please check your Bitget account balance and try again.")
        else:
            print(f"❌ Bitget order error: {e}")
            traceback.print_exc()
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                print(f"[Bitget Error Response] {e.response.text}")
        return False

def place_bitget_futures_order(symbol, side, quantity, price=None, leverage=1):
    logging.info(f"[Futures Order] Attempting to place order: symbol={symbol}, side={side}, quantity={quantity}, price={price}, leverage={leverage}")
    try:
        bitget_symbol = symbol.replace("_", "/") if "/" not in symbol else symbol
        if bitget_symbol not in bitget_futures.load_markets():
            if bitget_symbol.endswith("_PERP"):
                alt_symbol = bitget_symbol.replace("_PERP", "/USD:USD")
            elif bitget_symbol.endswith("USD_PERP"):
                alt_symbol = bitget_symbol.replace("USD_PERP", "USD/USD")
            else:
                alt_symbol = bitget_symbol
            if alt_symbol in bitget_futures.load_markets():
                bitget_symbol = alt_symbol
            else:
                logging.error(f"❌ No Bitget futures symbol mapping for {symbol}. Please check the list above and update your mapping if needed.")
                return False
        side = side.lower()
        params = {"leverage": leverage}
        logging.info(f"[Bitget Futures Debug] Placing {side.upper()} order: symbol={bitget_symbol}, amount={quantity}, params={params}")
        order = bitget_futures.create_order(
            symbol=bitget_symbol,
            type="market",
            side=side,
            amount=quantity,
            params=params,
        )
        logging.info(f"✅ Successfully placed {side} order on Bitget FUTURES for {quantity} {bitget_symbol} at market price")
        return True
    except Exception as e:
        logging.error(f"❌ Bitget futures order error: {e}")
        logging.error(traceback.format_exc())
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            logging.error(f"[Bitget Futures Error Response] {e.response.text}")
        return False

def handle_pretty_message(msg, market_type="spot"):
    event_type = msg.get("e")
    if event_type == "executionReport":
        status = msg.get("X")
        execution_type = msg.get("x")
        symbol = msg.get("s")
        side = msg.get("S")
        quantity = float(msg.get("q"))
        price = float(msg.get("L") or 0)
        trade_id = str(msg.get("t"))
        if status == "FILLED" and execution_type == "TRADE":
            if trade_id not in processed_trades:
                save_processed_trade(trade_id)
                divider = "🟢" + "=" * 30 + "🟢"
                side_icon = "🟩 BUY" if side.upper() == "BUY" else "🟥 SELL"
                market_icon = "💱 SPOT" if market_type == "spot" else "📈 FUTURES"
                logging.info(f"\n{divider}\n{side_icon} {market_icon}")
                logging.info(f"🔹 Symbol   : {symbol}")
                logging.info(f"🔹 Quantity : {quantity}")
                logging.info(f"🔹 Price    : {price:,.4f} USDT")
                logging.info(f"🔹 Total    : {float(quantity) * float(price):,.2f} USDT")
                logging.info(divider)
                logging.info(f"🔄 Mirroring trade on Bitget...")
                if market_type == "spot":
                    result = place_bitget_order(symbol, side, quantity, price)
                else:
                    logging.info(f"[Futures Mirror] Calling place_bitget_futures_order for {symbol}, {side}, {quantity}, {price}")
                    result = place_bitget_futures_order(symbol, side, quantity, price)
                if result:
                    logging.info(f"✅ Mirrored on Bitget [{market_type.upper()}]")
                else:
                    logging.error(f"❌ Mirror failed on Bitget [{market_type.upper()}]")
                logging.info(divider)
    elif event_type == "outboundAccountPosition":
        balances = msg.get("B", [])
        divider = "💼" + "-" * 30 + "💼"
        logging.info(f"\n{divider}\n[ACCOUNT UPDATE - {market_type.upper()}]")
        for asset in balances:
            available = float(asset['f'])
            if available > 0:
                logging.info(f"  💰 {asset['a']}: {available}")
        logging.info(divider)

def get_listen_key(api_key, base_url):
    url = f"{base_url}/fapi/v1/listenKey" if 'fapi' in base_url else f"{base_url}/dapi/v1/listenKey"
    headers = {"X-MBX-APIKEY": api_key}
    response = requests.post(url, headers=headers)
    if response.status_code == 200:
        return response.json()["listenKey"]
    else:
        print(f"Error getting listenKey: {response.status_code} {response.text}")
        return None

def get_listen_key_spot(api_key, base_url):
    """Get listen key for spot trading"""
    url = f"{base_url}/api/v3/userDataStream"
    headers = {"X-MBX-APIKEY": api_key}
    response = requests.post(url, headers=headers)
    if response.status_code == 200:
        return response.json()["listenKey"]
    else:
        print(f"Error getting spot listenKey: {response.status_code} {response.text}")
        return None

def keepalive_listen_key(api_key, listen_key, base_url):
    url = f"{base_url}/fapi/v1/listenKey" if 'fapi' in base_url else f"{base_url}/dapi/v1/listenKey"
    headers = {"X-MBX-APIKEY": api_key}
    while True:
        try:
            requests.put(url, headers=headers, params={"listenKey": listen_key})
        except Exception as e:
            print(f"Error keeping listenKey alive: {e}")
        time.sleep(30 * 60)

def keepalive_listen_key_spot(api_key, listen_key, base_url):
    """Keep spot listen key alive"""
    url = f"{base_url}/api/v3/userDataStream"
    headers = {"X-MBX-APIKEY": api_key}
    while True:
        try:
            requests.put(url, headers=headers, params={"listenKey": listen_key})
        except Exception as e:
            print(f"Error keeping spot listenKey alive: {e}")
        time.sleep(30 * 60)  # Every 30 minutes

def on_futures_message(ws, message, label):
    data = json.loads(message)
    if data.get('e') == 'ORDER_TRADE_UPDATE':
        o = data['o']
        if o['X'] == 'FILLED':
            symbol = o['s']
            side = o['S']
            quantity = float(o['l'])
            price = float(o['L'])
            trade_id = str(o['t'])
            total = quantity * price
            if trade_id not in processed_trades:
                save_processed_trade(trade_id)
                pretty = f"\n✅ {side} order FILLED for {symbol} [{label}]\n" \
                        f"📦 Quantity: {quantity}\n" \
                        f"💰 Price per unit: {price:,.4f} USDT\n" \
                        f"💸 Total spent: {total:,.2f} USDT\n" \
                        f"🔄 Attempting to mirror trade on Bitget..."
                logging.info(pretty)
                result = place_bitget_futures_order(symbol, side, quantity, price)
                if result:
                    logging.info(f"✅ Mirrored trade on Bitget successfully [{label}]")
                else:
                    logging.error(f"❌ Failed to mirror trade on Bitget [{label}]")
                logging.info("-" * 50)
    elif data.get('e') == 'ACCOUNT_UPDATE':
        logging.info(f"[{label}] ACCOUNT UPDATE: {data}")
    else:
        logging.info(f"[{label}] Other event: {data}")

def on_futures_error(ws, error, label):
    print(f"[{label}] WebSocket error: {error}")

def on_futures_close(ws, close_status_code, close_msg, label):
    print(f"[{label}] WebSocket closed: {close_status_code} {close_msg}")

def on_futures_open(ws, label):
    print(f"[{label}] WebSocket connection opened. Listening for real-time {label} trade/account events...")

def start_futures_ws(listen_key, ws_url, label):
    def _on_message(ws, message):
        on_futures_message(ws, message, label)
    def _on_error(ws, error):
        on_futures_error(ws, error, label)
    def _on_close(ws, close_status_code, close_msg):
        on_futures_close(ws, close_status_code, close_msg, label)
    def _on_open(ws):
        on_futures_open(ws, label)
    backoff = 2
    max_backoff = 60
    while True:
        ws = websocket.WebSocketApp(ws_url,
                                    on_message=_on_message,
                                    on_error=_on_error,
                                    on_close=_on_close,
                                    on_open=_on_open)
        try:
            ws.run_forever()
        except Exception as e:
            print(f"[{label}] WebSocket run_forever() error: {e}")
        print(f"[{label}] WebSocket disconnected. Reconnecting in {backoff} seconds...")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

# --- Restore old Binance USDT-margined futures WebSocket logic ---

def start_binance_futures_ws():
    base_url = "https://fapi.binance.com"
    ws_base = "wss://fstream.binance.com/ws/"
    listen_key = get_listen_key(BINANCE_API_KEY, base_url)
    if not listen_key:
        print("[Futures WS] Could not get listenKey, aborting futures monitor.")
        return
    ws_url = ws_base + listen_key
    print(f"[Futures WS] Connecting to {ws_url}")
    def keepalive():
        keepalive_listen_key(BINANCE_API_KEY, listen_key, base_url)
    threading.Thread(target=keepalive, daemon=True).start()
    start_futures_ws(listen_key, ws_url, label="USDT-M")

# Async Binance WebSocket monitor for spot (re-added)
def start_binance_spot_ws():
    """Standard WebSocket implementation for Binance spot trading"""
    base_url = "https://api.binance.com"
    ws_base = "wss://stream.binance.com:9443/ws/"
    
    # Get listen key for spot trading
    listen_key = get_listen_key_spot(BINANCE_API_KEY, base_url)
    if not listen_key:
        print("[Spot WS] Could not get listenKey, aborting spot monitor.")
        return
    
    ws_url = ws_base + listen_key
    print(f"[Spot WS] Connecting to {ws_url}")
    
    # Start keepalive thread
    def keepalive():
        keepalive_listen_key_spot(BINANCE_API_KEY, listen_key, base_url)
    threading.Thread(target=keepalive, daemon=True).start()
    
    # Start WebSocket connection
    start_spot_ws(listen_key, ws_url, label="SPOT")

def start_spot_ws(listen_key, ws_url, label):
    """Start spot WebSocket connection with reconnection logic"""
    def _on_message(ws, message):
        on_spot_message(ws, message, label)
    def _on_error(ws, error):
        on_spot_error(ws, error, label)
    def _on_close(ws, close_status_code, close_msg):
        on_spot_close(ws, close_status_code, close_msg, label)
    def _on_open(ws):
        on_spot_open(ws, label)
    
    backoff = 2
    max_backoff = 60
    while True:
        ws = websocket.WebSocketApp(ws_url,
                                    on_message=_on_message,
                                    on_error=_on_error,
                                    on_close=_on_close,
                                    on_open=_on_open)
        try:
            ws.run_forever()
        except Exception as e:
            print(f"[{label}] WebSocket run_forever() error: {e}")
        print(f"[{label}] WebSocket disconnected. Reconnecting in {backoff} seconds...")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

def on_spot_message(ws, message, label):
    """Handle spot WebSocket messages"""
    try:
        data = json.loads(message)
        handle_pretty_message(data, market_type="spot")
    except Exception as e:
        print(f"[{label}] Error processing message: {e}")

def on_spot_error(ws, error, label):
    print(f"[{label}] WebSocket error: {error}")

def on_spot_close(ws, close_status_code, close_msg, label):
    print(f"[{label}] WebSocket closed: {close_status_code} {close_msg}")

def on_spot_open(ws, label):
    print(f"[{label}] WebSocket connection opened. Listening for real-time {label} trade/account events...")

def on_all_futures_message(ws, message, label):
    data = json.loads(message)
    if data.get('e') == 'ORDER_TRADE_UPDATE':
        o = data['o']
        symbol = o['s']
        side = o['S']
        quantity = float(o['l'])
        price = float(o['L'])
        trade_id = str(o['t'])
        total = quantity * price
        if o['X'] == 'FILLED':
            divider = "🟢" + "=" * 30 + "🟢"
            side_icon = "🟩 BUY" if side.upper() == "BUY" else "🟥 SELL"
            market_icon = f"📈 {label}"
            logging.info(f"\n{divider}\n{side_icon} {market_icon}")
            logging.info(f"🔹 Symbol   : {symbol}")
            logging.info(f"🔹 Quantity : {quantity}")
            logging.info(f"🔹 Price    : {price:,.4f} USDT")
            logging.info(f"🔹 Total    : {total:,.2f} USDT")
            logging.info(divider)
            logging.info(f"🔄 Mirroring trade on Bitget...")
            if label == "USDT-M":
                result = place_bitget_futures_order(symbol, side, quantity, price)
                if result:
                    logging.info(f"✅ Mirrored on Bitget [{label}]")
                else:
                    logging.error(f"❌ Mirror failed on Bitget [{label}]")
            else:
                logging.info(f"⚠️ [COIN-M] Skipped Bitget mirror (not supported)")
            logging.info(divider)
    elif data.get('e') == 'ACCOUNT_UPDATE':
        divider = "💼" + "-" * 30 + "💼"
        logging.info(f"\n{divider}\n[ACCOUNT UPDATE - {label}]")
        acc = data.get('a', {})
        for asset in acc.get('B', []):
            available = float(asset.get('wb', 0))
            if available > 0:
                logging.info(f"  💰 {asset['a']}: {available}")
        for pos in acc.get('P', []):
            if float(pos.get('pa', 0)) != 0:
                logging.info(f"  📊 Position: {pos}")
        logging.info(divider)
    else:
        logging.info(f"[{label}] Other event: {data}")

def start_all_futures_ws():
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    # COIN-M (delivery) futures
    coinm_base_url = "https://dapi.binance.com"
    coinm_ws_base = "wss://dstream.binance.com/ws/"
    coinm_listen_key = get_listen_key(BINANCE_API_KEY, coinm_base_url)
    if coinm_listen_key:
        threading.Thread(target=keepalive_listen_key, args=(BINANCE_API_KEY, coinm_listen_key, coinm_base_url), daemon=True).start()
        def coinm_ws():
            start_futures_ws(coinm_listen_key, coinm_ws_base + coinm_listen_key, "COIN-M")
        threading.Thread(target=coinm_ws, daemon=True).start()
    # USDT-margined futures
    usdtm_base_url = "https://fapi.binance.com"
    usdtm_ws_base = "wss://fstream.binance.com/ws/"
    usdtm_listen_key = get_listen_key(BINANCE_API_KEY, usdtm_base_url)
    if usdtm_listen_key:
        threading.Thread(target=keepalive_listen_key, args=(BINANCE_API_KEY, usdtm_listen_key, usdtm_base_url), daemon=True).start()
        def usdtm_ws():
            start_futures_ws(usdtm_listen_key, usdtm_ws_base + usdtm_listen_key, "USDT-M")
        threading.Thread(target=usdtm_ws, daemon=True).start()

# Patch start_futures_ws to use the new handler
import types
start_futures_ws.__globals__['on_futures_message'] = on_all_futures_message

# --- Main entry point (CLEANED) ---
if __name__ == "__main__":
    threads = []
    if 'TRADING_MODE' not in globals():
        TRADING_MODE = os.getenv("TRADING_MODE", "both").lower()
    if TRADING_MODE in ("spot", "both"):
        # Use standard WebSocket instead of async
        t_spot = threading.Thread(target=start_binance_spot_ws, daemon=True)
        t_spot.start()
        threads.append(t_spot)
    if TRADING_MODE in ("futures", "both"):
        t_futures = threading.Thread(target=start_all_futures_ws, daemon=True)
        t_futures.start()
        threads.append(t_futures)
    print("[Main] Binance to Bitget CopyTrading Bot is running. Press Ctrl+C to exit.")
    print("BINANCE_API_KEY (debug):", BINANCE_API_KEY)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[Main] Exiting...")