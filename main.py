import os,sys,time,json,logging,traceback,threading,requests,ccxt,websocket,asyncio
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from future_copier import user_data_ws

# Workaround for asyncio/aiodns compatibility on Windows
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load environment variables from .env file
load_dotenv()

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
    'sandboxMode': os.getenv("USE_DEMO", "0") == "1",
    "enableRateLimit": True,
})
bitget_markets = bitget.load_markets()

bitget.set_sandbox_mode(os.getenv("USE_DEMO", "0") == "1")
SYMBOL_MAP = {symbol.replace('/', ''): symbol for symbol in bitget_markets if symbol.endswith('/USDT')}

# Bitget spot client
bitget_spot = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_PASSPHRASE,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
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

def handle_pretty_message(msg, market_type="spot"):
    event_type = msg.get("e")
    console = Console()
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
                # Build rich table for pretty output
                table = Table(show_header=False, box=box.SQUARE, expand=False)
                table.add_row("[cyan]Symbol", f"[white]{symbol}")
                table.add_row("[cyan]Side", f"[white]{side}")
                table.add_row("[cyan]Quantity", f"[white]{quantity}")
                table.add_row("[cyan]Price", f"[white]{price:,.4f} USDT")
                table.add_row("[cyan]Total Value", f"[white]{float(quantity) * float(price):,.2f} USDT")
                table.add_row("[cyan]Trade ID", f"[white]{trade_id}")
                table.add_row("[cyan]Order Status", f"[white]{status}")
                panel = Panel(table, title=f"[bold]{side} [blue]{symbol} [green]SPOT[/green]", border_style="green", expand=False)
                console.print(panel)
                logging.info(f"🔄 Mirroring trade on Bitget...")
                result = place_bitget_order(symbol, side, quantity, price)
                if result:
                    logging.info(f"✅ Mirrored on Bitget [SPOT]")
                else:
                    logging.error(f"❌ Mirror failed on Bitget [SPOT]")
    elif event_type == "outboundAccountPosition":
        balances = msg.get("B", [])
        table = Table(show_header=True, box=box.SQUARE, expand=False)
        table.add_column("Asset")
        table.add_column("Available")
        for asset in balances:
            available = float(asset['f'])
            if available > 0:
                table.add_row(asset['a'], f"{available}")
        if table.row_count > 0:
            panel = Panel(table, title="[bold]Account Update - SPOT[/bold]", border_style="blue", expand=False)
            console.print(panel)

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
        print(f"[{label}] WebSocket error: {error}")
    def _on_close(ws, close_status_code, close_msg):
        print(f"[{label}] WebSocket closed: {close_status_code} {close_msg}")
    def _on_open(ws):
        print(f"[{label}] WebSocket connection opened. Listening for real-time {label} trade/account events...")
    
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

# --- Main entry point (SPOT ONLY) ---
if __name__ == "__main__":
    print("[Main] Binance to Bitget CopyTrading New Bot (SPOT and FUTURE) is running. Press Ctrl+C to exit.")
    t_spot = threading.Thread(target=start_binance_spot_ws, daemon=True)
    t_spot.start()
    asyncio.run(user_data_ws())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[Main] Exiting...")