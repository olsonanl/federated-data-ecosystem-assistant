# Why wrap an API that is already documented?

Every source behind this chatbot has public documentation and an open endpoint. A
competent developer can read the docs and write the call. So the question a reviewer
should ask about any MCP server here is not "does it reach the API" — anything reaches
the API — but **what does it add that reading the documentation does not?**

This directory answers that with measurements rather than assertion.

## The claim

The value is not access. It is that the obvious call, written the way the
documentation leads you to write it, returns **HTTP 200 and a plausible answer about
the wrong thing** — and that a wrapper carrying the source's particularities returns
the right one.

That is a falsifiable claim, so `run_eval.py` tries to falsify it.

## How it is measured

```
uv run evals/run_eval.py --markdown evals/REPORT.md
```

Fourteen cases, each run twice against the live services:

- **baseline** — the obvious call, written from the API docs, with no guards
- **tool** — our MCP tool

Both are scored against a ground truth taken from a verified observation. A case counts
as value added only when the baseline fails and the tool passes. Cases where both pass
are reported too, and honestly: those are convenience, not correctness.

### Current result

| | correct |
|---|---|
| baseline (raw API, written from the docs) | **1/14** |
| our MCP tools | **13/14** |

Full detail, with the real output of every call, is in [`REPORT.md`](REPORT.md).

Seven of the ten baseline failures return **HTTP 200 with a plausible wrong answer**,
which is the case worth wrapping. The other three fail differently and are listed as such:
one is an HTTP 500 upstream, one returns a page size that reads as a total, and one has no
baseline call at all because without the counts the only option is to decline.

| the obvious call | what it returns | what is true |
|---|---|---|
| `esummary db=gds id=100005163` for GDS5163 | `GPL5163`, an Affymetrix array design | GDS takes no type digit; the UID is `5163` |
| `esearch db=gds term=GSM9284462`, take hit 1 | `200309890`, the Series | the Sample is `309284462` |
| `esearch` E. coli + ciprofloxacin, no filter | 513 | 37 Series — the rest are Samples and Platforms |
| `esummary` → `suppfile` | `'CSV'` | a format, never a filename or URL |
| `esearch` with default `retmax` | 20 ids, `count: 1929` | silently truncated, nothing says so |
| BRC `search_ena_keywords` | an ENA 400, returned as tool *text* | quoting the value returns 200 |
| BRC `search_ena` | `50`, `has_more: true` | 551,679 runs exist |
| BRC `/api/v1/ena/study/{acc}` | HTTP 500 | 369 runs across 13 organisms |
| Pathogen Detection `taxgroup_name=="Escherichia coli"` | `0` | the curated group is `"E.coli and Shigella"`, and it holds 581,464 |
| "how many methicillin-resistant E. coli" | nothing to say but "I don't know" | `mecA` = **2** of 581,464, vs 93,260 of 171,412 in *S. aureus* |
| `esummary db=bioproject id=PRJNA715470` | HTTP 200, `Invalid uid` inside the body | resolve the accession to UID 715470 first; then `submitter_organization` = University of Pennsylvania |
| taxonomy `"Escherichia coli K-12 MG1655"` | 0 hits; loosened to `"K-12"` gives 83333 | the reference assembly is taxid **511145**, a different strain |
| PubMed `sort="Publication Date"`, no `datetype` | 274, not newest-first | `datetype=pdat&sort=pub_date` gives 283, newest-first. Omitting `datetype` silently windows on the index date instead |
| **UniProt `GET /uniprotkb/P0AES4`** | **20 PDB cross-references** | **the baseline is right and our tool is wrong**: `uniprot_get_entry` trims them because `ENTRY_FIELDS` never names `xref_pdb` |

A model given only the API documentation writes the left column. That is the baseline
this project is worth measuring against.

**The one baseline pass is a gap in our own code, and it is reported rather than
excluded.** The board's "structural implications" branch needs PDB cross-references; UniProt
returns them by default; `uniprot_get_entry` drops them. That is a one-word fix in a file
this branch does not own, so it goes to that file's author as a finding. A harness that can
only find fault in other people's APIs is not a harness.

## What the eval found in our own code

The first run of the harness put two independent 0.4s pacers on NCBI in one process,
crossed the 3/second per-IP ceiling, and got HTTP 429 — which `geo.py` turned into a
failed tool call. **Reporting a throttle as an absence of data is exactly the failure
class this project exists to prevent**, so it was fixed in the server rather than worked
around in the test: both the E-utilities and FTP paths now back off and retry on 429 and
5xx and honour `Retry-After`.

That is the loop working. The harness is meant to keep finding these.

## Demo questions

[`QUESTIONS.md`](QUESTIONS.md) holds 15 questions for the demo, ordered single-source
first, each with its tool chain, its expected answer, and the part that cannot be
answered. Six need more than one server; three cannot be answered at all, and say why.

## Honest limits

- **The full chatbot has not been run end to end.** No LLM key was available on the
  machine these servers were built on. What is verified is every tool, its guards, and
  both servers reached through the same `MultiServerMCPClient` that `chatbot.py` uses.
- **The baseline is our reconstruction** of the obvious call. It is written to be fair —
  the call a careful developer would actually write from the docs — but it is ours.
  Every one is in `run_eval.py` and can be argued with.
- **These numbers move.** They are live counts from live services; BRC's workflow
  catalogue changed between 16 and 17 Sep while this was being built. Re-run the harness
  before quoting any figure.
- **Fourteen cases is not coverage.** It is the fourteen traps that were measured. Offline
  regression tests live in `tests/test_geo_tools.py` (52) and
  `tests/test_brc_analytics_tools.py` (26).
