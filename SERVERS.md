# Running the MCP servers

Everything `chatbot.py` federates, how to start it, and which port it owns.
Last checked 17 Sep 2026 against `main`.

## Start everything

```
uv run run_mcp_servers.py          # starts every local server
uv run chainlit run chatbot.py     # in a second terminal
```

## Port allocation

Ports are not interchangeable: `chatbot.py`'s `MCP_SERVERS` hard-codes each URL.

| port | server | owner |
|---|---|---|
| 8001 | `mcp_servers/pdn.py` | existing |
| 8002 | `mcp_servers/mygene.py` | existing |
| 8003 | `mcp_servers/uniprot.py` | existing |
| 8004 | `mcp_servers/myvariant.py` | existing |
| 8005 | `mcp_servers/ncbi.py` (+ `ncbi_lib/`) | Jonathan Gunti |
| 8006 | `mcp_servers/pubmed.py` | Everaldo |
| 8007 | NDE (`NIAID-Data-Ecosystem/`) | Robert Olson |
| 8009 | `mcp_servers/geo.py` | Bobby |
| 8008 | `mcp_servers/brc_analytics.py` | Bobby |
| — | `brc-analytics`, federated remote | Galaxy Project, nothing to start |
| — | `string`, `expasy` | public remotes, nothing to start |

Ports are claimed by landing on `main`, not by intent. This table has already had
two collisions in one day — the NCBI server and mygene both defaulted to 8002, and
`pubmed` and the BRC complement both wanted 8006. Check the table before picking one,
and push early so your claim is visible.

A missing server no longer takes the chat down. `init_agent()` asks each server
separately and prints a line per server at startup, so one that is not running
costs you its tools and nothing else. It refuses to start only if nothing answered.

## The two BRC entries, and why there are two

`brc-analytics` is BRC's own public MCP server, federated as-is (registered on main by PR #7) — 12 read-only tools,
no auth, not reimplemented here.

`brc_analytics_local` is `mcp_servers/brc_analytics.py`, and it adds only what that
server cannot do. Each gap was reproduced live on 17 Sep:

| our tool | closes |
|---|---|
| `brc_ena_runs` | their `search_ena` stops at 50 rows and never reports a total |
| `brc_ena_search` | their `search_ena_keywords` sends an unquoted value to ENA and gets HTTP 400 back — returned as tool *text*, so it can read as an answer |
| `brc_ena_study` | their `/api/v1/ena/study/{accession}` returns HTTP 500 |
| `brc_federation_status` | telling "BRC is down" apart from "BRC returned a bug" |

### The trailing slash is load-bearing

```python
"url": "https://brc-analytics.org/api/v1/mcp/",
```

Without it the server answers **307 to a plain `http://` URL**. The MCP client sends
with `follow_redirects=False` and re-implements a narrow same-origin follow that
accepts a trailing-slash normalisation and an `http`→`https` upgrade, but **not** an
`https`→`http` downgrade. So the redirect is refused and the call surfaces as an HTTP
error. Nothing in `chatbot.py`, `langchain-mcp-adapters` or the SDK adds the slash back.

## GEO, and why it is its own server

`mcp_servers/geo.py` covers the `gds` Entrez database. It is separate from `ncbi.py`
because GEO is the only source on this board with **processed gene expression**, and
because `gds` has traps none of the other Entrez databases have:

- **A GEO accession is not an Entrez UID.** The mapping is arithmetic: a type digit
  (`GPL`=1, `GSE`=2, `GSM`=3) plus the accession number padded to 8 digits — and **GDS
  takes no digit at all**. Getting it wrong does not error. Verified: gds UID
  `100005163` is a real record, `GPL5163`, so treating `GDS5163` as a platform hands
  you a microarray design labelled as a curated dataset. Every tool here checks that
  the record returned carries the accession that was asked for.
- **Searching for the accession is worse.** `gds` indexes the accession as free text
  inside every related record: `GSE309890` matches 8 records, `GPL24659` matches 491,
  and for a GSM accession the *first* hit is its Series. Use `geo_resolve_accession`,
  which is exact and costs no request.
- **The numbers are not in the API.** `esummary` returns metadata only. The processed
  values exist solely as files in the GEO FTP tree, a different host with no API, so
  `geo_series` lists that directory and returns real download URLs.

## The house pattern for a new server

1. One flat file, `mcp_servers/<resource>.py`. Shared internals go in a sibling
   package, as `ncbi.py` does with `ncbi_lib/`.
2. `import requests` and `from mcp.server.fastmcp import FastMCP` — the FastMCP that
   ships **inside the official SDK**. The standalone `fastmcp` package on PyPI is a
   different project.
3. `from mcp.server.fastmcp.exceptions import ToolError` for validation and API errors.
4. `FastMCP(name=..., dependencies=["mcp", "requests"], instructions=..., port=<port>,
   streamable_http_path="/mcp-<resource>")`.
5. Synchronous `@mcp.tool()` functions returning `dict`.
6. Docstrings in four sections — summary, prose, `Args:`, `Returns:`, `Example
   questions:`. The example questions are what a model matches against, so write them
   the way a biologist would ask.
7. Clamp limits rather than raising on them; return a flat dict carrying an `api_call`
   block so provenance survives into the answer.
8. The `__main__` argparse block from `uniprot.py`: `--stdio` and `--port`.
9. Register it in `chatbot.py`'s `MCP_SERVERS`, add it to `run_mcp_servers.py`, and
   add its row to the table above.

Tool names must be **globally unique across all servers** — `chatbot.py` does not set
`tool_name_prefix`, so two servers exposing the same tool name collide silently.

## Environment

`chatbot.py` reads exactly one variable directly, `OPENROUTER_API_KEY`, and only on the
openrouter branch of `load_chat_model()`. Other providers' keys are read by the
LangChain classes themselves — `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`.

`chatbot.py` never calls `load_dotenv()`. **Chainlit does it on import, from the current
working directory**, so `.env` must sit wherever you launch `chainlit run`.

Copy `.env.example` to `.env` and fill in the one key matching your `LLM_MODEL`.

No MCP server in this repo needs a key. NCBI allows 3 requests/second per IP without
one, and that ceiling is shared across every NCBI host — `geo.py` paces itself at 0.4s
between calls for that reason, including its FTP listings.
