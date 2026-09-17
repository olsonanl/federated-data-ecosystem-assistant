"""Chatbot with an agentic tool-call loop over MCP servers tools."""

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chainlit as cl
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from mcp.client.auth.oauth2 import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthClientProvider,
    OAuthToken,
)
from pydantic import SecretStr

SYSTEM_PROMPT = """You are a bioinformatics assistant with access to several databases.
Always use tools to retrieve real data, never invent accessions or sequences.
For multi-step questions, chain tools: search -> get entry -> get interactions.

Report every answer as a research paper, using these headings in this order.
Adapt each section to a database query. Do not pad a section to fill it.

## Abstract
150-250 words: the problem, the resources and filters used, the key numbers,
and why the result matters.

## I. Introduction
**Background.** What the organism, gene, or pathway is, and why the question
matters.
**Literature Review.** Only records you actually retrieved, such as linked
PubMed entries. If you retrieved none, write "No literature was retrieved for
this query." Never cite a paper you did not fetch with a tool.
**Knowledge Gap.** What was unresolved before the query.
**Objective & Hypothesis.** The question restated as an objective, with the
testable expectation where one applies.

## II. Materials and Methods
**Study Design.** Which resources you selected and why you routed to them.
**Materials.** Each database queried, named with the tool that reached it.
**Procedures.** Every tool call in order with its exact arguments and filters,
in enough detail that a reader could re-run the analysis.
**Statistical Analysis.** How each number was derived: deduplication, the
denominator behind any percentage, and the field the count came from.

## III. Results
**Data Presentation.** A markdown table whenever there is more than one number
to compare. Label it (Table 1, Table 2).
**Findings.** The counts and proportions, stated plainly. No adjectives, no
emphasis, no emotional modifiers.

## IV. Discussion
**Interpretation.** What the numbers mean and whether they meet the objective.
**Context.** How they relate to the records you retrieved.
**Limitations.** The caveats the tools reported in their provenance, plus what
these data cannot establish.
**Conclusion & Future Directions.** The takeaway and the next query worth running.

## References
Number every source [1], [2], ... Give the database name and the exact
provenance URL from the tool result. Copy each URL verbatim: never shorten,
reconstruct, or guess one.

## Acknowledgments
Name the data providers whose records you used.

These rules override the format:
- Never invent a number, accession, citation, or URL. Every figure must trace to
  a tool result in this conversation.
- If a section has no basis in retrieved data, write one line saying so. An
  empty section is correct; an invented one is not.
- Report, do not persuade.
"""

# ---------------------------------------------------------------------------
# OAuth helpers for BV-BRC
# ---------------------------------------------------------------------------

OAUTH_CALLBACK_PORT = 8910
OAUTH_TOKEN_FILE = Path(".bvbrc_oauth_tokens.json")


class _FileTokenStorage:
    """Persist OAuth tokens and client info to a local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


async def _oauth_redirect_handler(url: str) -> None:
    """Open the OAuth authorization URL in the user's default browser."""
    print(f"[oauth] Opening browser for BV-BRC login...")
    webbrowser.open(url)


async def _oauth_callback_handler() -> tuple[str, str | None]:
    """Start a one-shot HTTP server, wait for the OAuth callback, return (code, state)."""
    result: dict[str, str | None] = {}
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            result["code"] = qs.get("code", [None])[0]
            result["state"] = qs.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authentication complete.</h2>"
                b"<p>You can close this tab and return to the chatbot.</p>"
                b"</body></html>"
            )
            loop.call_soon_threadsafe(ready.set)

        def log_message(self, format, *args):  # noqa: A002
            pass  # suppress request logs

    server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), _Handler)

    def _serve():
        server.handle_request()  # serve exactly one request
        server.server_close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    await ready.wait()
    return result.get("code", ""), result.get("state")


def _bvbrc_connection() -> dict:
    """Build the MCP connection config for the BV-BRC server.

    If P3_AUTH_TOKEN is set, use it directly as a bearer token header.
    Otherwise, fall back to the full OAuth browser flow.
    """
    conn: dict = {
        "url": "https://dev-9.bv-brc.org",
        "transport": "streamable_http",
    }
    token = os.environ.get("P3_AUTH_TOKEN")
    if token:
        print("[bv-brc] Using P3_AUTH_TOKEN for authentication")
        conn["headers"] = {"Authorization": f"Bearer {token}"}
    else:
        conn["auth"] = OAuthClientProvider(
            server_url="https://dev-9.bv-brc.org",
            client_metadata=OAuthClientMetadata(
                redirect_uris=[f"http://localhost:{OAUTH_CALLBACK_PORT}/callback"],
                client_name="BV-BRC Chatbot",
            ),
            storage=_FileTokenStorage(OAUTH_TOKEN_FILE),
            redirect_handler=_oauth_redirect_handler,
            callback_handler=_oauth_callback_handler,
        )
    return conn


# Existing MCP servers or local ones running on localhost. The local servers are started by the `mcp_servers` scripts.
MCP_SERVERS = {
        "string": {
            "url": "https://mcp.string-db.org/",
            "transport": "streamable_http",
        },
        "expasy": {
            "url": "https://chat.expasy.org/mcp/",
            "transport": "streamable_http",
        },
        "brc-analytics": {
            "url": "https://brc-analytics.org/api/v1/mcp/",
            "transport": "streamable_http",
        },
        "pdn": {
            "url": "http://127.0.0.1:8001/mcp-pdn",
            "transport": "streamable_http",
        },
        "mygene": {
            "url": "http://127.0.0.1:8002/mcp-mygene",
            "transport": "streamable_http",
        },
        "uniprot": {
            "url": "http://127.0.0.1:8003/mcp-uniprot",
            "transport": "streamable_http",
        },
        "myvariant": {
            "url": "http://127.0.0.1:8004/mcp-myvariant",
            "transport": "streamable_http",
        },
        "ncbi": {
            "url": "http://127.0.0.1:8005/mcp-ncbi",
            "transport": "streamable_http",
        },
        "pubmed": {
            "url": "http://127.0.0.1:8006/mcp-pubmed",
            "transport": "streamable_http",
        },
        "nde": {
            "url": "http://127.0.0.1:8007/mcp-nde",
            "transport": "streamable_http",
        },
        "bv-brc": _bvbrc_connection(),
        "geo": {
            # GEO is the only source registered here with processed gene
            # expression: what genes changed, under what treatment.
            # 8009, not 8007: NDE landed on 8007 first (PR #12).
            "url": "http://127.0.0.1:8009/mcp-geo",
            "transport": "streamable_http",
        },
        "brc_analytics_local": {
            # Complements "brc-analytics" above, which is BRC's own public
            # server. This covers only what that server cannot do: ENA paging
            # past its hard 50-row cap and the real total, a working keyword
            # search (theirs answers HTTP 400), and study lookup (theirs 500s).
            "url": "http://127.0.0.1:8008/mcp-brc-analytics",
            "transport": "streamable_http",
        },
    }

# Overridable from .env so nobody has to commit a model switch. The default is
# unchanged; the commented lines below are the other providers that are wired.
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/google/gemma-4-26b-a4b-it")
# LLM_MODEL="openrouter/mistralai/mistral-small-2603"
# LLM_MODEL="cesnet/qwen3-coder"
# LLM_MODEL="ollama/qwen3.5:9b"
# LLM_MODEL="ollama/gemma4"
# LLM_MODEL="mistralai/mistral-small-latest"
# LLM_MODEL="anthropic/claude-opus-5"
LLM_MODEL="argo/claudesonnet5"   # Argonne Argo gateway; see load_chat_model

# Argo exposes an OpenAI-compatible surface, so it needs no new SDK -- only a
# base_url override. Two things differ from a normal OpenAI-compatible host:
#
#   1. There is no API key. The credential is your ANL username ("ac.jdoe"),
#      passed wherever a key is expected. It is an identifier, not a secret;
#      the Argonne network boundary is what actually restricts access.
#   2. Model names are short internal IDs -- "claudesonnet5", "gpt56sol",
#      "gemini35flash" -- not vendor names. "claude-sonnet-5" is rejected.
#      GET /v1/models lists them (use the `internal_id` field; the display
#      `id` such as "Claude Sonnet 5" also works).
ARGO_BASE_URL = os.environ.get(
    "ARGO_BASE_URL", "https://apps.inside.anl.gov/argoapi/v1"
)


def load_chat_model(model: str) -> BaseChatModel:
    provider, model_name = model.split("/", maxsplit=1)
    if provider == "openrouter":
        return ChatOpenAI(
            model=model_name,
            base_url="https://openrouter.ai/api/v1",
            api_key=SecretStr(os.environ["OPENROUTER_API_KEY"]),
            max_completion_tokens=2048,
        )
    if provider == "argo":
        # Requires being on the Argonne network -- otherwise every call hangs
        # until it times out.
        #
        # WARNING: an unrecognised username does NOT raise. Argo returns
        # HTTP 200 with "ACCESS DENIED" as the assistant's message content, so
        # a typo in ARGO_USER surfaces as the agent talking nonsense rather
        # than as an auth error. Check the first reply if results look strange.
        return ChatOpenAI(
            model=model_name,
            base_url=ARGO_BASE_URL,
            api_key=SecretStr(os.environ["ARGO_USER"]),
            # The Argo shim ignores max_completion_tokens -- which is what LangChain
            # renames max_tokens to -- and honours max_tokens only. Without it the
            # model runs to its maximum output length, and on the Claude models a
            # non-streaming call then trips the upstream ten-minute guard with
            # HTTP 500 "Streaming is required". extra_body bypasses the rename.
            # Measured by laptop_system_improvement, 17 Sep 2026. 4096 rather than
            # 2048 because the reasoning tiers spend part of the cap on reasoning
            # tokens and return empty at 2048.
            extra_body={"max_tokens": 4096},
            # Ask for usage on the final streamed chunk, so token counts are
            # recorded even on the streaming path chatbot.py and the evals use.
            stream_usage=True,
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model_name, temperature=0)
    if provider == "mistralai":
        from langchain_mistralai import ChatMistralAI
        return ChatMistralAI(model_name=model_name, temperature=0, max_tokens=2048)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        # Claude Opus 5 thinks by default and rejects temperature, so leave it unset.
        # Thinking tokens count against max_tokens, so allow more than the other providers.
        return ChatAnthropic(model=model_name, max_tokens=16000)
    raise ValueError(f"Unknown provider: {provider}")


def _root_cause(exc: BaseException) -> str:
    """The innermost real error.

    A failed MCP connection surfaces as an ExceptionGroup wrapping a TaskGroup,
    whose str() is "unhandled errors in a TaskGroup" and says nothing about what
    went wrong. Unwrap it so the startup line names the actual cause.
    """
    seen = 0
    while seen < 10:
        inner = getattr(exc, "exceptions", None)
        if not inner:
            break
        exc = inner[0]
        seen += 1
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def load_tools(servers: dict) -> tuple[list, dict]:
    """Collect tools from every server, skipping the ones that are down.

    MultiServerMCPClient.get_tools() fans out across all servers and raises if
    any one of them is unreachable, which took the whole chat down at startup
    whenever a single local server was not running. Asking each server
    separately costs a missing one its tools and nothing else.
    """
    tools: list = []
    report: dict = {}
    for name, config in servers.items():
        try:
            server_tools = await MultiServerMCPClient({name: config}).get_tools()
        except Exception as exc:  # unreachable, refused, timed out, bad protocol
            report[name] = f"unavailable: {_root_cause(exc)}"
            continue
        tools.extend(server_tools)
        report[name] = f"{len(server_tools)} tools"
    return tools, report


async def init_agent():
    tools, report = await load_tools(MCP_SERVERS)
    for name, status in report.items():
        print(f"  {name:22s} {status}")
    if not tools:
        raise RuntimeError(
            "No MCP server answered. Start them with run_mcp_servers.py, or trim "
            "MCP_SERVERS to the ones you are running."
        )
    llm = load_chat_model(LLM_MODEL)
    return create_agent(model=llm, tools=tools, system_prompt=SYSTEM_PROMPT)


@cl.on_chat_start
async def on_chat_start():
    agent = await init_agent()
    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])


@cl.on_message
async def on_message(message: cl.Message):
    """Handle each user message through the agentic tool loop."""
    agent = cl.user_session.get("agent")
    chat_history: list = cl.user_session.get("chat_history") or []
    if agent is None:
        await cl.Message(content="Agent not initialized.").send()
        return

    chat_history.append(HumanMessage(content=message.content))
    answer_msg = cl.Message(content="")
    pending_tool_calls: dict[str, dict] = {}
    current_tc_id: str | None = None

    async for chunk, __ in agent.astream(
        {"messages": chat_history}, stream_mode="messages"
    ):
        if isinstance(chunk, AIMessageChunk):
            # Anthropic streams content as a list of blocks; .text keeps only the text.
            if chunk.text:
                await answer_msg.stream_token(chunk.text)
            for tc_chunk in getattr(chunk, "tool_call_chunks", []) or []:
                tc_id = tc_chunk.get("id")
                if tc_id:
                    current_tc_id = tc_id
                    pending_tool_calls[tc_id] = {
                        "name": tc_chunk.get("name", "tool"),
                        "args": tc_chunk.get("args", "") or "",
                    }
                elif current_tc_id:
                    pending_tool_calls[current_tc_id]["args"] += tc_chunk.get("args", "") or ""
        elif isinstance(chunk, ToolMessage):
            tc_id = getattr(chunk, "tool_call_id", "")
            info = pending_tool_calls.get(tc_id, {})
            async with cl.Step(name=f"🛠 {info.get('name', 'tool')}") as s:
                s.input = info.get("args", "")
                s.output = str(chunk.content)[:800]
            answer_msg = cl.Message(content="")

    final_answer = answer_msg.content
    if final_answer:
        chat_history.append(AIMessage(content=final_answer))
    cl.user_session.set("chat_history", chat_history[-20:])
    await answer_msg.send()


@cl.set_starters
async def set_starters(user: cl.User | None = None, language: str | None = None):
    return [
        cl.Starter(
            label="UniProt disease variants BRCA1",
            message="What is the function of human BRCA1 and which diseases is it linked to?",
        ),
        cl.Starter(
            label="Rhea reactions ATP hydrolysis",
            message="Find reactions involving ATP hydrolysis in Rhea",
        ),
        cl.Starter(
            label="STRING interactions TP53",
            message="What are the interaction partners of TP53 with high confidence?",
        ),
        cl.Starter(
            label="GEO expression under ciprofloxacin",
            message=(
                "Which E. coli gene expression studies involve ciprofloxacin, and "
                "where are the actual expression values for the top one?"
            ),
        ),
        cl.Starter(
            label="BRC what can I run on E. coli",
            message=(
                "Which genome assemblies does BRC Analytics hold for Escherichia "
                "coli, and which analysis workflows can I run on them?"
            ),
        ),
        cl.Starter(
            label="Expression study to runnable workflow",
            message=(
                "Find an E. coli antibiotic resistance expression study in GEO, "
                "then tell me whether AMR Gene Detection can run on the E. coli "
                "reference genome."
            ),
        ),
    ]
