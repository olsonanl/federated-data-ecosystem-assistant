# Contributing

This is a three-day codeathon repo, so the bar is "another person can pick this up and
know what to do" rather than "perfect". Everything below takes minutes, not hours.

## Get it running

```bash
uv sync                          # installs everything, including dev deps
uv run pytest -q                 # 566 offline tests, no network, ~5s
uv run run_mcp_servers.py        # starts every local server
uv run chainlit run chatbot.py   # the chatbot, in a second terminal
```

The chatbot needs one LLM API key in `.env` — copy `.env.example` and fill in the one
matching `LLM_MODEL` in `chatbot.py`. **None of the MCP servers needs a key**; every source
they reach is a public read.

Ports, what runs where, and the traps that bite are in [`SERVERS.md`](SERVERS.md).

## The three rules that matter here

**1. A tool's job is to be right, not to be thin.**
Every source on this board is public and documented. A wrapper that just forwards the call
adds nothing — the person could have read the docs. What earns its place is the guard
against the call that returns **HTTP 200 and a plausible answer about the wrong thing**.
There are ten measured examples in [`evals/README.md`](evals/README.md); the baseline gets
0 of 10 right.

**2. An empty result is never "no data" until you have ruled out "I asked wrong".**
A wrong Pathogen Detection group name returns `0`. The wrong NDE field returns `1` instead
of `60,107`. A missing GEO FTP directory means the record registered no files, which is an
answer, not a failure. Say which one it is, in the return value, in words a model will
repeat.

**3. Say what you did not test.**
"Done" means you ran it and read the output. The full chatbot has never been run end to
end on the machine these servers were built on, and [`evals/README.md`](evals/README.md)
says so rather than letting "it works" imply more than it should.

## Adding an MCP server

Follow `mcp_servers/uniprot.py`; the full pattern is in
[`SERVERS.md`](SERVERS.md#the-house-pattern-for-a-new-server). In short: one flat file,
`requests`, the SDK's FastMCP (**not** the standalone `fastmcp` package), sync functions
returning `dict`, docstrings with Args / Returns / **Example questions**, and the
`__main__` argparse block.

Then three registrations, all of which are easy to forget:

1. `chatbot.py` → `MCP_SERVERS`
2. `run_mcp_servers.py` → `SERVERS`
3. `SERVERS.md` → the port table

**Claim your port by landing on `main`, not by intending to.** Two collisions happened in a
single day — the NCBI server and mygene both defaulted to 8002, and pubmed and the BRC
complement both wanted 8006. Check the table, then push early.

Tool names must be **globally unique across every server**. `chatbot.py` does not set
`tool_name_prefix`, so two servers exposing the same tool name collide silently.

## Adding tests

`tests/conftest.py` has a recorder per transport shape and an autouse `block_network`
fixture that fails any request a test did not arrange. If your server talks to the network
in a new way, add a recorder and add your module to `block_network` — otherwise your tests
will hit the real API and look like they pass.

```python
def test_a_wrong_group_name_is_not_read_as_absence(myserver, my_record):
    my_record({"count": 0})
    out = myserver.my_tool(group="Escherichia coli")
    assert "no matching group" in out["note"]      # not just `count == 0`
```

Write one test per trap, and name it after the wrong behaviour it prevents rather than the
function it calls. A test called `test_search_works` tells the next person nothing.

Anything that touches the network is marked `@pytest.mark.live` and is deselected by
default — NCBI allows 3 requests/second **per IP across all its hosts**, and that budget is
shared with everyone else on your network.

## Adding a pipeline or an eval case

[`evals/PIPELINES.md`](evals/PIPELINES.md) is the map: eight pipelines, each with the
researcher's question, the tool chain, a ground truth, and what someone would have done
without it. **Every benchmark should trace back to a row there.** A pipeline with no ground
truth is labelled unverified and should not appear in a demo.

To add a case to the harness, write one function in `evals/run_eval.py` returning
`(id, question, truth, baseline, tool, why)`:

- **baseline** is the call a careful developer would actually write from the API docs. Write
  it to be fair. It is the whole comparison, and it is the part a reviewer should be able
  to argue with.
- **tool** is ours.
- Both return `(value, one_line_detail)` and are scored against `truth`.

```bash
uv run evals/run_eval.py --markdown evals/REPORT.md
```

A case where both pass is still worth keeping — it gets reported as convenience rather than
correctness, which is the honest label.

## Verification plan — what is still open

| | Pipeline | State | Next step |
|---|---|---|---|
| P1 | Discovery (NDE) | **blocked** | NDE is stdio-only and not in `MCP_SERVERS`. It needs an HTTP transport. Biggest hole in the system; currently unowned. |
| P2 | ID crosswalk | verified | add offline tests |
| P3 | Expression (GEO) | verified, 52 tests | more eval cases beyond the four measured |
| P4 | AMR genotype | verified | `AST_phenotypes` is unreachable — `_pathogen_filter` builds from five fields and that is not one. Phenotype questions stay unanswerable until it is added. |
| P5 | Compute planning | verified, 26 tests | Galaxy execution is deliberately out: it needs a key and a human yes, and read and execute stay in separate servers |
| P6 | Literature | spot-checked | its owner should set a suite |
| P7 | Protein | spot-checked | structure is the weakest area on the board — no folding workflows exist |
| P8 | Gap | one case | the other four board gaps are specified but unmeasured |

## What a good PR looks like here

- Says what you **observed**, with the real output pasted, not what you expect to be true.
- Says what you did **not** test.
- Touches only files you own. If something in someone else's server is wrong, **report it,
  do not patch it** — they have context you do not. Two defects were found this way and
  both went back to their authors as notes.
- Names the trap it guards against, so the next person knows why the code is not simpler.
