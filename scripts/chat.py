"""Talk to Ballast in your terminal, with Claude on Amazon Bedrock as the brain.

    python -m scripts.chat --local          # in-process server + fake broker (no server needed)
    python -m scripts.chat                  # connect to a running server at http://127.0.0.1:8000/mcp
    python -m scripts.chat --trace          # also show every tool call ("under the hood")

Needs AWS credentials (aws configure) with Bedrock access.
"""
import argparse
import asyncio
import json

from agent.agent import BallastAgent
from agent.llm import BedrockLLM


async def confirm_in_terminal(message: str) -> bool:
    print("\n" + "-" * 44 + "\n" + message + "\n" + "-" * 44)
    answer = await asyncio.to_thread(input, "Confirm? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def show_trace(trace):
    for step in trace:
        if step["event"] == "tool_call":
            flag = " (error)" if step["error"] else ""
            print(f"  -> {step['tool']}({json.dumps(step['input'])}){flag}")
            print(f"     {json.dumps(step['output'])[:300]}")
        else:
            print(f"  -> user confirmation: {'YES' if step['answer'] else 'NO'}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--local", action="store_true", help="run the MCP server in-process with the fake broker")
    ap.add_argument("--trace", action="store_true", help="show tool calls")
    args = ap.parse_args()

    if args.local:
        from mcp_server.server import build_server
        server = build_server()
    else:
        server = args.url

    llm = BedrockLLM()
    print(f"Ballast (model: {llm.model_id}). Type 'quit' to exit.\n")
    async with BallastAgent(server, llm, confirm_in_terminal) as agent:
        while True:
            text = (await asyncio.to_thread(input, "You: ")).strip()
            if text.lower() in ("quit", "exit", "q"):
                break
            if not text:
                continue
            reply = await agent.ask(text)
            if args.trace:
                show_trace(reply.trace)
            print(f"Ballast: {reply.text}\n")


if __name__ == "__main__":
    asyncio.run(main())
