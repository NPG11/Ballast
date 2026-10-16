# Friction log

Problems I hit while building Ballast for the Amazon Developer Hackathon, with what I did about each one.

Severity: **High** = blocked the planned approach, **Medium** = cost real time but had a workaround, **Low** = minor.

---

## 1. No way to test against Alexa+ itself

- **Severity:** Medium
- **Task:** Build an MCP server for the Alexa+ track and see how Alexa+ would use it.
- **Steps:** Read the track rules and the linked docs to find out how to connect my MCP server to Alexa+.
- **Expected:** An Alexa+ guide or test tool where I could point Alexa+ at my MCP server URL and try it by voice.
- **Actual:** The track points to the general MCP docs only. There's no Alexa+-specific guide or simulator, so I couldn't see how Alexa+ would pick my tools, read results aloud, or handle a confirmation question.
- **Workaround:** I took the track's simulation option: a web app with voice in and out, using Claude on Amazon Bedrock as the agent. It connects to the same MCP server over Streamable HTTP, so the server would work the same with Alexa+.
- **Suggestion:** Publish a short "connect your MCP server to Alexa+" guide and a simulator that accepts a remote MCP server URL, so builders can test the real voice experience.

## 2. AWS Free plan region limit

- **Severity:** Medium
- **Task:** Deploy Lambda, DynamoDB and Secrets Manager.
- **Steps:** Signed up for the AWS Free plan and opened the console to deploy.
- **Expected:** To pick any region, or to be told clearly which one I could use.
- **Actual:** The account only worked in us-east-2. I found that out from the console rather than from anything shown up front.
- **Workaround:** Deployed everything to us-east-2 and made the region a flag in my deploy script.
- **Suggestion:** Show the allowed region at signup and in the console header, and name it in any "not available" errors.

## 3. Streaming responses from Lambda for an MCP server

- **Severity:** Medium
- **Task:** Host an MCP server (Streamable HTTP) and a chat site on Lambda behind a Function URL.
- **Steps:** Wrote a Python web app and tried to serve it from Lambda.
- **Expected:** A Python Lambda could stream responses, since MCP Streamable HTTP uses streamed responses.
- **Actual:** The Python runtime doesn't stream responses directly. I needed the Lambda Web Adapter layer, the `AWS_LWA_INVOKE_MODE=response_stream` setting, a `run.sh` handler, and a Function URL set to `RESPONSE_STREAM`. I put that together from several separate docs.
- **Workaround:** Used the Lambda Web Adapter layer exactly as above. It works well once set up.
- **Suggestion:** Publish an official "MCP server on Lambda" template for Python, or support response streaming in the Python runtime directly.

## 4. Asking the user to confirm a trade (MCP elicitation)

- **Severity:** Medium
- **Task:** Make the MCP server ask the user to approve each trade, so the AI can't approve on its own.
- **Steps:** Used MCP elicitation in the MCP Python SDK.
- **Expected:** The voice assistant would show or speak the server's question to the user and send the answer back.
- **Actual:** The server side works well. But I couldn't find out whether Alexa+ shows elicitation requests to users, so I had to build the confirmation screen myself.
- **Workaround:** The demo site shows a confirm card that you answer by tapping or by saying "yes" or "no". The answer goes straight back to the server.
- **Suggestion:** Document whether Alexa+ supports MCP elicitation and how it presents those requests to users, by voice or on screen.
