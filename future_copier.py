import asyncio,websockets,requests,time,hmac,hashlib,json
from rich.console import Console
from dotenv import load_dotenv
from rich.table import Table
from rich.panel import Panel
from rich import box
import ccxt,os
from bitget_order_utils import place_bitget_order
import threading
import traceback

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE")



bitget = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_PASSPHRASE,
    "enableRateLimit": True,
    'options': {'defaultType': 'swap'},
})

# For futures trading links for binance
REST_BASE = "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com/ws/"


console = Console()

################################# Support Functions ###################################

def get_listen_key():
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.post(f"{REST_BASE}/fapi/v1/listenKey", headers=headers)
    resp.raise_for_status()
    return resp.json()["listenKey"]

def get_position_info(symbol, position_side):
    """
    Fetch the current leverage and marginType for a symbol and position side (LONG/SHORT) using the REST API.
    """
    params = {
        "timestamp": int(time.time() * 1000)
    }
    query_string = '&'.join([f"{k}={params[k]}" for k in sorted(params)])
    signature = hmac.new(BINANCE_API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.get(f"{REST_BASE}/fapi/v2/positionRisk", params=params, headers=headers)
    resp.raise_for_status()
    positions = resp.json()
    for p in positions:
        if p["symbol"] == symbol and p["positionSide"] == position_side:
            return f"{p['leverage']}x", p.get('marginType', '-')
    return "-", "-"

def format_order_update(data):
    o = data["o"]
    table = Table(show_header=False, box=box.SQUARE, expand=False)
    table.add_row("[cyan]Timestamp", f"[white]{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['T']/1000))}")
    table.add_row("[cyan]Symbol", f"[white]{o['s']}")
    table.add_row("[cyan]Side", f"[white]{o['S']}" )
    # Always show the position side as reported by Binance (LONG/SHORT/BOTH)
    table.add_row("[cyan]Position Side", f"[white]{o.get('ps', '-')}")
    table.add_row("[cyan]Quantity", f"[white]{o['q']}")
    table.add_row("[cyan]Price", f"[white]{o['ap']} USDT")
    table.add_row("[cyan]Total Value", f"[white]{float(o['ap'])*float(o['q']):.2f} USDT")
    table.add_row("[cyan]Trade ID", f"[white]{o['t']}" )
    table.add_row("[cyan]Order Status", f"[white]{o['X']}" )
    table.add_row("[cyan]Order Type", f"[white]{o['o']}" )
    if 'ps' in o:
        leverage, margin_type = get_position_info(o['s'], o['ps'])
    else:
        leverage, margin_type = "-", "-"
    table.add_row("[cyan]Leverage", f"[white]{leverage}")
    table.add_row("[cyan]Margin Type", f"[white]{margin_type}")
    table.add_row("[cyan]Position Amt", f"[white]{o['z']}")
    table.add_row("[cyan]Reduce Only", f"[white]{o['R']}")
    table.add_row("[cyan]Direction", f"[white]{'OPEN' if o['X']=='FILLED' and float(o['z'])!=0 else 'CLOSE'}")    
    return table

def convert_binance_to_bitget_symbol(binance_symbol):
    if binance_symbol.endswith("USDT"):
        base = binance_symbol[:-4]
        return f"{base}/USDT:USDT"
    else:
        raise ValueError(f"Unsupported symbol format: {binance_symbol}")


################################## Main Function #######################################

def keepalive_listen_key(listen_key, stop_event):
    """
    Sends a keepalive request to Binance every 20 minutes to prevent listen key expiration.
    Stops if stop_event is set.
    """
    while not stop_event.is_set():
        try:
            headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
            requests.put(f"{REST_BASE}/fapi/v1/listenKey", headers=headers, params={"listenKey": listen_key})
        except Exception as e:
            console.print(f"[bold red]Error keeping listen key alive: {e}[/bold red]")
        stop_event.wait(20 * 60)  # 20 minutes

async def user_data_ws(shutdown_event):
    while not shutdown_event.is_set():
        listen_key = get_listen_key()
        ws_url = WS_BASE + listen_key
        stop_event = shutdown_event  # Use the global shutdown_event
        keepalive_thread = threading.Thread(target=keepalive_listen_key, args=(listen_key, stop_event), daemon=True)
        keepalive_thread.start()
        try:
            async with websockets.connect(ws_url) as ws:
                console.print("[bold green]Listening for real-time order updates...[/bold green]")
                while not shutdown_event.is_set():
                    try:
                        msg = await ws.recv()
                        if shutdown_event.is_set():
                            break
                        data = json.loads(msg)
                        if data.get("e") == "ORDER_TRADE_UPDATE":
                            o = data["o"]
                            if o["X"] == "FILLED":
                                table = format_order_update(data)
                                panel = Panel(table, title=f"[bold]{o['S']} [blue]{o['s']} - [green]{'OPEN' if float(o['z'])!=0 else 'CLOSE'}[/green]", border_style="green", expand=False)
                                console.print(panel)
                                binance_symbol = o["s"]
                                bitget_symbol = convert_binance_to_bitget_symbol(binance_symbol)
                                if o["ps"] == "LONG":
                                    side = "sell" if o["ps"] == "SHORT" else "buy"
                                    direction = "open" if o["S"] == "BUY" else "close"
                                elif o["ps"] == "SHORT":
                                    side = "sell" if o["ps"] == "SHORT" else "buy"
                                    direction = "open" if o["S"] == "SELL" else "close"
                                leverage_, margin_type_ = get_position_info(o['s'], o['ps'])
                                leverage = leverage_.replace('x', '') if isinstance(leverage_, str) else leverage_
                                print(f"Placing Bitget order for {bitget_symbol} with side {side}, amount {o['q']}, leverage {leverage}, margin type {margin_type_}, direction {direction}")
                                place_bitget_order(
                                    bitget=bitget,
                                    symbol=bitget_symbol,
                                    order_type="market",
                                    side=side,
                                    amount=o['q'],
                                    price=None,
                                    leverage=int(leverage),
                                    margin_mode=margin_type_,
                                    trade_side=direction
                                )
                    except websockets.ConnectionClosed as e:
                        if shutdown_event.is_set():
                            break
                        console.print(f"[yellow]WebSocket closed: {e}. Reconnecting in 10 seconds...[/yellow]")
                        break  # Break inner loop to reconnect
                    except Exception as e:
                        if shutdown_event.is_set():
                            break
                        console.print(f"[bold red]Error in message processing: {e}\n{traceback.format_exc()}[/bold red]")
                        await asyncio.sleep(1)
        except Exception as e:
            if shutdown_event.is_set():
                break
            console.print(f"[bold red]WebSocket connection error: {e}\nReconnecting in 10 seconds...[/bold red]")
            await asyncio.sleep(10)
        finally:
            stop_event.set()  # Stop the keepalive thread when reconnecting
            await asyncio.sleep(10)  # Prevent rapid reconnect loops