"""Run the full Ballast loop in the terminal.

    python -m scripts.loop --fake     # offline, with a fake broker
    python -m scripts.loop            # your Alpaca paper account (needs ALPACA_KEY / ALPACA_SECRET)
"""
import argparse
import json
from decimal import Decimal as D

from engine import Rules
from service.ballast import Ballast

RULES = Rules(targets={"us_stocks": D("0.60"), "bonds": D("0.20"), "cash": D("0.20")})


def show(title, data):
    print(f"\n== {title} ==\n{json.dumps(data, indent=2, default=str)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="use the offline fake broker")
    args = ap.parse_args()

    if args.fake:
        from adapters.fake import FakeBroker
        broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")
    else:
        from adapters.alpaca import AlpacaBroker
        broker = AlpacaBroker()

    app = Ballast(broker)
    app.store.save_rules("neel", RULES)

    show("READ + DRIFT", app.portfolio_status("neel"))
    plan = app.plan_rebalance("neel")
    show("PLAN", plan)
    if plan["status"] != "awaiting_approval":
        return

    if input("\nApprove this proposal? [y/N] ").strip().lower() != "y":
        show("REJECT", app.reject("neel", plan["proposal_id"]))
        return
    show("APPROVE", app.approve("neel", plan["proposal_id"]))
    show("EXECUTE + VERIFY", app.execute("neel", plan["proposal_id"]))
    show("AUDIT LOG", app.history("neel"))


if __name__ == "__main__":
    main()
