"""BRC Analytics MCP Server (complement)

A local MCP server that fills the gaps in BRC Analytics' own public MCP server.

BRC Analytics publishes an MCP server at https://brc-analytics.org/api/v1/mcp/ with
12 read-only tools, and chatbot.py federates it directly. This file deliberately does
NOT reimplement those 12. It adds only what that server cannot do, each gap verified
live on 2026-09-17:

  * Their `search_ena` caps at 50 rows, reports `has_more: true`, and never returns a
    total. `brc_ena_runs` pages past the cap and `brc_ena_count` gives the real total.
  * Their `search_ena_keywords` is broken: it sends an unquoted `scientific_name=` value
    to ENA, which answers HTTP 400. Worse, the failure comes back as tool *text*, so a
    model sees a wall of error prose instead of an error. `brc_ena_search` quotes
    properly and works.
  * Their `/api/v1/ena/study/{accession}` answers HTTP 500. `brc_ena_study` goes to ENA
    directly instead.

Two things this server does that the federated one does not, on every response:
it turns ENA's scheme-less FTP paths into working https URLs, and it flags any
plain-http link rather than handing it back silently. The BRC catalog does publish
`datacache.galaxyproject.org` links over plain http.

Read and execute stay in different servers. Nothing here can start a Galaxy job.

Run over HTTP:  uv run mcp_servers/brc_analytics.py --port 8008
Run over stdio: uv run mcp_servers/brc_analytics.py --stdio
"""

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

mcp = FastMCP(
    name="BRC Analytics MCP",
    dependencies=["mcp", "requests"],
    instructions=(
        "Sequencing-data tools for BRC Analytics, complementing the public BRC "
        "Analytics MCP server rather than duplicating it. Use the federated BRC "
        "tools (search_organisms, get_assemblies, check_compatibility, ...) for the "
        "genome catalog and workflow compatibility. Use these tools for raw "
        "sequencing runs in ENA: real totals, paging past 50 results, keyword "
        "search, and study lookup."
    ),
    port=8008,
    streamable_http_path="/mcp-brc-analytics",
)

BRC_API = "https://brc-analytics.org/api/v1"
ENA_API = "https://www.ebi.ac.uk/ena/portal/api"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "federated-data-ecosystem-assistant "
        "(+https://github.com/NIAID-BRC-Codeathons/federated-data-ecosystem-assistant)"
    ),
}

# The ENA read_run fields the board questions actually need. Asking for everything
# makes the response large enough to crowd out the answer.
ENA_FIELDS = (
    "run_accession,experiment_accession,sample_accession,study_accession,"
    "tax_id,scientific_name,library_strategy,library_source,library_selection,"
    "library_layout,read_count,base_count,instrument_platform,instrument_model,"
    "first_public,last_updated,collection_date,study_title,sample_title,"
    "fastq_ftp,fastq_bytes"
)

MAX_LIMIT = 1000  # ENA's documented ceiling; asking for more is an error, not a bigger page.


# ---------------------------------------------------------------- helpers


def _get(url: str, params: dict | None = None, timeout: int = 60):
    """One HTTP GET with the shared headers, raising ToolError on anything but 2xx."""
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise ToolError(f"Request to {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(
            f"{url} returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp


def _fastq_https(fastq_ftp: str | None) -> list[str]:
    """Turn ENA's semicolon-separated, scheme-less FTP paths into https URLs.

    ENA returns `ftp.sra.ebi.ac.uk/vol1/fastq/...` with no scheme at all, which is
    not a URL a client can follow. The same host serves the same paths over https
    (verified with a range request, 2026-09-17), so this is the download link.
    """
    if not fastq_ftp:
        return []
    urls = []
    for part in str(fastq_ftp).split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith(("http://", "https://", "ftp://")):
            part = part.split("://", 1)[1]
        urls.append(f"https://{part}")
    return urls


def _flag_insecure(obj, found: list) -> None:
    """Walk a response and record every plain-http link, without mutating it.

    The BRC catalog hands out `datacache.galaxyproject.org` URLs over plain http.
    Rewriting them silently would hide a real downgrade from the caller, so this
    records them and the tool returns them under `insecure_links`.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            _flag_insecure(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _flag_insecure(item, found)
    elif isinstance(obj, str) and obj.startswith("http://"):
        found.append(obj)


def _shape_runs(rows: list) -> tuple[list, list]:
    """Add https download links to ENA rows and collect any insecure links found."""
    insecure: list = []
    _flag_insecure(rows, insecure)
    shaped = []
    for row in rows:
        row = dict(row)
        row["fastq_https"] = _fastq_https(row.get("fastq_ftp"))
        shaped.append(row)
    return shaped, sorted(set(insecure))


def _ena_query(
    organism: str | None,
    taxonomy_id: str | None,
    library_strategy: str | None,
    title_contains: str | None,
) -> str:
    """Build an ENA portal query, quoting every value.

    This is the whole reason `brc_ena_search` exists. BRC's own `search_ena_keywords`
    builds `scientific_name=Escherichia coli AND antimicrobial resistance` with no
    quotes, and ENA answers HTTP 400. Quoting the value returns 200 (both verified
    2026-09-17).
    """
    clauses = []
    if taxonomy_id:
        if not str(taxonomy_id).isdigit():
            raise ToolError(f"taxonomy_id must be numeric, got {taxonomy_id!r}")
        clauses.append(f"tax_eq({taxonomy_id})")
    if organism:
        clauses.append(f'scientific_name="{organism}"')
    if library_strategy:
        clauses.append(f'library_strategy="{library_strategy}"')
    if title_contains:
        clauses.append(f'study_title="*{title_contains}*"')
    if not clauses:
        raise ToolError(
            "Give at least one of organism, taxonomy_id, library_strategy or "
            "title_contains. An unfiltered ENA query would return the whole archive."
        )
    return " AND ".join(clauses)


def _ena_count(query: str) -> int | None:
    """The real number of matching runs. ENA's search endpoint never returns a total."""
    resp = _get(
        f"{ENA_API}/count",
        {"result": "read_run", "query": query},
    )
    text = resp.text.strip().splitlines()
    for line in reversed(text):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


# ---------------------------------------------------------------- BRC ANALYTICS TOOLS


@mcp.tool()
def brc_ena_runs(taxonomy_id: str, limit: int = 50, offset: int = 0) -> dict:
    """List raw sequencing runs in ENA for an organism, with real download links.

    Goes through BRC Analytics' own ENA proxy, so the answer is the same data their
    site shows. Use this instead of the federated `search_ena` tool whenever you need
    more than 50 runs or need to know how many exist in total: `search_ena` stops at
    50, says only `has_more: true`, and never reports a total.

    Args:
        taxonomy_id: NCBI Taxonomy id as a string, e.g. "562" for Escherichia coli.
            Note that a species taxid and a strain taxid are different: 562 is the
            species, 511145 is K-12 substr. MG1655.
        limit: Runs to return, 1-1000. Default 50.
        offset: Runs to skip, for paging. Default 0.

    Returns:
        Run records with study, sample and experiment accessions, library strategy,
        read counts, instrument, dates, plus `fastq_ftp` as ENA returns it and
        `fastq_https` as working download URLs. `total_in_ena` is the real count.

    Example questions:
        "What raw sequencing data exists for E. coli?"
        "Give me 200 E. coli runs with their FASTQ download links"
        "How many sequencing runs does ENA hold for taxid 562?"
    """
    if not str(taxonomy_id).isdigit():
        raise ToolError(f"taxonomy_id must be numeric, got {taxonomy_id!r}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    # BRC's proxy takes a limit but no offset, so fetch through the offset and slice.
    fetch = min(offset + limit, MAX_LIMIT)
    if offset >= MAX_LIMIT:
        raise ToolError(
            f"offset {offset} is past ENA's {MAX_LIMIT}-row ceiling for one call. "
            "Narrow the query with brc_ena_search instead of paging further."
        )
    resp = _get(f"{BRC_API}/ena/taxonomy/{taxonomy_id}", {"limit": fetch})
    payload = resp.json()
    rows = payload.get("results", [])[offset : offset + limit]
    shaped, insecure = _shape_runs(rows)

    result = {
        "taxonomy_id": str(taxonomy_id),
        "returned": len(shaped),
        "offset": offset,
        "total_in_ena": _ena_count(f"tax_eq({taxonomy_id})"),
        "results": shaped,
        "api_call": {
            "url": f"{BRC_API}/ena/taxonomy/{taxonomy_id}",
            "params": {"limit": fetch},
            "note": "BRC Analytics proxies ENA; the rows are ENA's.",
        },
    }
    if insecure:
        result["insecure_links"] = insecure
    return result


@mcp.tool()
def brc_ena_search(
    organism: str = "",
    taxonomy_id: str = "",
    library_strategy: str = "",
    title_contains: str = "",
    limit: int = 20,
) -> dict:
    """Search ENA for sequencing runs by organism, assay type or study title.

    Use this rather than the federated `search_ena_keywords` tool, which is currently
    broken: it sends an unquoted value to ENA and gets HTTP 400 back, then returns the
    error as ordinary text so it looks like an answer. This tool quotes every value.

    Filters are ANDed. Give at least one, or the query would match the whole archive.

    Args:
        organism: Scientific name, matched exactly, e.g. "Escherichia coli".
        taxonomy_id: NCBI Taxonomy id, e.g. "562". Matches the taxon and its
            descendants, so it is broader than `organism`.
        library_strategy: Assay type as ENA spells it, e.g. "WGS", "RNA-Seq",
            "AMPLICON". This is one of the few controlled fields in ENA.
        title_contains: Substring of the study title, e.g. "resistance". Wrapped in
            wildcards. Study titles are submitter free text, so treat a miss as
            "not worded that way", not "no such data".
        limit: Runs to return, 1-1000. Default 20.

    Returns:
        Matching runs with accessions, library metadata and `fastq_https` download
        links, plus `total_matching` (the real count, not the page size) and the exact
        ENA query that produced them.

    Example questions:
        "Find E. coli RNA-seq runs in ENA"
        "Which E. coli studies have resistance in the title?"
        "How many whole-genome sequencing runs exist for Escherichia coli?"
    """
    query = _ena_query(
        organism or None,
        taxonomy_id or None,
        library_strategy or None,
        title_contains or None,
    )
    limit = max(1, min(int(limit), MAX_LIMIT))
    resp = _get(
        f"{ENA_API}/search",
        {
            "result": "read_run",
            "query": query,
            "format": "json",
            "limit": limit,
            "fields": ENA_FIELDS,
        },
    )
    rows = resp.json() if resp.text.strip() else []
    shaped, insecure = _shape_runs(rows)
    total = _ena_count(query)

    result = {
        "query": query,
        "returned": len(shaped),
        "total_matching": total,
        "results": shaped,
        "api_call": {"url": f"{ENA_API}/search", "params": {"query": query, "limit": limit}},
    }
    if total is not None and total > len(shaped):
        result["note"] = (
            f"{total} runs match; {len(shaped)} returned. Raise limit or narrow the query."
        )
    if insecure:
        result["insecure_links"] = insecure
    return result


@mcp.tool()
def brc_ena_study(study_accession: str) -> dict:
    """Look up one ENA or SRA study and list its sequencing runs.

    Accepts either namespace for the same study: an ENA project (PRJEB…, PRJNA…) or a
    secondary study accession (ERP…, SRP…). BRC Analytics has an endpoint for this but
    it answers HTTP 500, so this tool goes to ENA directly.

    Args:
        study_accession: e.g. "PRJEB8667", "PRJNA715470", "ERP009685".

    Returns:
        Study title and first-public date, the run count, and the runs with their
        accessions, organism, library strategy and `fastq_https` download links.

    Example questions:
        "What is in BioProject PRJNA715470?"
        "List the sequencing runs for PRJEB8667 with download links"
        "Which organisms are actually in this E. coli project?"
    """
    accession = str(study_accession).strip().upper()
    if not accession:
        raise ToolError("study_accession is required.")

    study_resp = _get(
        f"{ENA_API}/filereport",
        {
            "accession": accession,
            "result": "study",
            "fields": "study_accession,secondary_study_accession,study_title,first_public,tax_id,scientific_name",
            "format": "json",
        },
    )
    study_rows = study_resp.json() if study_resp.text.strip() else []

    runs_resp = _get(
        f"{ENA_API}/filereport",
        {
            "accession": accession,
            "result": "read_run",
            "fields": ENA_FIELDS,
            "format": "json",
        },
    )
    runs = runs_resp.json() if runs_resp.text.strip() else []
    shaped, insecure = _shape_runs(runs)

    if not study_rows and not shaped:
        raise ToolError(
            f"ENA returned nothing for {accession}. Check the accession; a study "
            "registered in the last day or two may not be indexed yet."
        )

    organisms = sorted({r.get("scientific_name") for r in shaped if r.get("scientific_name")})
    result = {
        "study_accession": accession,
        "study": study_rows[0] if study_rows else None,
        "run_count": len(shaped),
        "organisms_in_runs": organisms,
        "results": shaped,
        "api_call": {"url": f"{ENA_API}/filereport", "params": {"accession": accession}},
    }
    if len(organisms) > 1:
        result["note"] = (
            "This study contains more than one organism. A project-level organism "
            "label is not the organism of every run in it."
        )
    if insecure:
        result["insecure_links"] = insecure
    return result


@mcp.tool()
def brc_federation_status() -> dict:
    """Check whether BRC Analytics' own MCP server is reachable, and what it cannot do.

    The chatbot federates BRC's public MCP server, so a demo depends on that server
    being up. Call this before relying on the federated BRC tools, or when one of them
    returns something that looks like an error message rather than data.

    Args:
        None.

    Returns:
        Live service version and health, plus the known limitations of the 12
        federated tools, so an agent can route around them instead of reporting an
        upstream bug as a scientific result.

    Example questions:
        "Is BRC Analytics up?"
        "Why did the BRC keyword search return an error?"
        "Can I trust the BRC ENA results?"
    """
    status: dict = {"mcp_url": "https://brc-analytics.org/api/v1/mcp/"}
    try:
        status["version"] = _get(f"{BRC_API}/version", timeout=20).json()
        status["health"] = _get(f"{BRC_API}/health", timeout=20).json()
        status["reachable"] = True
    except ToolError as exc:
        status["reachable"] = False
        status["error"] = str(exc)

    status["known_limitations"] = [
        {
            "tool": "search_ena_keywords",
            "problem": "Sends an unquoted scientific_name to ENA; ENA answers HTTP 400. "
                       "The failure is returned as tool text, so it can read like an answer.",
            "use_instead": "brc_ena_search",
            "verified": "2026-09-17",
        },
        {
            "tool": "search_ena",
            "problem": "Caps at 50 rows with has_more: true, and never reports a total.",
            "use_instead": "brc_ena_runs",
            "verified": "2026-09-17",
        },
        {
            "endpoint": "/api/v1/ena/study/{accession}",
            "problem": "Answers HTTP 500 for every accession tried.",
            "use_instead": "brc_ena_study",
            "verified": "2026-09-17",
        },
        {
            "tool": "all catalog tools",
            "problem": "The catalog holds no resistance phenotypes and only 2 "
                       "Escherichia coli assemblies (K-12 MG1655, O157:H7 Sakai). It "
                       "answers 'what can I compute, on which genome', not 'how many "
                       "resistant strains are there'.",
            "use_instead": "Say so plainly; route phenotype questions to BV-BRC or CARD.",
            "verified": "2026-09-17",
        },
        {
            "tool": "mcp endpoint itself",
            "problem": "https://brc-analytics.org/api/v1/mcp without the trailing slash "
                       "answers 307 to a plain http:// URL. The MCP client refuses an "
                       "https-to-http downgrade, so it surfaces as an HTTP error.",
            "use_instead": "Always call the slashed URL.",
            "verified": "2026-09-17",
        },
    ]
    return status


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BRC Analytics complement MCP server")
    parser.add_argument("--stdio", action="store_true", help="run over stdio")
    parser.add_argument("--port", type=int, default=8008, help="HTTP port")
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
    else:
        mcp.settings.port = args.port
        print(f"BRC Analytics MCP Server starting on http://localhost:{args.port}/mcp-brc-analytics ...")
        mcp.run(transport="streamable-http")
