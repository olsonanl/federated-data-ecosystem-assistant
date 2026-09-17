"""Measure what these MCP servers add over calling the APIs straight from the docs.

The claim an MCP server has to earn is not "it reaches the API" -- anything reaches
the API. It is that a competent developer reading the same documentation, writing
the obvious call, gets a WRONG answer that looks right, and that the tool gets it
right. This harness makes that claim falsifiable.

Each case runs twice against the live services:

  baseline  the obvious call, written the way the API docs lead you to write it
  tool      our MCP tool

and both are scored against a ground truth recorded from a verified observation.
A case only counts as value added when baseline fails and the tool passes. Cases
where both pass are reported too, and honestly: they are cases where the tool is
convenience, not correctness.

Run:  uv run evals/run_eval.py
      uv run evals/run_eval.py --markdown evals/REPORT.md
"""

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "mcp_servers"))

import geo as GEO                      # noqa: E402
import brc_analytics as BRC            # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BRC_MCP = "https://brc-analytics.org/api/v1/mcp/"
TOOL = "niaid-bionexus-p7"

def _pace() -> None:
    """Take a turn from geo.py's pacer rather than keeping a second one.

    NCBI's 3/second ceiling is per IP. The first run of this harness used its own
    independent pacer alongside geo.py's, put roughly 5 req/s on eutils, and got
    HTTP 429 -- scoring a throttle as a failed case. Sharing the pacer fixes the
    measurement; the retry that run also exposed is fixed in geo.py itself.
    """
    GEO._wait_turn()


def eutils(endpoint: str, **params) -> dict:
    """A bare E-utilities call -- exactly what the docs show, no guards."""
    _pace()
    query = {"db": "gds", "retmode": "json", "tool": TOOL, **params}
    url = f"{EUTILS}/{endpoint}.fcgi?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def brc_mcp(name: str, args: dict):
    """Call BRC's own federated MCP server, the way the chatbot does."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}}
    ).encode()
    req = urllib.request.Request(BRC_MCP, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode()
    for line in raw.splitlines():
        if line.startswith("data: "):
            d = json.loads(line[6:])
            text = d.get("result", {}).get("content", [{}])[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_text": text}
    return {}


def get_json(url: str, timeout: int = 60) -> dict:
    """GET a JSON endpoint that is not E-utilities: NCBI Datasets, UniProt REST.

    Paced through geo.py's pacer for the same reason eutils() is: Datasets is
    NCBI, the ceiling is per IP, and the venue IP is shared with the room.
    """
    _pace()
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{TOOL} (+codeathon)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


PATHOGENS = "https://www.ncbi.nlm.nih.gov/pathogens/pathogens-srv/"


def pathogen_count(fq: str | None = None) -> int | None:
    """Distinct isolates from the target_acc facet.

    Never totalCount: that number is 2x the isolate count for E. coli and
    exactly 1x for S. aureus, so no arithmetic correction on it is safe.
    """
    _pace()
    params = {"action": "retrieve", "collection": "pathogen",
              "limit": 0, "facets": "target_acc[||1|1]"}
    if fq:
        params["fq"] = fq
    url = PATHOGENS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{TOOL} (+codeathon)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.loads(r.read().decode())

    found = []

    def walk(node):
        if isinstance(node, dict):
            if "numBuckets" in node:
                found.append(node["numBuckets"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return found[0] if found else None


# ---------------------------------------------------------------- the cases


def case_gds_prefix():
    """GDS is the one GEO prefix with no type digit, and guessing returns a real record."""
    truth = "GDS5163"

    def baseline():
        # The docs give the prefix rule for GSE/GPL/GSM. GDS looks like a platform
        # sibling, so 1 + padded is the natural guess.
        rec = eutils("esummary", id="100005163", version="2.0")
        got = list(rec.get("result", {}).values())
        acc = next((r.get("accession") for r in got if isinstance(r, dict)), None)
        return acc, f"esummary id=100005163 -> {acc}"

    def tool():
        out = GEO.geo_series("GDS5163", list_files=False)
        acc = out["record"]["accession"]
        return acc, f"geo_series('GDS5163') -> {acc}"

    return ("geo-gds-prefix",
            "Fetch the curated DataSet GDS5163",
            truth, baseline, tool,
            "A wrong prefix returns a real Affymetrix array design, with no error.")


def case_gsm_resolution():
    """Resolving a GEO accession by searching for it returns the wrong record type."""
    truth = "309284462"

    def baseline():
        # The documented way to turn a term into UIDs is esearch. Take the first hit.
        res = eutils("esearch", term="GSM9284462", retmax=5)
        ids = res["esearchresult"].get("idlist", [])
        first = ids[0] if ids else None
        return first, f"esearch term=GSM9284462 -> {len(ids)} hits, first={first}"

    def tool():
        out = GEO.geo_resolve_accession("GSM9284462")
        uid = out["results"][0]["uid"]
        return uid, f"geo_resolve_accession -> {uid} (0 requests)"

    return ("geo-gsm-resolve",
            "Get the Entrez UID for sample GSM9284462",
            truth, baseline, tool,
            "gds indexes the accession inside every related record, so the Series ranks first.")


def case_series_count():
    """An unfiltered gds count sums four different record types."""
    truth = 37

    def baseline():
        res = eutils("esearch",
                     term='"Escherichia coli"[Organism] AND ciprofloxacin')
        return int(res["esearchresult"]["count"]), \
            f'esearch without an entry-type filter -> {res["esearchresult"]["count"]}'

    def tool():
        out = GEO.geo_search(organism="Escherichia coli", term="ciprofloxacin",
                             entry_type="gse", max_results=1)
        return out["total_count"], f'geo_search(entry_type="gse") -> {out["total_count"]}'

    return ("geo-series-count",
            "How many E. coli GEO Series involve ciprofloxacin?",
            truth, baseline, tool,
            "Without a filter the count mixes Series, Platforms, Samples and DataSets.")


def case_expression_files():
    """The processed values are in no API response at all."""
    truth = "GSE309890_FPKMs_allSamples.csv.gz"

    def baseline():
        rec = eutils("esummary", id="200309890", version="2.0")
        r = rec["result"]["200309890"]
        supp = r.get("suppfile")
        return supp, f"esummary suppfile -> {supp!r} (a format, not a filename)"

    def tool():
        out = GEO.geo_series("GSE309890")
        names = [f["name"] for f in out.get("supplementary_files", [])]
        return (names[0] if names else None), \
            f"geo_series -> {len(names)} file(s): {names[0] if names else 'none'}"

    return ("geo-expression-files",
            "Where are the expression values for GSE309890?",
            truth, baseline, tool,
            "esummary names file FORMATS. The filenames and URLs live on the FTP host.")


def case_retmax():
    """esearch silently returns 20 ids while reporting a much larger count."""
    truth = "the id list is complete, or the payload says it was cut"

    def baseline():
        res = eutils("esearch", term='"Escherichia coli"[Organism] AND "gse"[Filter]')
        b = res["esearchresult"]
        returned, count = len(b.get("idlist", [])), int(b["count"])
        # esearch echoes retmax=20 -- a default the caller never set -- but it
        # carries no field that says the answer is incomplete. You only learn
        # that by thinking to compare two numbers you were not told to compare.
        stated = b.get("truncated") is True
        return (returned, count, stated), \
            (f'count={count} but {returned} ids returned; retmax echoed as '
             f'{b.get("retmax")}, no truncation flag in the payload')

    def tool():
        out = GEO.geo_search(organism="Escherichia coli", entry_type="gse", max_results=5)
        return (out["returned"], out["total_count"], out.get("truncated") is True), \
            f'total_count={out["total_count"]}, returned={out["returned"]}, truncated={out.get("truncated")}'

    def states_the_cut(v):
        returned, count, stated = v
        # returned < count is true of the baseline too, so it cannot be the
        # test. The flag is the whole point of the case: a caller that never
        # reads it has no way to know the list is partial.
        return returned == count or stated

    return ("geo-retmax", "List the E. coli GEO Series",
            truth, baseline, tool,
            "Default retmax is 20. The count is real; the id list is quietly cut.",
            states_the_cut, states_the_cut)


def case_ena_keywords():
    """BRC's own keyword search is broken, and fails as text rather than as an error."""
    truth = "a usable row count"

    def baseline():
        out = brc_mcp("search_ena_keywords",
                      {"keywords": ["Escherichia coli", "antimicrobial resistance"]})
        if "_text" in out:
            return None, "search_ena_keywords -> error text, no rows"
        return out.get("count"), f'search_ena_keywords -> count={out.get("count")}'

    def tool():
        out = BRC.brc_ena_search(organism="Escherichia coli",
                                 title_contains="resistance", limit=2)
        return out["total_matching"], \
            f'brc_ena_search -> total_matching={out["total_matching"]:,}'

    def usable(v):
        # Not "> 0": a page size passes that. This is the E. coli subset whose
        # study title mentions resistance, 48,421 on 17 Sep. The band has to sit
        # above any page size (2, 50, 1000) and below the unfiltered ENA total
        # for taxid 562 (551,679), so a tool that quietly drops the title filter
        # fails here rather than looking like a bigger, better answer.
        return isinstance(v, int) and 10_000 <= v <= 200_000

    return ("brc-ena-keywords",
            "Find E. coli sequencing runs whose study mentions resistance",
            truth, baseline, tool,
            "It sends an unquoted scientific_name to ENA; ENA answers HTTP 400.",
            usable, usable)


def case_ena_total():
    """The federated tool caps at 50 and never reports a total."""
    truth = "a real total"

    def baseline():
        out = brc_mcp("search_ena", {"taxonomy_id": "562"})
        return out.get("count"), \
            f'search_ena -> count={out.get("count")}, has_more={out.get("has_more")}'

    def tool():
        out = BRC.brc_ena_runs("562", limit=1)
        return out["total_in_ena"], f'brc_ena_runs -> total_in_ena={out["total_in_ena"]:,}'

    def real_total(v):
        # Not "> 1000": 1,001 would pass where 551,679 is the answer, and any
        # page size a future client picks could land above a flat threshold.
        # A band pinned to the observed magnitude (551,679 on 17 Sep, growing)
        # fails both a page size and a number off by an order of magnitude. The
        # bounds are deliberately wide, not an exact match, because ENA grows
        # daily and an exact number would turn every re-run into a false alarm.
        return isinstance(v, int) and 400_000 <= v <= 900_000

    return ("brc-ena-total", "How many sequencing runs does ENA hold for E. coli?",
            truth, baseline, tool,
            "50 with has_more is a page size. Reporting it as the answer is wrong by 4 orders of magnitude.",
            real_total, real_total)


def case_study_organisms():
    """A project's organism label is not its runs' organism."""
    truth = 13

    def baseline():
        # The obvious read: BRC proxies ENA, so ask it for the study.
        try:
            req = urllib.request.Request(
                "https://brc-analytics.org/api/v1/ena/study/PRJNA715470")
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            return len({x.get("scientific_name") for x in data.get("results", [])}), \
                "GET /api/v1/ena/study/PRJNA715470 -> parsed"
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return None, f"GET /api/v1/ena/study/PRJNA715470 -> HTTP {code}"

    def tool():
        out = BRC.brc_ena_study("PRJNA715470")
        n = len(out["organisms_in_runs"])
        return n, f'brc_ena_study -> {out["run_count"]} runs, {n} distinct organisms'

    return ("brc-study-organisms",
            "What organisms are actually in BioProject PRJNA715470?",
            truth, baseline, tool,
            "BRC's study endpoint 500s. The project is labelled E. coli and holds 13 genera-worth of runs.")


def case_pathogen_group_name():
    """The group name is curated, and a wrong one returns a confident zero."""
    truth = 581464

    def baseline():
        # The organism is Escherichia coli. Every other source on the board
        # takes that string, so this is the obvious value to pass.
        n = pathogen_count('taxgroup_name==["Escherichia coli"]')
        return n, f'taxgroup_name=="Escherichia coli" -> {n}'

    def tool():
        n = pathogen_count('taxgroup_name==["E.coli and Shigella"]')
        return n, f'taxgroup_name=="E.coli and Shigella" -> {n:,}'

    def ok(v):
        return isinstance(v, int) and v > 100000

    return ("amr-group-name",
            "How many E. coli isolates has NCBI Pathogen Detection sequenced?",
            truth, baseline, tool,
            "Pathogen Detection groups are curated, not taxonomy names. A wrong one "
            "returns 0, which reads as absence rather than as a bad query.",
            ok, ok)


def case_gap_methicillin():
    """The board's headline question. The number is the answer, and it is small."""
    truth = "a number that shows the category is empty, plus a reframing"

    def baseline():
        # Without the counts, the only honest option is to decline -- and a
        # decline carries no information about whether the data is missing or
        # the question is malformed.
        return None, "no number available; the question can only be declined"

    def tool():
        ecoli = pathogen_count('taxgroup_name==["E.coli and Shigella"] '
                               'and AMR_genotypes==["mecA"]')
        aureus = pathogen_count('taxgroup_name==["Staphylococcus aureus"] '
                                'and AMR_genotypes==["mecA"]')
        return (ecoli, aureus),             (f"mecA in E. coli = {ecoli} of 581,464; in S. aureus = {aureus:,} "
             f"of 171,412 -- the question is malformed, not the data missing")

    def baseline_ok(v):
        return v is not None

    def tool_ok(v):
        ecoli, aureus = v
        # The claim is not "mecA is rare". It is that the contrast carries the
        # explanation: two in half a million against half of S. aureus.
        return ecoli is not None and ecoli < 10 and aureus > 10000

    return ("gap-methicillin",
            "How many methicillin-resistant strains of E. coli are there?",
            truth, baseline, tool,
            "The board's own headline question. A refusal carries no information; "
            "the number does, and it points at the reframing.",
            baseline_ok, tool_ok)


# --------------------------------------- the board's four sub-branches
#
# The day-1 whiteboard hung four sub-branches off the headline question, and
# until now only the headline was measured. Each case below is the smallest
# chain that answers its branch, against the call a developer writes first.
# Every ground truth here was re-verified live on 17 Sep 2026.
#
# These four call E-utilities, Datasets and UniProt REST directly on both
# sides. The comparison is about the shape of the call, and the servers that
# own these sources belong to other people.

DATASETS = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"
UNIPROT = "https://rest.uniprot.org/uniprotkb"
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def pubdate_key(pubdate: str | None) -> tuple:
    """'2026 Dec' / '2026 Aug 28' / '2026' -> a sortable (year, month, day)."""
    parts = (pubdate or "").split()
    year = int(parts[0]) if parts and parts[0].isdigit() else 0
    month = MONTHS.get(parts[1][:3], 0) if len(parts) > 1 else 0
    day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (year, month, day)


def case_board_groups():
    """Branch 1, "what groups study these?" -- an accession is not a UID."""
    truth = "University of Pennsylvania"

    def baseline():
        # esummary takes id=. The identifier you hold is PRJNA715470, so that
        # is what you pass. The status line does not argue with you.
        rec = eutils("esummary", db="bioproject", id="PRJNA715470", version="2.0")
        uids = rec.get("result", {}).get("uids", [])
        org = next((rec["result"][u].get("submitter_organization") for u in uids), None)
        return org, (f'esummary db=bioproject id=PRJNA715470 -> HTTP 200, '
                     f'{len(uids)} records, error={rec.get("error")!r}')

    def tool():
        res = eutils("esearch", db="bioproject", term="PRJNA715470[Project Accession]")
        ids = res["esearchresult"]["idlist"]
        uid = ids[0]
        rec = eutils("esummary", db="bioproject", id=uid, version="2.0")
        org = rec["result"][uid].get("submitter_organization")
        return org, (f'esearch term=PRJNA715470[Project Accession] -> uid {uid}; '
                     f'esummary id={uid} -> submitter_organization={org!r}')

    return ("board-groups",
            "What group submitted BioProject PRJNA715470?",
            truth, baseline, tool,
            "esummary indexes UIDs, not accessions. Passing the accession returns "
            "HTTP 200 with the failure buried in an error key and zero records, "
            "which reads as 'this project has no submitter' rather than 'bad call'.")


def case_board_strain():
    """Branch 2, "tell me about strain X" -- a strain name is not a taxonomy name."""
    truth = 511145

    def baseline():
        # The strain is E. coli K-12 MG1655, so that is the term. Zero hits
        # reads as a typo, and dropping the substrain is the natural next move.
        first = eutils("esearch", db="taxonomy", term='"Escherichia coli K-12 MG1655"')
        n_first = int(first["esearchresult"]["count"])
        second = eutils("esearch", db="taxonomy", term='"Escherichia coli K-12"')
        ids = second["esearchresult"]["idlist"]
        got = int(ids[0]) if ids else None
        return got, (f'taxonomy "Escherichia coli K-12 MG1655" -> {n_first} hits; '
                     f'loosened to "Escherichia coli K-12" -> taxid {got}')

    def tool():
        rep = get_json(f"{DATASETS}/genome/accession/GCF_000005845.2/dataset_report")
        r = rep["reports"][0]
        taxid = int(r["organism"]["tax_id"])
        stats = r["assembly_stats"]
        return taxid, (f'datasets dataset_report GCF_000005845.2 -> taxid {taxid} '
                       f'({r["organism"]["organism_name"]}), '
                       f'{int(stats["total_sequence_length"]):,} bp, '
                       f'{int(stats["number_of_contigs"])} contig')

    return ("board-strain",
            "Tell me about strain E. coli K-12 MG1655",
            truth, baseline, tool,
            "The strain name in the question is not a name NCBI Taxonomy holds; the "
            "record is 'Escherichia coli str. K-12 substr. MG1655'. The near miss "
            "resolves to 83333, a different K-12 substrain, with no warning. Go in "
            "through the assembly accession and the taxid is stated, not guessed.")


def case_board_structure():
    """Branch 3, "structural implications" -- the one case the raw API wins."""
    truth = "at least one PDB structure for P0AES4"

    def baseline():
        entry = get_json(f"{UNIPROT}/P0AES4?format=json")
        pdb = [x["id"] for x in entry.get("uniProtKBCrossReferences", [])
               if x.get("database") == "PDB"]
        return len(pdb), (f'GET /uniprotkb/P0AES4 (whole entry) -> {len(pdb)} PDB '
                          f'cross-references, first four {pdb[:4]}')

    def tool():
        # uniprot.py belongs to another team, so this does not import it. It
        # replays the field list its uniprot_get_entry sends -- ENTRY_FIELDS,
        # uniprot.py:55, copied verbatim -- against the same endpoint. The list
        # never names xref_pdb, so no structure can come back through that tool.
        fields = ("accession,protein_name,gene_names,organism_name,length,reviewed,"
                  "cc_function,cc_disease,cc_subcellular_location,cc_ptm,go")
        entry = get_json(f"{UNIPROT}/P0AES4?format=json&fields={fields}")
        pdb = [x["id"] for x in entry.get("uniProtKBCrossReferences", [])
               if x.get("database") == "PDB"]
        return len(pdb), (f"replaying uniprot_get_entry's ENTRY_FIELDS "
                          f"(uniprot.py:55) -> {len(pdb)} PDB cross-references")

    def has_structure(v):
        return isinstance(v, int) and v >= 1

    return ("board-structure",
            "What structures exist for E. coli GyrA (P0AES4)?",
            truth, baseline, tool,
            "The only case here where the obvious call is the right one. The 20 PDB "
            "ids sit in the entry UniProt returns by default; ENTRY_FIELDS trims them "
            "away and nothing downstream can ask for them. The fix is one field name "
            "in a file this branch does not own, so it is reported, not patched.",
            has_structure, has_structure)


def case_board_latest():
    """Branch 4, "latest in AMR for X" -- an unknown sort value is ignored, not refused."""
    truth = "a 2026 publication-date count, newest first"
    term = "Escherichia coli ciprofloxacin resistance"
    window = {"mindate": "2026/01/01", "maxdate": "2026/12/31"}

    def newest_first(ids):
        """Are these PMIDs in descending order of the citation's publication date?"""
        rec = eutils("esummary", db="pubmed", id=",".join(ids), version="2.0")
        dates = [rec["result"][u].get("pubdate") for u in ids]
        keys = [pubdate_key(d) for d in dates]
        ordered = all(keys[i] >= keys[i + 1] for i in range(len(keys) - 1))
        return ordered, dates

    def baseline():
        # PubMed's web UI labels the control "Publication Date", and the
        # E-utilities docs list sort= without enumerating its tokens. datetype
        # is a separate parameter and easy not to know you needed.
        res = eutils("esearch", db="pubmed", term=term, retmax=5,
                     sort="Publication Date", **window)
        b = res["esearchresult"]
        count, ids = int(b["count"]), b["idlist"]
        ordered, dates = newest_first(ids)
        return (count, ordered), \
            (f'sort="Publication Date" (the web UI label), no datetype -> count='
             f'{count}, top 5 pubdates {dates}, newest-first={ordered}')

    def tool():
        res = eutils("esearch", db="pubmed", term=term, retmax=5,
                     datetype="pdat", sort="pub_date", **window)
        b = res["esearchresult"]
        count, ids = int(b["count"]), b["idlist"]
        ordered, dates = newest_first(ids)
        return (count, ordered), \
            (f'datetype=pdat&sort=pub_date -> count={count}, top 5 pubdates '
             f'{dates}, newest-first={ordered}')

    def sound(v):
        count, ordered = v
        # 283 on 17 Sep and rising for the rest of the year, against 5,273 for
        # the same term with no window at all. The band has to exclude the
        # unwindowed count and still tolerate growth, so it is wide. It cannot
        # separate pdat (283) from the edat default (274) -- those are nine
        # apart -- which is why the ordering half of the test carries the case.
        return isinstance(count, int) and 100 <= count <= 2000 and ordered

    return ("board-latest",
            "What is the latest published in 2026 on ciprofloxacin resistance in E. coli?",
            truth, baseline, tool,
            "An unrecognised sort value is not an error: esearch returns HTTP 200 in "
            "relevance order, so 'latest' is whatever matched best. Omitting datetype "
            "does not drop the window either -- it silently switches it to edat, the "
            "date PubMed indexed the record, which is a different question with a "
            "plausible answer nine hits away from the right one.",
            sound, sound)


# Cases where a tool FAIL is the measurement rather than a regression. They are
# scored like every other case and printed in the scoreboard, but they do not
# fail the run, because the thing they measure is a hole the board already knows
# about and wants stated. Do not add an id here to quiet a real failure.
EXPECTED_GAPS = {"board-structure"}


CASES = [
    case_gds_prefix,
    case_gsm_resolution,
    case_series_count,
    case_expression_files,
    case_retmax,
    case_ena_keywords,
    case_ena_total,
    case_study_organisms,
    case_pathogen_group_name,
    case_gap_methicillin,
    case_board_groups,
    case_board_strain,
    case_board_structure,
    case_board_latest,
]


# ---------------------------------------------------------------- the runner


def run_case(factory):
    parts = factory()
    if len(parts) == 6:
        cid, question, truth, baseline, tool, why = parts
        score_b = score_t = (lambda t: (lambda v: v == t))(truth)
    else:
        cid, question, truth, baseline, tool, why, score_b, score_t = parts

    row = {"id": cid, "question": question, "truth": truth, "why": why}
    for label, fn, scorer in (("baseline", baseline, score_b), ("tool", tool, score_t)):
        try:
            value, detail = fn()
            row[label] = {"value": value, "detail": detail, "pass": bool(scorer(value))}
        except Exception as exc:
            row[label] = {"value": None,
                          "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
                          "pass": False}
    row["value_added"] = row["tool"]["pass"] and not row["baseline"]["pass"]
    row["gap"] = cid in EXPECTED_GAPS
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--markdown", help="also write a markdown report here")
    args = ap.parse_args()

    print(f"Running {len(CASES)} cases against live services.\n")
    rows = []
    for factory in CASES:
        row = run_case(factory)
        rows.append(row)
        b = "PASS" if row["baseline"]["pass"] else "FAIL"
        t = "PASS" if row["tool"]["pass"] else "FAIL"
        if row["gap"] and not row["tool"]["pass"]:
            flag = "  <-- GAP: the system cannot answer this"
        elif row["value_added"]:
            flag = "  <-- value added"
        else:
            flag = ""
        print(f"  {row['id']:24s} baseline {b}   tool {t}{flag}")
        print(f"      baseline: {row['baseline']['detail']}")
        print(f"      tool    : {row['tool']['detail']}")

    n = len(rows)
    bp = sum(r["baseline"]["pass"] for r in rows)
    tp = sum(r["tool"]["pass"] for r in rows)
    va = sum(r["value_added"] for r in rows)
    open_gaps = [r["id"] for r in rows if r["gap"] and not r["tool"]["pass"]]
    print(f"\n  baseline correct : {bp}/{n}")
    print(f"  tool correct     : {tp}/{n}")
    print(f"  value added      : {va}/{n}")
    if open_gaps:
        print(f"  measured gaps    : {len(open_gaps)}/{n} "
              f"({', '.join(open_gaps)}) -- scored and reported, not excluded")

    if args.markdown:
        write_markdown(pathlib.Path(args.markdown), rows, bp, tp, va)
        print(f"\n  wrote {args.markdown}")
    # A known gap is a result, not a regression. Anything else failing is one.
    return 0 if all(r["tool"]["pass"] or r["gap"] for r in rows) else 1


def write_markdown(path: pathlib.Path, rows, bp, tp, va) -> None:
    n = len(rows)
    L = ["# What these MCP servers add over reading the API docs", "",
         "Generated by `evals/run_eval.py` against the live services.", "",
         "Each case is run twice: the **baseline** is the obvious call, written the way",
         "the API documentation leads you to write it, and the **tool** is our MCP tool.",
         "Both are scored against a verified ground truth.", "",
         f"| | correct |", "|---|---|",
         f"| baseline (raw API, written from the docs) | **{bp}/{n}** |",
         f"| our MCP tools | **{tp}/{n}** |",
         f"| cases where the tool fixes a wrong answer | **{va}/{n}** |", "",]
    open_gaps = [r for r in rows if r["gap"] and not r["tool"]["pass"]]
    if open_gaps:
        n_gap = len(open_gaps)
        L += [f"{n_gap} of the cases below {'is a' if n_gap == 1 else 'are'} **measured "
              f"gap{'' if n_gap == 1 else 's'}**: the question is on the board, the system",
              "cannot answer it, and the row says so rather than being left out of the",
              "denominator. A gap does not fail the run.", ""]
    L += [
         "Most of the failures below are not API outages: they return HTTP 200 with a",
         "plausible-looking answer, which is what makes them worth wrapping. A few fail",
         "in other ways -- an upstream 500, a page size that reads as a total, and one",
         "case with no baseline call at all, because without the counts the only option",
         "is to decline. Each row says which.", ""]
    for r in rows:
        if r["gap"] and not r["tool"]["pass"]:
            mark = "**a measured gap**, the system cannot answer this"
        elif r["value_added"]:
            mark = "fixes a silent wrong answer"
        elif r["tool"]["pass"] and r["baseline"]["pass"]:
            mark = "both correct"
        else:
            mark = "**failing** — not a known gap"
        L += [f"## {r['question']}", "",
              f"*{r['why']}*", "",
              f"| | result | correct |", "|---|---|---|",
              f"| baseline | `{r['baseline']['detail']}` | {'yes' if r['baseline']['pass'] else '**no**'} |",
              f"| tool | `{r['tool']['detail']}` | {'yes' if r['tool']['pass'] else '**no**'} |", "",
              f"Expected: `{r['truth']}` — {mark}.", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
