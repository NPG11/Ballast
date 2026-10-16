# Ballast

An Alexa+ agent that maintains **your own** portfolio rules in a paper-trading account.
You set your target allocation and safety limits. Ballast watches for drift, calculates the trades
that restore your targets, explains them, and executes only after you explicitly approve.

**Ballast does not tell you what to invest in.** It enforces rules you set. Paper trading only.

**Live demo:** https://ballast-demo.netlify.app
**Video:** (link coming)

## How it works

```
READ -> DRIFT -> PLAN -> APPROVE -> REVALIDATE -> EXECUTE -> VERIFY -> AUDIT
```

| Layer | Folder | Job |
|---|---|---|
| Engine | `engine/` | Pure, deterministic logic: allocation, drift, rebalance plans, guardrails, proposals, rules policy, audit log. No AI, no network. |
| Broker adapters | `adapters/` | `alpaca.py` (Alpaca REST API, paper trading forced), `fake.py` (offline tests), `stored.py` (a demo portfolio per visitor, kept in the store). |
| Storage | `store/` | Rules, proposals, audit log, sessions and confirm cards. DynamoDB when deployed, memory locally. |
| Service | `service/ballast.py` | The full loop the MCP tools call. |
| MCP server | `mcp_server/server.py` | 9 MCP tools over Streamable HTTP. Works with MCP 2025-11-25 and 2026-07-28. |
| Agent | `agent/` | Bedrock agent standing in for Alexa+: sends your words and the MCP tools to Claude, runs the tools it picks, hands confirmations to you. |
| Demo site | `web/` | Simulated Alexa+ experience in the browser: voice in and out, live tool timeline, confirm card. Also serves the MCP server at `/mcp`. |
| Deploy | `infra/` | One-command deploy to AWS Lambda, DynamoDB and Secrets Manager. |

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
pip install -r requirements-dev.txt
python -m pytest -q                 # 107 tests
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

## Talk to it (Bedrock agent)

Needs AWS credentials with Bedrock access (`aws configure`).

```bash
python -m scripts.chat --local --trace     # in-process server + fake broker, shows tool calls
python -m scripts.chat                     # connect to a running MCP server
```

Model: `BALLAST_MODEL` (default `us.anthropic.claude-haiku-4-5-20251001-v1:0`), region: `AWS_REGION`.

## Demo site

```bash
python -m web.server                       # http://127.0.0.1:8080, Claude on Bedrock, demo portfolio
BALLAST_LLM=offline python -m web.server   # no AWS needed: keyword rules instead of Claude
BALLAST_BROKER=alpaca python -m web.server # your Alpaca paper account
```

Voice input needs Chrome or Edge. The horizon tilts with your biggest drift and settles level once
every category is back inside your limit. Trades only run after you answer the confirm card.

## Running on AWS

```
Browser ──HTTPS──▶ Lambda Function URL (response streaming)
                    └─ Lambda "ballast" (Python 3.12 + AWS Lambda Web Adapter)
                        ├─ demo site + Bedrock agent ──▶ Amazon Bedrock (Claude Haiku 4.5)
                        ├─ MCP server at /mcp (Streamable HTTP, bearer token)
                        ├─ DynamoDB "ballast": rules, proposals, audit log, sessions, confirm cards
                        └─ Secrets Manager "ballast/alpaca" ──▶ Alpaca paper trading API
```

The function keeps no state in memory, so any instance can serve any request. Each visitor gets
their own session and, in demo mode, their own demo portfolio. A confirm card can be answered on a
different instance than the conversation waiting for it: the answer goes through DynamoDB with a
conditional write, so only the card's owner can answer it, and only once. Demo data expires after
14 days, and a daily turn cap protects the Bedrock bill.

### Deploy

1. Give your IAM user the permissions in `infra/deployer-policy.json` (scoped to resources named `ballast`).
2. Deploy:

```bash
python -m infra.deploy --region us-east-2                 # demo portfolio for every visitor
ALPACA_KEY=... ALPACA_SECRET=... python -m infra.deploy --region us-east-2 --broker alpaca
python -m infra.deploy --region us-east-2 --destroy       # remove it (add --delete-data for the table and secret)
```

It prints the site URL, the MCP URL and the MCP token. Running it again updates in place.
