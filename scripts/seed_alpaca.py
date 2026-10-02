"""Build a deliberately drifted demo portfolio in your Alpaca PAPER account.
Run during market hours (6:30am-1pm Pacific). Buys ~$65k VTI and ~$12k BND from the $100k paper balance,
leaving ~$23k cash: roughly 65/12/23 against a 60/20/20 target, so bonds are 8 points under.

    python -m scripts.seed_alpaca
"""
from decimal import Decimal as D

from adapters.alpaca import AlpacaBroker
from engine import Side, Trade

broker = AlpacaBroker()
if not broker.is_market_open():
    raise SystemExit("Market is closed. Run this between 6:30am and 1pm Pacific on a weekday.")
for i, (sym, amt, cat) in enumerate([("VTI", "65000", "us_stocks"), ("BND", "12000", "bonds")]):
    oid = broker.submit_order(Trade(sym, Side.BUY, D(amt), cat), client_order_id=f"seed-{sym}-{i}")
    print(sym, "order", oid)
print("Done. Wait a few seconds, then run: python -m scripts.loop")
