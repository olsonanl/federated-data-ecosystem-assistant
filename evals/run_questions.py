"""Drive the real chatbot agent through the demo questions and record every step.

This is the missing piece of evidence. The offline suite proves each tool is
right; `run_eval.py` proves the tools beat the documented call. Neither says
anything about the *system*: whether the model picks the right tool, chains them
in the right order, or writes an answer a person would accept. This does.

It uses exactly what `chatbot.py` uses -- `init_agent()`, the same MCP servers,
the same SYSTEM_PROMPT -- so what it records is what a user gets. Run it with
several models and it writes a side-by-side comparison as well.

    uv run evals/run_questions.py                          # LLM_MODEL from .env, all 15
    uv run evals/run_questions.py --only 3 7               # a subset, by Q number
    uv run evals/run_questions.py --model argo/gpt4o argo/claudesonnet45 argo/claudehaiku45
    uv run evals/run_questions.py --dry-run                # list questions, call nothing

Prerequisites:
  * the local servers are up:  uv run run_mcp_servers.py
  * `.env` holds the credential for each provider you name, e.g.
        LLM_MODEL=argo/claudesonnet45
        ARGO_USER=<your Argonne username>
    Argo only resolves on the Argonne network; off it, every call hangs.

Output:
  evals/runs/<model>/qNN.jsonl          every message the agent produced: tool calls
                                        with arguments, tool results (truncated), answer
  evals/runs/<model>/routing-scorecard.md  one row per question, tools in call order
  evals/model-comparison.md             one row per question, one column per model,
                                        plus per-model totals -- only with --model

Argo returns HTTP 200 with "ACCESS DENIED" as the assistant's *content* when the
username is not authorised -- there is no auth error to catch. A model whose
first reply says that is marked denied and skipped for the rest of the run,
rather than recording fifteen nonsense answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
# The Windows console is cp1252; transcripts and tables are UTF-8 files, but the
# progress lines go to stdout and must not crash on a non-ASCII answer excerpt.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # a Chainlit dependency, already installed

# chatbot.py never calls load_dotenv itself; Chainlit does it on import from the
# working directory. This driver is not Chainlit, so it has to do the same.
load_dotenv(REPO / ".env")

import chatbot  # noqa: E402
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage  # noqa: E402

QUESTIONS_MD = REPO / "evals" / "QUESTIONS.md"
RUNS = REPO / "evals" / "runs"
RUN_TAG = ""   # "-minimal", "-r2", set per run by run_model
COMPARISON = REPO / "evals" / "model-comparison.md"

# `## Q7. "Which E. coli ..." — NCBI ↔ BRC`  ->  (7, 'Which E. coli ...')
_Q = re.compile(r'^## Q(\d+)\.\s+"(.+?)"')

DENIAL = "ACCESS DENIED"

# Reference list prices, USD per 1M tokens (input, output), for the "what would this
# cost outside Argonne" column. Argo bills NONE of this -- it draws on Argonne's
# allocation -- so the column is a comparison aid, not a bill. Approximate, from
# public price pages as remembered on 17 Sep 2026; verify before quoting, and a
# model absent here simply gets no cost column. Keys are the Argo aliases.
LIST_PRICE_PER_M = {
    "gpt4o": (2.50, 10.00),
    "gpt41": (2.00, 8.00), "gpt41mini": (0.40, 1.60), "gpt41nano": (0.10, 0.40),
    "gpto3": (2.00, 8.00), "gpto3mini": (1.10, 4.40), "gpto4mini": (1.10, 4.40),
    "claudesonnet45": (3.00, 15.00), "claudehaiku45": (1.00, 5.00),
    "claudeopus45": (15.00, 75.00), "claudeopus41": (15.00, 75.00),
}

# Prompt ablation. "paper" is whatever chatbot.py ships (the 57-line research-paper
# prompt on main); "minimal" is the three-line prompt the repo started with; "none"
# is no system prompt at all. Comparing the three per model measures what the
# prompt actually buys -- tool selection, refusal quality, answer shape -- rather
# than assuming it.
MINIMAL_PROMPT = """You are a bioinformatics assistant with access to several databases.
Always use tools to retrieve real data, never invent accessions or sequences.
For multi-step questions, chain tools: search -> get entry -> get interactions.
"""
PROMPTS = {"paper": None, "minimal": MINIMAL_PROMPT, "none": ""}
RESULT_EXCERPT = 600


def load_questions() -> list[tuple[int, str]]:
    out = []
    for line in QUESTIONS_MD.read_text(encoding="utf-8").splitlines():
        m = _Q.match(line)
        if m:
            text = m.group(2).replace("*", "")   # markdown italics on species names
            out.append((int(m.group(1)), text))
    return out


def _safe(model: str) -> str:
    """argo/claudesonnet45 -> argo_claudesonnet45, usable as a directory name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model) + RUN_TAG


def _tool_calls(msg) -> list[dict]:
    return [
        {"tool": tc.get("name"), "args": tc.get("args")}
        for tc in (getattr(msg, "tool_calls", None) or [])
    ]


def _text(msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    # Anthropic-style content blocks
    return "".join(
        b.get("text", "") if isinstance(b, dict) else str(b) for b in content
    )


async def run_one(agent, model: str, number: int, question: str) -> dict:
    """Stream the agent exactly the way chatbot.py does, and rebuild the transcript.

    Argo's Claude models answer HTTP 500 "Streaming is required for operations
    that may take longer than 10 minutes" to a non-streaming call, so ainvoke()
    fails on argo/claudesonnet45 while the Chainlit UI -- which streams -- works.
    Measured by laptop_system_improvement on 17 Sep. Streaming here keeps the
    model, the transport and the code path identical to what the demo ships.

    stream_mode="messages" yields AIMessageChunks and ToolMessages. Chunks are
    summed with `+`, which is how LangChain reassembles tool_call_chunks into
    tool_calls, so the accumulated chunk carries the full tool calls and text.
    """
    t0 = time.monotonic()
    steps: list[dict] = [{"role": "user", "text": question}]
    tools_in_order: list[str] = []
    answer = ""
    denied = False
    ai_acc = None
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    llm_round_trips = 0
    tool_result_chars = 0
    ttft: float | None = None          # time to the first token from the model
    tool_timings: list[dict] = []      # per tool call: name, seconds
    last_activity: float = t0          # when the model last emitted anything
    tool_seconds = 0.0

    def flush_ai():
        nonlocal ai_acc, answer, denied, llm_round_trips
        if ai_acc is None:
            return
        llm_round_trips += 1
        um = getattr(ai_acc, "usage_metadata", None) or {}
        for k in usage:
            usage[k] += int(um.get(k) or 0)
        calls = _tool_calls(ai_acc)
        text = _text(ai_acc)
        if DENIAL in text.upper():
            denied = True
        steps.append({"role": "assistant", "text": text, "tool_calls": calls,
                      "usage": {k: int(um.get(k) or 0) for k in usage}})
        tools_in_order.extend(c["tool"] for c in calls if c["tool"])
        if text and not calls:
            answer = text          # the last text-only AI message is the answer
        ai_acc = None

    async for item in agent.astream({"messages": [HumanMessage(content=question)]},
                                    stream_mode="messages"):
        chunk = item[0] if isinstance(item, tuple) else item
        if isinstance(chunk, ToolMessage):
            flush_ai()
            now = time.monotonic()
            # last_activity is when the model last emitted a chunk, i.e. when it
            # finished asking for this tool (or when the previous tool result was
            # consumed). The gap to this result is the tool's wall time.
            secs = round(now - last_activity, 2)
            last_activity = now
            tool_seconds += secs
            raw = _text(chunk)
            name = getattr(chunk, "name", None)
            tool_timings.append({"tool": name, "seconds": secs, "result_chars": len(raw)})
            steps.append({
                "role": "tool",
                "tool": name,
                "seconds": secs,
                "result_excerpt": raw[:RESULT_EXCERPT],
                "result_chars": len(raw),
            })
        elif isinstance(chunk, AIMessageChunk):
            last_activity = time.monotonic()
            if ttft is None and (chunk.content or getattr(chunk, "tool_call_chunks", None)):
                ttft = round(last_activity - t0, 2)
            ai_acc = chunk if ai_acc is None else ai_acc + chunk
    flush_ai()
    elapsed = round(time.monotonic() - t0, 1)
    tool_result_chars = sum(st.get("result_chars", 0) for st in steps if st.get("role") == "tool")
    alias = model.split("/", 1)[-1]
    price = LIST_PRICE_PER_M.get(alias)
    list_cost_usd = (round(usage["input_tokens"] / 1e6 * price[0]
                           + usage["output_tokens"] / 1e6 * price[1], 4)
                     if price and usage["total_tokens"] else None)

    record = {
        "question_number": number,
        "question": question,
        "model": model,
        "elapsed_s": elapsed,
        "tools_in_order": tools_in_order,
        "tool_call_count": len(tools_in_order),
        "denied": denied,
        "error": None,
        "answer": answer,
        "answer_chars": len(answer),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "usage_reported": bool(usage["total_tokens"]),
        "ttft_s": ttft,
        "llm_round_trips": llm_round_trips,
        "tool_result_chars": tool_result_chars,
        "tool_seconds": round(tool_seconds, 2),
        "model_seconds": round(max(elapsed - tool_seconds, 0.0), 2),
        "tool_timings": tool_timings,
        "list_cost_usd": list_cost_usd,
        "steps": steps,
    }
    out_dir = RUNS / _safe(model)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"q{number:02d}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for step in steps:
            f.write(json.dumps(step, ensure_ascii=False) + "\n")
        f.write(json.dumps({"summary": {k: v for k, v in record.items() if k != "steps"}},
                           ensure_ascii=False) + "\n")
    return record


def _error_record(model: str, number: int, question: str, exc: Exception) -> dict:
    return {"question_number": number, "question": question, "model": model,
            "elapsed_s": 0, "tools_in_order": [], "tool_call_count": 0, "denied": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}", "answer": "", "answer_chars": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "usage_reported": False,
            "ttft_s": None, "llm_round_trips": 0, "tool_result_chars": 0, "list_cost_usd": None,
            "tool_seconds": 0.0, "model_seconds": 0.0, "tool_timings": []}


def write_scorecard(model: str, records: list[dict]) -> pathlib.Path:
    path = RUNS / _safe(model) / "routing-scorecard.md"
    L = ["# Routing scorecard", "",
         f"Model: `{model}`. Generated by `evals/run_questions.py`; one row per question,",
         "tools in the order the agent called them. Compare each row with the expected chain",
         "in `QUESTIONS.md`. Full transcripts are the `qNN.jsonl` files beside this one.", "",
         "| Q | question | tools called, in order | calls | time | answer opens with |",
         "|---|---|---|---|---|---|"]
    for r in records:
        chain = " → ".join(f"`{t}`" for t in r["tools_in_order"]) or "*none*"
        opening = (r["answer"] or "").strip().replace("\n", " ")[:90]
        if r["denied"]:
            opening = "**ACCESS DENIED** (see docstring)"
        elif r["error"]:
            opening = f"**ERROR** {r['error'][:80]}"
        L.append(f"| {r['question_number']} | {r['question'][:70]} | {chain} | "
                 f"{r['tool_call_count']} | {r['elapsed_s']}s | {opening} |")
    L += ["", "## What to look for", "",
          "- A question that names a source and a row that never calls it.",
          "- A `search_ena_keywords` call: that federated tool returns an ENA 400 as text.",
          "- `geo_search` without `entry_type`: the count would mix four record types.",
          "- Any answer that quotes a number with no tool call behind it.",
          "- `denied`: an unauthorised Argo user gets HTTP 200 with ACCESS DENIED as content.", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def write_comparison(by_model: dict[str, list[dict]], questions: list[tuple[int, str]]) -> None:
    models = list(by_model)
    L = ["# Model comparison", "",
         "Same 15 questions, same servers, same SYSTEM_PROMPT, one column per model.",
         "Generated by `evals/run_questions.py --model ...`. Each cell is",
         "`calls · seconds · first tool`; **denied** means Argo answered ACCESS DENIED,",
         "**error** means the run raised. Transcripts under `evals/runs/<model>/`.", "",
         "This table measures behaviour, not correctness: it says which tools each model",
         "reached for and how fast, not whether the answer was right. Judge correctness",
         "against the ground truths in `PIPELINES.md` by reading the transcripts.", "",
         "| Q | question | " + " | ".join(f"`{m}`" for m in models) + " |",
         "|---|---|" + "---|" * len(models)]
    index = {m: {r["question_number"]: r for r in recs} for m, recs in by_model.items()}
    for n, q in questions:
        cells = []
        for m in models:
            r = index[m].get(n)
            if r is None:
                cells.append("—")
            elif r["denied"]:
                cells.append("**denied**")
            elif r["error"]:
                cells.append("**error**")
            else:
                first = r["tools_in_order"][0] if r["tools_in_order"] else "no tool"
                tok = f" · {r['total_tokens']:,}t" if r.get("total_tokens") else ""
                cells.append(f"{r['tool_call_count']} · {r['elapsed_s']}s{tok} · `{first}`")
        L.append(f"| {n} | {q[:60]} | " + " | ".join(cells) + " |")

    L += ["", "## Per-model totals", "",
          "Tokens are what the provider reported on the stream (`usage_reported` says whether it did).",
          "`list $` is what the run would cost at public list price outside Argonne -- Argo bills none of it.", "",
          "| model | answered | denied | errors | mean calls | LLM trips | mean s | model s | tool s | mean TTFT s | in tok | out tok | usage | no-tool | mean chars | list $ |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        recs = by_model[m]
        ok = [r for r in recs if not r["denied"] and not r["error"]]
        n_ok = len(ok) or 1
        ttfts = [r["ttft_s"] for r in ok if r.get("ttft_s") is not None]
        costs = [r["list_cost_usd"] for r in ok if r.get("list_cost_usd") is not None]
        L.append(
            f"| `{m}` | {len(ok)}/{len(recs)} | {sum(r['denied'] for r in recs)} | "
            f"{sum(bool(r['error']) for r in recs)} | "
            f"{sum(r['tool_call_count'] for r in ok) / n_ok:.1f} | "
            f"{sum(r.get('llm_round_trips', 0) for r in ok) / n_ok:.1f} | "
            f"{sum(r['elapsed_s'] for r in ok) / n_ok:.1f} | "
            f"{sum(r.get('model_seconds', 0) for r in ok) / n_ok:.1f} | "
            f"{sum(r.get('tool_seconds', 0) for r in ok) / n_ok:.1f} | "
            f"{(sum(ttfts) / len(ttfts)) if ttfts else 0:.1f} | "
            f"{sum(r.get('input_tokens', 0) for r in ok):,} | "
            f"{sum(r.get('output_tokens', 0) for r in ok):,} | "
            f"{sum(1 for r in ok if r.get('usage_reported'))}/{len(ok)} | "
            f"{sum(1 for r in ok if not r['tools_in_order'])} | "
            f"{sum(r['answer_chars'] for r in ok) / n_ok:.0f} | "
            f"{('$' + format(sum(costs), '.2f')) if costs else '--'} |")
    L += ["", "**A no-tool answer to a data question is a fabrication until proven otherwise.**",
          "That column is the first one to read.", ""]
    COMPARISON.write_text("\n".join(L), encoding="utf-8")


async def run_model(model: str, questions: list[tuple[int, str]],
                    prompt: str = "paper", rep: int = 0) -> list[dict]:
    chatbot.LLM_MODEL = model            # load_chat_model reads this at init_agent()
    if PROMPTS.get(prompt) is not None:  # "paper" leaves chatbot.SYSTEM_PROMPT alone
        chatbot.SYSTEM_PROMPT = PROMPTS[prompt]
    global RUN_TAG
    RUN_TAG = (f"-{prompt}" if prompt != "paper" else "") + (f"-r{rep}" if rep else "")
    print(f"\n===== {model} =====\nconnecting to MCP servers ...")
    try:
        agent = await chatbot.init_agent()
    except Exception as exc:
        print(f"  could not build the agent: {type(exc).__name__}: {str(exc)[:200]}")
        return [_error_record(model, n, q, exc) for n, q in questions]

    records: list[dict] = []
    for n, q in questions:
        print(f"--- Q{n:02d}: {q[:80]}")
        try:
            rec = await run_one(agent, model, n, q)
        except Exception as exc:  # one bad question must not lose the run
            print(f"    FAILED: {type(exc).__name__}: {str(exc)[:160]}")
            records.append(_error_record(model, n, q, exc))
            continue
        records.append(rec)
        print(f"    {rec['tool_call_count']} tool call(s) in {rec['elapsed_s']}s: "
              + (" -> ".join(rec["tools_in_order"]) or "none"))
        if rec["denied"]:
            print("    Argo answered ACCESS DENIED with no error -- this username is not "
                  "authorised or the machine is off the Argonne network. Skipping the "
                  "rest of this model.")
            for n2, q2 in questions[len(records):]:
                records.append({**_error_record(model, n2, q2, RuntimeError("skipped after denial")),
                                "denied": True, "error": None})
            break
    path = write_scorecard(model, records)
    print(f"  wrote {path.relative_to(REPO)}")
    return records


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", type=int, nargs="*", help="question numbers to run")
    ap.add_argument("--model", nargs="*",
                    help="one or more LLM_MODEL values, e.g. argo/gpt4o argo/claudesonnet45; "
                         "default is LLM_MODEL from .env")
    ap.add_argument("--prompt", nargs="*", choices=list(PROMPTS), default=["paper"],
                    help="system-prompt variants to run: paper (as shipped), minimal, none")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each model/prompt N times to measure tool-selection variance")
    ap.add_argument("--dry-run", action="store_true", help="list questions and exit")
    args = ap.parse_args()

    questions = load_questions()
    if args.only:
        questions = [q for q in questions if q[0] in set(args.only)]
    if not questions:
        print("no questions matched", file=sys.stderr)
        return 2
    models = args.model or [chatbot.LLM_MODEL]
    print(f"{len(questions)} question(s) x {len(models)} model(s): {', '.join(models)}")
    for n, q in questions:
        print(f"  Q{n:02d}  {q[:88]}")
    if args.dry_run:
        return 0

    by_model: dict[str, list[dict]] = {}
    for model in models:
        for prompt in args.prompt:
            for rep in range(args.repeat):
                label = model + ("" if prompt == "paper" else f" [{prompt}]") + \
                        ("" if args.repeat == 1 else f" #{rep + 1}")
                by_model[label] = await run_model(model, questions, prompt=prompt, rep=rep)

    if args.model:
        write_comparison(by_model, questions)
        print(f"\nwrote {COMPARISON.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
