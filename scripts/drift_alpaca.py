"""Knock your Alpaca PAPER portfolio off balance, so there's something to rebalance in a demo.

Sells some bonds and buys the same amount of stocks: from 60/20/20 with --amount 8000
you end up near 68/12/20, which is past the 5-point limit on both.

    python -m scripts.drift_alpaca                 # move $8,000 from BND to VTI
    python -m scripts.drift_alpaca --amount 10000

Market hours only (6:30am-1pm Pacific). Each run uses new order IDs, so you can run it again.
"""
import argparse
import time
from decimal import Decimal as D

from adapters.alpaca import AlpacaBroker
from adapters.broker import FAILED, FILLED
from engine import Side, Trade


def wait_for(broker, order_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        o = broker.get_order(order_id)
        if o.status in (FILLED, FAILED):
            return o
        time.sleep(1)
    return broker.get_order(order_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=D, default=D("8000"), help="dollars to move from bonds to stocks")
    ap.add_argument("--sell", default="BND")
    ap.add_argument("--buy", default="VTI")
    args = ap.parse_args()

    broker = AlpacaBroker()
    if not broker.is_market_open():
        raise SystemExit("Market is closed. Run this between 6:30am and 1pm Pacific on a weekday.")
    held = {h.symbol: h.market_value for h in broker.get_portfolio().holdings}
    if held.get(args.sell, D("0")) < args.amount:
        raise SystemExit(f"You only hold ${held.get(args.sell, 0):,.2f} of {args.sell}. "
                         f"Run scripts.seed_alpaca first, or use a smaller --amount.")

    tag = time.strftime("%Y%m%d%H%M%S")
    sell = broker.submit_order(Trade(args.sell, Side.SELL, args.amount, "bonds"), f"drift-{tag}-sell")
    o = wait_for(broker, sell)
    print(f"Sold ${o.filled_notional:,.2f} of {args.sell}: {o.status}")
    if o.status != FILLED:
        raise SystemExit("The sell didn't fill, so nothing was bought.")
    buy = broker.submit_order(Trade(args.buy, Side.BUY, args.amount, "us_stocks"), f"drift-{tag}-buy")
    o = wait_for(broker, buy)
    print(f"Bought ${o.filled_notional:,.2f} of {args.buy}: {o.status}")
    print("Done. Your portfolio is now off balance. Ask Ballast: \"How's my portfolio?\"")


if __name__ == "__main__":
    main()
