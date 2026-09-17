"""Score recorded chatbot transcripts against the ground truths, with no model in the loop.

`run_questions.py` records what the agent did. It does not say whether the agent
was **right**, and the obvious way to find out -- ask another model -- puts a
grader with the same failure modes in charge of the grading. This script does the
opposite: every check here is decidable from the transcript plus the expectations
encoded at the top of this file, so two people running it get the same numbers and
can argue with the encoding rather than with a judge's mood.

    uv run evals/judge.py                       # every model under evals/runs/
    uv run evals/judge.py --runs evals/runs     # the same, said out loud
    uv run evals/judge.py --out evals/judge-report.md
    uv run evals/judge.py --quiet               # write the file, print nothing

Stdlib only, no network, no key. It reads `evals/runs/<model>/qNN.jsonl` as written
by `run_questions.py`: one JSON object per line -- user / assistant-with-tool_calls /
tool-result steps -- and a final `{"summary": {...}}` line.

What it scores, per transcript:

* **routed** -- did the agent call at least one tool from the source that can
  answer this question? The expected tools per question come from `QUESTIONS.md`
  and are in `EXPECTED` below. Presence, not order: a chain in the wrong order
  still reached the right source, and ordering is a weaker signal than routing.
* **no_fabrication** -- every number >= 100 in the answer has to appear in some
  tool result, tool argument, or the question itself. An unmatched number is a
  number the model produced from nowhere.
* **known_traps** -- the named ways to get an HTTP 200 and a wrong answer out of
  this board. Each is a separate flag, listed in `TRAP_NOTES`.
* **honest_null** -- for the questions whose correct answer is "cannot", or
  "partly, and here is the part that is missing", does the answer carry a refusal
  *with a reason*, and does it name the source that could answer it?
* **denied / error** -- passed through from the summary line. Argo returns HTTP
  200 with ACCESS DENIED as the assistant's content, so a denial is content, not
  an exception, and it has to be carried rather than inferred.

## The one thing this cannot see, said here rather than in a footnote

`run_questions.py` stores `result_excerpt = raw[:600]`. For a 97,060-character
`geo_search` result the judge holds 0.6% of the evidence. So an answer number that
is *not* in the transcript may still have been in the tool result the model saw.

This script therefore never calls such a number fabricated unless **every** tool
result in that transcript arrived complete (`result_chars <= len(result_excerpt)`).
Otherwise it is counted as `unmatched` and reported in a separate column, which
means "a human has to look", not "the model made it up". Raising RESULT_EXCERPT in
`run_questions.py` converts unmatched flags into decidable ones.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNS = REPO / "evals" / "runs"
OUT = REPO / "evals" / "judge-report.md"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# EVERYTHING EDITABLE IS BETWEEN HERE
# ---------------------------------------------------------------------------

# Per question: the tools that mean "this reached a source that can answer it",
# taken from the chains in QUESTIONS.md. `kind` is what a correct answer looks
# like -- "answer" wants a number, "gap" wants a refusal with a reason. `wired`
# is False where the expected source is not in MCP_SERVERS in chatbot.py, so
# routing cannot pass and the failure belongs to the repo, not to the model.
EXPECTED: dict[int, dict] = {
    1:  {"primary": {"uniprot_search", "uniprot_get_entry", "uniprot_get_protein_info"},
         "kind": "answer", "source": "UniProt"},
    2:  {"primary": {"ncbi_pathogen_organisms", "ncbi_pathogen_isolate_count"},
         "kind": "answer", "source": "NCBI Pathogen Detection"},
    3:  {"primary": {"geo_search", "geo_series"},
         "kind": "answer", "source": "NCBI GEO"},
    4:  {"primary": {"search_organisms", "get_assemblies", "get_compatible_workflows"},
         "kind": "answer", "source": "BRC Analytics"},
    5:  {"primary": {"lapis_list_organisms", "lapis_describe_organism", "lapis_aggregate_samples"},
         "kind": "answer", "source": "PDN / LAPIS"},
    6:  {"primary": {"ncbi_pathogen_amr_genes", "ncbi_pathogen_isolate_count"},
         "kind": "answer", "source": "NCBI Pathogen Detection"},
    7:  {"primary": {"geo_search", "geo_series", "get_assemblies",
                     "check_compatibility", "resolve_workflow_inputs"},
         "kind": "answer", "source": "NCBI GEO + BRC Analytics"},
    8:  {"primary": {"geo_series", "ncbi_sra_runs_for_project", "ncbi_sra_run_metadata",
                     "get_compatible_workflows"},
         "kind": "answer", "source": "NCBI GEO + SRA + BRC Analytics"},
    9:  {"primary": {"ncbi_pathogen_amr_genes", "ncbi_pathogen_isolate_count",
                     "check_compatibility"},
         "kind": "answer", "source": "NCBI Pathogen Detection + BRC Analytics"},
    10: {"primary": {"nde_facet_counts", "nde_search_datasets", "nde_get_record"},
         "kind": "gap", "wired": False, "source": "NDE (not in MCP_SERVERS)"},
    11: {"primary": {"nde_list_repositories", "nde_search_datasets"},
         "kind": "gap", "wired": False, "source": "NDE (not in MCP_SERVERS)"},
    12: {"primary": {"ncbi_taxonomy_lookup", "mygene_search_genes", "mygene_map_ids",
                     "uniprot_get_sequence"},
         "kind": "answer", "source": "NCBI Taxonomy + MyGene + UniProt"},
    13: {"primary": {"ncbi_pathogen_isolate_count", "ncbi_pathogen_amr_genes"},
         "kind": "gap", "source": "NCBI Pathogen Detection (the proof number)"},
    14: {"primary": {"ncbi_pathogen_isolate_count", "ncbi_pathogen_isolates"},
         "kind": "gap", "source": "NCBI Pathogen Detection (genotype only)"},
    15: {"primary": set(),
         "kind": "gap", "source": "none on this board -- RCSB PDB / AlphaFold"},
}

# A number this large in an answer is a claim about the data, not a sample count
# or a step number, so it has to trace to something the model was shown.
FABRICATION_MIN = 100

# A bare four-digit number in this range is read as a year and skipped. A real
# count that happens to land here is skipped with it; that is the price of not
# flagging every "2026" in a References section.
YEAR_RANGE = (1900, 2100)

# The refusal has to carry a reason. "I don't know" scores nothing here.
REFUSAL_MARKERS = (
    "cannot", "can not", "can't", "unable", "not available", "no tool",
    "not wired", "not connected", "not indexed", "malformed", "instead",
    "does not hold", "not recognised", "not recognized", "no source",
    "out of scope", "not possible", "does not carry", "not exposed",
)

# ... and name where the answer actually lives.
ALTERNATIVE_SOURCES = (
    "bv-brc", "bvbrc", "card", "rcsb", "protein data bank", "pdb",
    "alphafold", "bv-brc.org", "ncbi pathogen", "bigquery",
)

# The curated Pathogen Detection group is "E.coli and Shigella". Any of these as
# an `organism` argument returns a confident 0 that reads as absence. Measured
# 17 Sep 2026: 106 groups exist and none of them is "Escherichia coli".
WRONG_PATHOGEN_GROUPS = {
    "escherichia coli", "e. coli", "e.coli", "e coli",
    "shigella", "shigella flexneri", "shigella sonnei", "escherichia",
}

TRAP_NOTES = {
    "ena_keywords": "called `search_ena_keywords` -- BRC's federated tool returns an ENA 400 as tool *text*",
    "geo_no_entry_type": "called `geo_search` with no `entry_type` -- the count then mixes GSE, GSM, GDS and GPL",
    "gds_513": "quoted **513**, the unfiltered `db=gds` count, as the Series count (37 is right)",
    "ena_50": "quoted **50** as an ENA total -- that is the federated page size; 551,679 runs exist",
    "pathogen_wrong_group": "passed an organism that is not a curated group name -- returns 0, not an error",
    "zero_as_absence": "a tool returned 0 isolates and the answer reported the zero as a finding",
}

# ---------------------------------------------------------------------------
# AND HERE. Below this line is machinery.
# ---------------------------------------------------------------------------


# A number worth checking: comma-grouped or plain, not glued to a letter, an
# underscore or a dot. That excludes GSE309890, PRJNA1363958, P0AES4 and
# GCF_000005845.2, which are identifiers the model was handed, not counts.
_ANSWER_NUM = re.compile(r"(?<![A-Za-z0-9_.\-/])(\d{1,3}(?:,\d{3})+|\d+)(?![A-Za-z0-9_])")
# In the evidence, anything goes: a number inside an accession still counts as
# "the model saw these digits", and a permissive haystack means fewer false flags.
_ANY_NUM = re.compile(r"\d[\d,]*")


def _as_int(token: str) -> int | None:
    try:
        return int(token.replace(",", ""))
    except ValueError:
        return None


def answer_numbers(text: str) -> list[int]:
    """Numbers in an answer that are claims about the data."""
    out = []
    for m in _ANSWER_NUM.finditer(text or ""):
        n = _as_int(m.group(1))
        if n is None or n < FABRICATION_MIN:
            continue
        if "," not in m.group(1) and YEAR_RANGE[0] <= n <= YEAR_RANGE[1]:
            continue          # a year, not a count -- see YEAR_RANGE
        out.append(n)
    return out


def evidence_numbers(texts) -> set[int]:
    seen: set[int] = set()
    for t in texts:
        for m in _ANY_NUM.finditer(t or ""):
            n = _as_int(m.group(0))
            if n is not None:
                seen.add(n)
    return seen


class Transcript:
    """One qNN.jsonl: the steps, the summary, and the evidence the model was shown."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.steps: list[dict] = []
        self.summary: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "summary" in obj:
                self.summary = obj["summary"]
            else:
                self.steps.append(obj)

        self.number = self.summary.get("question_number") or _number_from_name(path)
        self.question = self.summary.get("question", "")
        self.answer = self.summary.get("answer", "") or ""
        self.tools = list(self.summary.get("tools_in_order") or [])
        self.denied = bool(self.summary.get("denied"))
        self.error = self.summary.get("error")

        self.calls = [c for s in self.steps for c in (s.get("tool_calls") or [])]
        self.results = [s for s in self.steps if s.get("role") == "tool"]
        if not self.tools:
            self.tools = [c.get("tool") for c in self.calls if c.get("tool")]

        # A result is complete only if nothing was cut off it. run_questions.py
        # truncates at RESULT_EXCERPT; result_chars is the length before the cut.
        self.truncated = any(
            (r.get("result_chars") or 0) > len(r.get("result_excerpt") or "")
            for r in self.results
        )
        self.evidence = evidence_numbers(
            [r.get("result_excerpt", "") for r in self.results]
            + [json.dumps(c.get("args"), ensure_ascii=False) for c in self.calls]
            + [self.question]
        )

    @property
    def results_text(self) -> str:
        return "\n".join(r.get("result_excerpt", "") for r in self.results)


def _number_from_name(path: pathlib.Path) -> int | None:
    m = re.search(r"q(\d+)", path.stem)
    return int(m.group(1)) if m else None


# --- the checks ------------------------------------------------------------


def check_routed(t: Transcript, exp: dict) -> tuple[str, str]:
    """Did it reach a tool from the source that can answer this question?"""
    primary = exp.get("primary") or set()
    if not primary:
        # Q15: the right behaviour is to call nothing and name RCSB/AlphaFold.
        return ("n/a", "no tool on this board can answer it")
    hit = [x for x in t.tools if x in primary]
    if hit:
        return ("yes", ", ".join(sorted(set(hit))))
    if exp.get("wired") is False:
        return ("blocked", "expected source is not in MCP_SERVERS")
    return ("no", "none of " + ", ".join(sorted(primary)))


def check_numbers(t: Transcript) -> dict:
    """Every number >= 100 in the answer, against everything the model was shown."""
    claimed = answer_numbers(t.answer)
    unmatched = sorted({n for n in claimed if n not in t.evidence})
    return {
        "claimed": len(claimed),
        "unmatched": unmatched,
        # Only decidable when no tool result was cut short -- see the docstring.
        "decidable": not t.truncated,
    }


def check_traps(t: Transcript) -> list[str]:
    fired = []
    if "search_ena_keywords" in t.tools:
        fired.append("ena_keywords")

    for c in t.calls:
        if c.get("tool") == "geo_search" and not (c.get("args") or {}).get("entry_type"):
            fired.append("geo_no_entry_type")
            break

    for c in t.calls:
        if not str(c.get("tool") or "").startswith("ncbi_pathogen"):
            continue
        args = c.get("args") or {}
        value = str(args.get("organism") or args.get("taxgroup_name") or "").strip().lower()
        if value in WRONG_PATHOGEN_GROUPS:
            fired.append("pathogen_wrong_group")
            break

    a = t.answer
    if t.number in (3, 7) and 513 in answer_numbers(a):
        fired.append("gds_513")

    # 50 on its own is a page size, a sample count and a percentage. It is only a
    # trap when the answer ties it to ENA or to a run count.
    if re.search(r"\b50\b[^.\n]{0,70}(ena|runs?\b)|(ena|runs?)[^.\n]{0,70}\b50\b", a, re.I):
        fired.append("ena_50")

    zero_result = re.search(r'(?i)(0 distinct isolates|"isolates"\s*:\s*0\b)', t.results_text)
    zero_claim = re.search(r"(?i)(\bzero\b|\b0\b)[^.\n]{0,60}isolat", a)
    if zero_result and zero_claim:
        fired.append("zero_as_absence")

    return fired


def check_honest_null(t: Transcript, exp: dict) -> dict:
    """For a question whose honest answer is 'cannot': is the refusal informative?"""
    if exp.get("kind") != "gap":
        return {"applies": False}
    low = t.answer.lower()
    return {
        "applies": True,
        "refusal": any(m in low for m in REFUSAL_MARKERS),
        "alternative": any(s in low for s in ALTERNATIVE_SOURCES),
    }


def judge_one(t: Transcript) -> dict:
    exp = EXPECTED.get(t.number, {"primary": set(), "kind": "answer", "source": "unknown"})
    routed, routed_detail = check_routed(t, exp)
    nums = check_numbers(t)
    return {
        "q": t.number,
        "question": t.question,
        "kind": exp.get("kind"),
        "source": exp.get("source", ""),
        "routed": routed,
        "routed_detail": routed_detail,
        "tools": t.tools,
        "numbers": nums,
        "traps": check_traps(t),
        "null": check_honest_null(t, exp),
        "denied": t.denied,
        "error": t.error,
        "truncated": t.truncated,
        "answer_chars": len(t.answer),
    }


# --- the report ------------------------------------------------------------


def _fmt_unmatched(nums: dict) -> str:
    if not nums["unmatched"]:
        return "—"
    shown = ", ".join(f"{n:,}" for n in nums["unmatched"][:4])
    more = "" if len(nums["unmatched"]) <= 4 else f" +{len(nums['unmatched']) - 4}"
    tag = "**fabricated**" if nums["decidable"] else "unmatched"
    return f"{tag}: {shown}{more}"


def _fmt_null(null: dict) -> str:
    if not null["applies"]:
        return "—"
    bits = []
    bits.append("reason ✓" if null["refusal"] else "**no reason**")
    bits.append("source ✓" if null["alternative"] else "**no source**")
    return " · ".join(bits)


def model_section(model: str, rows: list[dict]) -> list[str]:
    L = [f"## `{model}`", "",
         "| Q | expects | routed | tools called | nums ≥100 | unmatched | traps | honest null |",
         "|---|---|---|---|---:|---|---|---|"]
    for r in sorted(rows, key=lambda x: x["q"] or 0):
        if r["denied"]:
            L.append(f"| {r['q']} | — | **denied** | — | — | — | — | — |")
            continue
        if r["error"]:
            L.append(f"| {r['q']} | — | **error** | {r['error'][:60]} | — | — | — | — |")
            continue
        if not r["answer_chars"]:
            # Not denied, not an exception, and nothing said. That is its own
            # failure and must not read as a clean row.
            L.append(f"| {r['q']} | {r['kind']} | {r['routed']} | **empty answer** | — | — | — | — |")
            continue
        chain = " → ".join(f"`{x}`" for x in r["tools"]) or "*none*"
        traps = ", ".join(f"`{x}`" for x in r["traps"]) or "—"
        L.append(
            f"| {r['q']} | {r['kind']} | {r['routed']} | {chain[:120]} | "
            f"{r['numbers']['claimed']} | {_fmt_unmatched(r['numbers'])} | {traps} | "
            f"{_fmt_null(r['null'])} |"
        )
    L.append("")

    live = [r for r in rows if not r["denied"] and not r["error"]]
    routed_yes = sum(1 for r in live if r["routed"] == "yes")
    routed_scored = sum(1 for r in live if r["routed"] in ("yes", "no"))
    fab = sum(1 for r in live if r["numbers"]["unmatched"] and r["numbers"]["decidable"])
    unm = sum(1 for r in live if r["numbers"]["unmatched"] and not r["numbers"]["decidable"])
    traps = Counter(x for r in live for x in r["traps"])
    nulls = [r for r in live if r["null"]["applies"]]
    null_ok = sum(1 for r in nulls if r["null"]["refusal"])
    null_full = sum(1 for r in nulls if r["null"]["refusal"] and r["null"]["alternative"])

    L += [f"**{model}**: routed {routed_yes}/{routed_scored} scored "
          f"({len(live) - routed_scored} not scorable: source not wired, or no tool applies) · "
          f"fabrication flags {fab} · unmatched-but-truncated {unm} · "
          f"trap flags {sum(traps.values())} "
          f"({', '.join(f'{k}×{v}' for k, v in traps.most_common()) or 'none'}) · "
          f"honest null {null_ok}/{len(nulls)} with a reason, {null_full}/{len(nulls)} "
          f"also naming a source.", ""]
    return L


def write_report(by_model: dict[str, list[dict]], out: pathlib.Path) -> str:
    L = ["# Judge report", "",
         "Generated by `evals/judge.py` from the transcripts under `evals/runs/`. No model",
         "was asked anything: every flag below is decided from the transcript and the",
         "expectations encoded at the top of that file, which come from `QUESTIONS.md` and",
         "`PIPELINES.md`. Disagree with a flag by editing `EXPECTED` or `TRAP_NOTES` and",
         "re-running.", ""]

    for model in sorted(by_model):
        L += model_section(model, by_model[model])

    models = sorted(by_model)
    if len(models) > 1:
        L += ["## Cross-model", "",
              "| Q | " + " | ".join(f"`{m}`" for m in models) + " |",
              "|---|" + "---|" * len(models)]
        qs = sorted({r["q"] for rows in by_model.values() for r in rows if r["q"]})
        for q in qs:
            cells = []
            for m in models:
                r = next((x for x in by_model[m] if x["q"] == q), None)
                if r is None:
                    cells.append("—")
                elif r["denied"]:
                    cells.append("denied")
                elif r["error"]:
                    cells.append("error")
                else:
                    bits = [r["routed"]]
                    if r["numbers"]["unmatched"]:
                        bits.append("fab" if r["numbers"]["decidable"] else "unm")
                    if r["traps"]:
                        bits.append("trap:" + ",".join(r["traps"]))
                    if r["null"]["applies"]:
                        bits.append("null✓" if r["null"]["refusal"] else "null✗")
                    cells.append(" · ".join(bits))
            L.append(f"| {q} | " + " | ".join(cells) + " |")
        L.append("")

    L += ["## What each trap flag means", ""]
    L += [f"- `{k}` — {v}" for k, v in TRAP_NOTES.items()]
    L += ["",
          "## What this judge cannot decide", "",
          "- **Truncated evidence.** `run_questions.py` keeps the first 600 characters of",
          "  each tool result. A number the model was shown further down looks unmatched",
          "  here, so an unmatched number is only called **fabricated** when every tool",
          "  result in that transcript arrived whole. Everything else says `unmatched`",
          "  and means a human has to look.",
          "- **Derived numbers.** 29% of 581,464 is arithmetic, not fabrication, and this",
          "  script cannot tell the two apart. A subtraction or a percentage the model",
          "  computed correctly will still be flagged.",
          "- **Years.** A bare four-digit number between 1900 and 2100 is skipped, so a",
          "  real count in that range is skipped with it.",
          "- **Order.** `routed` asks whether the right source was reached, not whether",
          "  the chain ran in the documented order.",
          "- **Identifiers are checked like counts, on purpose.** A PMID or a UID in the",
          "  answer with no tool result behind it is a fabricated citation, which the",
          "  SYSTEM_PROMPT forbids in as many words, so it is flagged rather than exempted.",
          "  Accessions glued to letters (`GSE309890`, `P0AES4`) are not, because the",
          "  letters make them unambiguous and the model was handed them.",
          "- **Whether the answer is true.** A correctly routed, fully evidenced answer",
          "  can still misread its own tool result. Compare against the ground truths in",
          "  `PIPELINES.md` by hand.", ""]

    text = "\n".join(L)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text


def collect(runs: pathlib.Path) -> dict[str, list[dict]]:
    by_model: dict[str, list[dict]] = {}
    for model_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        rows = []
        for path in sorted(model_dir.glob("q*.jsonl")):
            try:
                rows.append(judge_one(Transcript(path)))
            except Exception as exc:   # one unreadable transcript must not lose the run
                print(f"  could not read {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if rows:
            by_model[model_dir.name] = rows
    return by_model


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=pathlib.Path, default=RUNS,
                    help="directory of <model>/qNN.jsonl transcripts")
    ap.add_argument("--out", type=pathlib.Path, default=OUT, help="markdown report to write")
    ap.add_argument("--quiet", action="store_true", help="write the report, print nothing")
    args = ap.parse_args()

    if not args.runs.is_dir():
        print(f"no transcripts: {args.runs} is not a directory", file=sys.stderr)
        return 2
    by_model = collect(args.runs)
    if not by_model:
        print(f"no qNN.jsonl transcripts under {args.runs}", file=sys.stderr)
        return 2
    text = write_report(by_model, args.out)
    if not args.quiet:
        print(text)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
