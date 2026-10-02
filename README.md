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
| MCP server | `mcp_server/server.py` | 9 MCP tools over Streamable HTTP. Works with MCP 2025-11-25 and 2026-07-28. |
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
- **The AI cannot approve anything.** Executing a rebalance and saving rules ask the *user*
  directly through MCP elicitation before the tool runs. The approval isn't a tool argument,
  so the model can't fill it in. A client that can't ask the user can't trade.

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q                 # 53 tests
python -m scripts.loop --fake       # full loop offline

cp .env.example .env                # add your Alpaca PAPER keys, then export them
python -m scripts.seed_alpaca       # market hours only: builds a drifted demo portfolio
python -m scripts.loop              # full loop against your paper account
```

## MCP server

```bash
python -m mcp_server.server                       # fake broker, http://127.0.0.1:8000/mcp
BALLAST_BROKER=alpaca python -m mcp_server.server # your Alpaca paper account
```

| Tool | What it does | Writes? |
|---|---|---|
| `get_portfolio` | Value, split by category, drift vs targets | no |
| `get_rules` | Targets, drift limit, safety limits | no |
| `list_categories` | Categories and default funds | no |
| `plan_rebalance` | Builds a proposal. Places no trades | proposal only |
| `execute_rebalance` | Asks the user, revalidates, places paper orders, verifies | **yes, user-confirmed** |
| `get_order_status` | Fill status for a proposal | no |
| `get_activity` | Audit log | no |
| `preview_target_allocation` | Checks new targets without saving | no |
| `save_target_allocation` | Asks the user, then saves new targets (safety limits unchanged) | **yes, user-confirmed** |

Test it visually with MCP Inspector (needs Node.js): `npx @modelcontextprotocol/inspector`,
then connect to `http://127.0.0.1:8000/mcp` with transport "Streamable HTTP".
