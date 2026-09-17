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


def _bvbrc_auth() -> OAuthClientProvider:
    """Build an OAuthClientProvider for the BV-BRC MCP server."""
    return OAuthClientProvider(
        server_url="https://dev-9.bv-brc.org",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[f"http://localhost:{OAUTH_CALLBACK_PORT}/callback"],
            client_name="BV-BRC Chatbot",
        ),
        storage=_FileTokenStorage(OAUTH_TOKEN_FILE),
        redirect_handler=_oauth_redirect_handler,
        callback_handler=_oauth_callback_handler,
    )


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
        "bv-brc": {
            "url": "https://dev-9.bv-brc.org",
            "transport": "streamable_http",
            "auth": _bvbrc_auth(),
        },
    }

LLM_MODEL="openrouter/google/gemma-4-26b-a4b-it"
# LLM_MODEL="openrouter/mistralai/mistral-small-2603"
# LLM_MODEL="cesnet/qwen3-coder"
# LLM_MODEL="ollama/gemma4"
# LLM_MODEL="mistralai/mistral-small-latest"
# LLM_MODEL="anthropic/claude-opus-5"
# LLM_MODEL="argo/claudesonnet5"   # Argonne Argo gateway; see load_chat_model

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
            max_completion_tokens=2048,
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


async def init_agent():
    # Connect to each MCP server individually so that one failure (e.g. a 401
    # from an OAuth-protected server) doesn't take down all the others.
    all_tools = []
    for name in MCP_SERVERS:
        try:
            client = MultiServerMCPClient({name: MCP_SERVERS[name]})
            tools = await client.get_tools()
            all_tools.extend(tools)
        except Exception as exc:
            print(f"[warn] MCP server '{name}' unavailable, skipping: {exc}")
    llm = load_chat_model(LLM_MODEL)
    return create_agent(model=llm, tools=all_tools, system_prompt=SYSTEM_PROMPT)


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
    ]
