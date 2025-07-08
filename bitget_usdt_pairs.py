import ccxt

def get_bitget_futures_usdt_pairs():
    """
    Return a list of all Bitget USDT-margined perpetual futures pairs (symbol format: COIN/USDT:USDT).
    """
    bitget_futures = ccxt.bitget({'options': {'defaultType': 'swap'}})
    futures_markets = bitget_futures.load_markets()
    return [symbol for symbol in futures_markets if symbol.endswith('/USDT:USDT')]

def print_bitget_futures_usdt_pairs():
    pairs = get_bitget_futures_usdt_pairs()
    print("Bitget FUTURES USDT pairs:")
    for symbol in pairs:
        print(symbol)

if __name__ == "__main__":
    print_bitget_futures_usdt_pairs()
