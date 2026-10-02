# Ballast

An Alexa+ agent that maintains **your own** portfolio rules in a paper-trading account.
You set your target allocation and safety limits. Ballast watches for drift, calculates the trades
that restore your targets, explains them, and executes only after you explicitly approve.

**Ballast does not tell you what to invest in.** It enforces rules you set. Paper trading only.

## How it works

```
READ -> DRIFT -> PLAN -> APPROVE -> REVALIDATE -> EXECUTE -> VERIFY -> AUDIT
```

| Layer | Folder | Job |
|---|---|---|
| Engine | `engine/` | Pure, deterministic logic: allocation, drift, rebalance plans, guardrails, proposals, rules policy, audit log. No AI, no network. |
| Broker adapters | `adapters/` | `alpaca.py` (paper trading, forced) and `fake.py` (offline tests). |
| Service | `service/ballast.py` | The full loop the MCP tools call. |
| MCP server | `mcp_server/` | *(next)* |
| Agent | `agent/` | *(next)* Bedrock agent standing in for Alexa+. |
| Demo site | `web/` | *(next)* |

### What the AI can and cannot do

**Can:** understand requests, choose MCP tools, explain engine results, keep conversational context.

**Cannot:** choose investments, invent target allocations, calculate trades, override guardrails,
place an order directly, or trade without an approved proposal.

### Safety model
- Targets can be changed by voice. **Safety limits can only be changed on the web app.**
- Every proposal records the portfolio and rules versions. If either changes, Ballast refuses to execute it.
- Proposals expire after 5 minutes. Guardrails run at planning time and again right before execution.
- Sells fill before buys are placed. Retries reuse the same order ID, so orders are never duplicated.
- Every proposal, approval, refusal and fill is written to the audit log.

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q                 # 40 tests
python -m scripts.loop --fake       # full loop offline

cp .env.example .env                # add your Alpaca PAPER keys, then export them
python -m scripts.seed_alpaca       # market hours only: builds a drifted demo portfolio
python -m scripts.loop              # full loop against your paper account
```
