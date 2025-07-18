import ccxt
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from dotenv import load_dotenv
import os
load_dotenv()

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

def place_bitget_order(bitget, symbol, order_type, side, amount, price, leverage, margin_mode, trade_side):
    """
    Place an order on Bitget using ccxt.
    Parameters:
        bitget: ccxt.bitget instance
        symbol: str, e.g. 'BTC/USDT:USDT'
        order_type: 'market' or 'limit'
        side: 'buy' or 'sell'
        amount: float
        price: float or None (required for limit orders)
        leverage: int
        margin_mode: 'isolated' or 'cross'
        trade_side: 'open' or 'close' or None
    Returns:
        order response dict or None
    """
    amount = float(amount)
    console = Console()
    try:
        bitget.set_leverage(
            leverage=leverage,
            symbol=symbol,
            params={'marginMode': margin_mode}
        )
    except Exception as e:
        console.print(f"[bold red]Failed to set leverage on Bitget because the amount is less than the minimum required $5 USDT.[/bold red]")
        return None

    params = {'marginMode': margin_mode}
    if trade_side:
        params['tradeSide'] = trade_side

    try:
        order = bitget.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            price=price if order_type == 'limit' else None,
            params=params
        )
        # Check for Bitget error in response
        if 'info' in order and isinstance(order['info'], dict) and ('code' in order['info'] and order['info']['code'] != '00000'):
            console.print(f"[bold red]Bitget order error: {order['info'].get('msg', order['info'])}[/bold red]")
            return None
        # Fetch full order details for accurate output
        try:
            order_id = order.get('id') or (order.get('info', {}).get('orderId'))
            if order_id:
                full_order = bitget.fetch_order(order_id, symbol)
                format_bitget_order_output(full_order)
                return full_order
            else:
                format_bitget_order_output(order)
                return order
        except Exception as e:
            console.print(f"[bold yellow]Order placed, but failed to fetch full details: {e}[/bold yellow]")
            format_bitget_order_output(order)
            return order
    except Exception as e:
        console.print(f"[bold red]Bitget order failed: {e}[/bold red]")
        return None

def format_bitget_order_output(order):
    table = Table(show_header=False, box=box.SQUARE, expand=False)
    info = order.get('info', {})
    # Try to get values from both order and info, with fallbacks
    order_id = order.get('id') or info.get('orderId', '-')
    symbol = order.get('symbol') or info.get('symbol', '-')
    side = order.get('side') or info.get('side', '-')
    if side == '-':
        # Bitget sometimes uses 'posSide' for position side
        side = info.get('posSide', '-')
    order_type = order.get('type') or info.get('orderType', '-')
    amount = order.get('amount') or info.get('size', '-') or info.get('amount', '-')
    price = order.get('price') or info.get('price', '-')
    leverage = info.get('leverage', '-')
    margin_mode = info.get('marginMode', '-')
    trade_side = info.get('tradeSide', '-')
    status = order.get('status') or info.get('status', '-')

    table.add_row("[cyan]Order ID", f"[white]{order_id}")
    table.add_row("[cyan]Symbol", f"[white]{symbol}")
    table.add_row("[cyan]Side", f"[white]{side}")
    table.add_row("[cyan]Order Type", f"[white]{order_type}")
    table.add_row("[cyan]Amount", f"[white]{amount}")
    table.add_row("[cyan]Price", f"[white]{price}")
    table.add_row("[cyan]Leverage", f"[white]{leverage}")
    table.add_row("[cyan]Margin Mode", f"[white]{margin_mode}")
    table.add_row("[cyan]Trade Side", f"[white]{trade_side}")
    table.add_row("[cyan]Order Status", f"[white]{status}")
    panel = Panel(table, title=f"[bold]{side} [blue] Bitget [green]{order_type}[/green]", border_style="blue", expand=False)
    console = Console()
    console.print(panel)


# # Example usage
# order = place_bitget_order(
#     bitget=bitget,
#     symbol='XRP/USDT:USDT',
#     order_type='market',
#     side='sell', # sell for short trade or 'buy' for long trade
#     amount=float(4.1),
#     price=None,  # or a float for limit orders
#     leverage=3,
#     margin_mode='isolated',
#     trade_side='open' # 'open' for opening positions, 'close' for closing positions
# )

# print(bitget.load_markets()['XRP/USDT:USDT'])