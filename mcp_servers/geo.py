"""GEO MCP Server

A remote MCP server exposing tools over NCBI's Gene Expression Omnibus:
expression studies, their samples and platforms, and the supplementary files
that hold the actual numbers.

GEO is the one source on this board with processed gene expression. It is
reached through a single Entrez database, `gds`, which holds four record types
at once -- Series (GSE), Platforms (GPL), Samples (GSM) and curated DataSets
(GDS) -- and that arrangement is the source of every trap here.

Two things will silently hand you the wrong record if you skip them:

1. A GEO accession is not an Entrez UID. The mapping is arithmetic: a type digit
   followed by the accession number padded to 8 digits, and NO digit at all for
   GDS. Guessing wrong does not error. Measured 2026-09-17: gds UID 100005163 is
   a real record, GPL5163, so treating GDS5163 as a platform hands you a
   microarray design labelled as a curated dataset.
2. Searching for the accession instead is worse. `gds` indexes the accession as
   free text inside every related record, so GSE309890 matches 8 records and
   GPL24659 matches 491 -- and for a GSM accession the FIRST hit is its Series.

Every tool here therefore derives the UID by arithmetic and then checks that the
record NCBI returned carries the accession that was asked for.

The processed values -- counts, FPKMs, normalised matrices -- are in no
E-utilities response at all. They exist only as files in GEO's FTP tree, a
different host with no API, so `geo_series` lists that directory.

Rate limit: NCBI allows 3 requests/second per IP without an API key, and that
ceiling is shared across every NCBI host, so the FTP listing draws on the same
budget as E-utilities.

Run over HTTP:  uv run mcp_servers/geo.py --port 8009
Run over stdio: uv run mcp_servers/geo.py --stdio
"""

import os
import re
import threading
import time
from urllib.parse import urlencode, urljoin

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

mcp = FastMCP(
    name="GEO MCP",
    dependencies=["mcp", "requests"],
    instructions=(
        "Query NCBI GEO for gene expression studies: find Series by organism and "
        "treatment, read one study's metadata and samples, and locate the "
        "supplementary files that hold the processed expression values. GEO is "
        "the only source here with expression data. Resolve GEO accessions with "
        "geo_resolve_accession rather than by searching for them."
    ),
    port=8009,   # 8007 went to the NDE server in PR #12
    streamable_http_path="/mcp-geo",
)

EUTILS_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
FTP_BASE_URL = "https://ftp.ncbi.nlm.nih.gov/geo/"
GEO_DB = "gds"

# NCBI asks that every program identify itself. A blocked IP is only unblocked
# for software that registered a tool name, so this is not decoration.
TOOL_NAME = "niaid-bionexus-p7"

# Optional, and worth setting on a shared network. NCBI's anonymous ceiling is
# 3 requests/second PER IP, counted across every NCBI host -- so at a codeathon
# where thirty people share one egress address, the budget is gone before this
# server sends anything. Measured on the venue network: HTTP 429 reading
# {"count": "4", "limit": "3"} while this process was pacing itself at 2/s.
# An API key raises the ceiling to 10/s AND meters per key rather than per IP,
# which is the part that actually solves it. Read from the environment only --
# no key is ever written to a file or a command line here.
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "federated-data-ecosystem-assistant "
        "(+https://github.com/NIAID-BRC-Codeathons/federated-data-ecosystem-assistant)"
    ),
}

# 3 requests/second without an API key, NCBI-wide -- and the ceiling is per IP,
# shared with everything else on this machine and network. 0.4s gives 2.5/s,
# which leaves nothing spare once anything else is talking to NCBI; measured a
# 429 reading 'count: 4, limit: 3' at that rate. 0.5s gives 2/s and a margin.
MIN_REQUEST_GAP = 0.5   # overridden below when an API key raises the ceiling

# NCBI answers 429 when the shared per-IP ceiling is crossed, and 5xx under load.
# Both are transient and both deserve a retry rather than a failed tool call.
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Measured 2026-09-17: 20 GSE records returned 67,426 bytes, about 3.3 KB each,
# because a Series record inlines its abstract and its whole sample list. NCBI
# accepts far more; this is the point at which the result stops being a sensible
# size to hand a model.
MAX_ESUMMARY_UIDS = 20

# The type digit that turns a GEO accession number into a gds UID. GDS maps to
# the empty string deliberately -- a curated DataSet UID carries no prefix, and
# inventing one lands on a real record of another type.
UID_PREFIX = {"GPL": "1", "GSE": "2", "GSM": "3", "GDS": ""}
UID_WIDTH = 8

# Which FTP folder each record type lives in, and which subdirectory of its
# record directory actually holds data files. All four verified 2026-09-17:
# GSE and GSM keep supplementary files in suppl/; GPL and GDS have no suppl/
# directory at all and their content is the SOFT file in soft/.
FTP_FOLDER = {"GSE": "series", "GPL": "platforms", "GSM": "samples", "GDS": "datasets"}
FILE_SUBDIR = {"GSE": "suppl", "GSM": "suppl", "GPL": "soft", "GDS": "soft"}

ENTRY_TYPES = {
    "gse": "Series",
    "gpl": "Platform",
    "gsm": "Sample",
    "gds": "curated DataSet",
}

PROCESSED_VALUES_NOTE = (
    "GEO's API returns metadata only. The processed expression values (counts, "
    "FPKMs, normalised matrices) are not in this record and are not available "
    "from any E-utilities call. They exist only inside the files listed under "
    "supplementary_files, which must be downloaded from the GEO FTP tree."
)

NO_PUBMED_NOTE = (
    "No PubMed record is linked to this GEO record. That means no link is "
    "registered in GEO, typically an unpublished or not-yet-indexed study. It "
    "never means the study has no data."
)

_ACCESSION_RE = re.compile(r"^(GSE|GPL|GSM|GDS)0*(\d{1,8})$", re.IGNORECASE)

# Every Apache autoindex page titles itself this way. Its absence from a 200
# means the page is not the listing this module knows how to read.
AUTOINDEX_MARKER = "Index of"

# One row of an Apache autoindex. An href that is absolute or a mailto is
# navigation furniture, never content, so the pattern requires a relative href.
_AUTOINDEX_ROW = re.compile(
    r'<a href="(?P<href>[^"/][^":]*)">(?P<name>[^<]+)</a>'
    r"(?:\s*(?P<modified>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}))?"
    r"(?:\s+(?P<size>[\d.]+[KMGT]?|-))?"
)

# 10/s with a key, 3/s without. Stay under either with room to spare, because
# the anonymous budget is shared with every other process on this IP.
if NCBI_API_KEY:
    MIN_REQUEST_GAP = 0.15

_session = requests.Session()
_session.headers.update(HEADERS)
_rate_lock = threading.Lock()
_last_request_at = 0.0


# ---------------------------------------------------------------- transport


def _wait_turn() -> None:
    """Hold the shared gap between requests to any NCBI host."""
    global _last_request_at
    with _rate_lock:
        wait = MIN_REQUEST_GAP - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _get(url: str, params: dict | None = None, timeout: int = 60):
    """One rate-limited GET, retrying a throttle or a transient server error.

    NCBI answers HTTP 429 with {"error": "API rate limit exceeded"} when the
    3/second ceiling is crossed -- which happens whenever anything else on this
    machine is also talking to NCBI, since the ceiling is per IP and shared
    across every NCBI host. Failing the tool call on that would report a
    throttle as "no data", so back off and retry instead.
    """
    last = None
    for attempt in range(MAX_ATTEMPTS):
        _wait_turn()
        try:
            resp = _session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last = f"Could not reach {url}: {exc}"
            if attempt == MAX_ATTEMPTS - 1:
                raise ToolError(last) from exc
            time.sleep(BACKOFF_SECONDS * (attempt + 1))
            continue

        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
            # Respect Retry-After when NCBI sends one; otherwise widen the gap.
            delay = resp.headers.get("Retry-After")
            try:
                pause = float(delay) if delay else BACKOFF_SECONDS * (attempt + 1)
            except ValueError:
                pause = BACKOFF_SECONDS * (attempt + 1)
            time.sleep(min(pause, 10.0))
            continue

        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 429:
                hint = (" NCBI throttles at 3 requests/second per IP across all "
                        "its hosts; an API key raises that to 10.")
            raise ToolError(
                f"{url} returned HTTP {resp.status_code}: {resp.text[:300]}{hint}"
            )
        return resp
    raise ToolError(last or f"{url} failed after {MAX_ATTEMPTS} attempts.")


def _eutils(endpoint: str, params: dict) -> dict:
    """Call one E-utilities endpoint and return parsed JSON.

    E-utilities reports its own errors inside an HTTP 200 body, so checking the
    status code catches nothing. Verified: `esearch db=nosuchdb` returns 200 with
    `{"esearchresult": {"ERROR": "Invalid db name specified: nosuchdb"}}`. This
    raises on that, rather than returning a success-shaped empty result.
    """
    query = {"db": GEO_DB, "retmode": "json", "tool": TOOL_NAME, **params}
    if NCBI_API_KEY:
        query["api_key"] = NCBI_API_KEY
    resp = _get(f"{EUTILS_API}/{endpoint}.fcgi", query)
    try:
        data = resp.json()
    except ValueError as exc:
        raise ToolError(
            f"{endpoint} returned a non-JSON body (first 200 chars): {resp.text[:200]}"
        ) from exc

    for block in (data.get("esearchresult"), data.get("result"), data):
        if isinstance(block, dict) and block.get("ERROR"):
            raise ToolError(f"NCBI {endpoint} reported: {block['ERROR']}")
    if isinstance(data.get("error"), str):
        raise ToolError(f"NCBI {endpoint} reported: {data['error']}")
    return data


# ---------------------------------------------------------- accessions/UIDs


def parse_accession(value: str) -> tuple[str, int]:
    """Split a GEO accession into (PREFIX, number), or raise.

    Rejecting loudly is the point. An unrecognised string passed to esummary as a
    UID does not error -- it returns nothing, or another record entirely, and
    both look like answers.
    """
    text = (value or "").strip()
    match = _ACCESSION_RE.match(text)
    number = int(match.group(2)) if match else 0
    if not match or number <= 0:
        raise ToolError(
            f"{text!r} is not a GEO accession. Expected GSE, GPL, GSM or GDS "
            f"followed by digits, e.g. GSE309890, GPL24659, GSM9284462, GDS5163."
        )
    return match.group(1).upper(), number


def accession_to_uid(value: str) -> str:
    """Translate a GEO accession to its gds Entrez UID by the measured rule.

    Exact and free: no request is made.
    """
    prefix, number = parse_accession(value)
    padded = str(number).zfill(UID_WIDTH)
    if len(padded) > UID_WIDTH:
        # Unreachable through parse_accession's 8-digit cap, and kept anyway: if
        # GEO ever issues a 9-digit accession the rule stops holding, and this
        # must fail rather than build a UID for somebody else's record.
        raise ToolError(
            f"{value!r} has more than {UID_WIDTH} digits, which the GEO UID rule "
            f"does not cover. Look the UID up rather than deriving it."
        )
    return f"{UID_PREFIX[prefix]}{padded}".lstrip("0") or "0"


def resolve_uid(value: str) -> tuple[str, str | None]:
    """Accept an accession or a raw UID. Returns (uid, canonical accession or None).

    A bare run of digits passes through untouched: a GDS UID is a bare number, so
    there is no way to tell a UID the caller meant from one this module would
    have derived, and rewriting it would be guessing.
    """
    text = (value or "").strip()
    if not text:
        raise ToolError("No GEO accession or UID was provided.")
    if text.isdigit():
        return text, None
    prefix, number = parse_accession(text)
    canonical = f"{prefix}{number}"
    return accession_to_uid(canonical), canonical


def ftp_bucket(prefix: str, number: int) -> str:
    """The GSE309nnn-style directory that groups a thousand accessions.

    The last three digits become `nnn`; below 1000 nothing is left in front and
    the bucket is the bare prefix, e.g. GSEnnn for GSE1 (verified: that directory
    exists and holds GSE1).
    """
    return f"{prefix}{str(number)[:-3]}nnn"


def ftp_directory(accession: str) -> str:
    """The HTTPS URL of a GEO record's own FTP directory."""
    prefix, number = parse_accession(accession)
    bucket = ftp_bucket(prefix, number)
    return f"{FTP_BASE_URL}{FTP_FOLDER[prefix]}/{bucket}/{prefix}{number}/"


def https_from_ftplink(ftplink: str | None) -> str | None:
    """Rewrite the ftp:// URL in an esummary record as https, or return None.

    esummary hands back `ftp://ftp.ncbi.nlm.nih.gov/geo/series/...`. The same
    tree is served over HTTPS on the same host and path, which is what an HTTP
    client can actually read. GSE, GPL and GDS records carry this field; a GSM
    record's ftplink is the empty string, so for samples the URL is constructed.
    """
    text = (ftplink or "").strip()
    if not text:
        return None
    if text.startswith("ftp://"):
        text = "https://" + text[len("ftp://"):]
    if not text.startswith("https://"):
        return None
    return text if text.endswith("/") else text + "/"


def geo_browser_url(accession: str) -> str:
    """The human-facing GEO page, for a citation a person can open."""
    prefix, number = parse_accession(accession)
    return (
        "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?"
        + urlencode({"acc": f"{prefix}{number}"})
    )


def parse_autoindex(html: str, base_url: str) -> list:
    """Turn an Apache autoindex page into [{name, url, size, last_modified}].

    Subdirectories keep their trailing slash and are flagged, because GPL and GDS
    record directories contain only subdirectories and reporting them as files
    would promise downloads that are not there.
    """
    entries = []
    for match in _AUTOINDEX_ROW.finditer(html or ""):
        href = match.group("href")
        name = match.group("name").strip()
        if not href or href.startswith(("?", "#")) or name == "Parent Directory":
            continue
        entry = {
            "name": name,
            "url": urljoin(base_url, href),
            "is_directory": href.endswith("/"),
        }
        size = (match.group("size") or "").strip()
        if size and size != "-":
            entry["size"] = size
        modified = match.group("modified")
        if modified:
            entry["last_modified"] = modified.strip()
        entries.append(entry)
    return entries


def list_ftp_directory(url: str):
    """List one GEO FTP directory. Returns None when the directory does not exist.

    None is a real answer -- "this record registered no files" -- and is distinct
    from an empty list, which would mean the directory exists and is empty.
    Verified: GSM9284462 has no FTP directory at all, because its data is
    published at the Series level, while the bucket above it holds 275 siblings.
    """
    for attempt in range(MAX_ATTEMPTS):
        _wait_turn()
        try:
            resp = _session.get(url, timeout=60)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise ToolError(
                    f"Could not reach the GEO FTP site at {url}: {exc}") from exc
            time.sleep(BACKOFF_SECONDS * (attempt + 1))
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF_SECONDS * (attempt + 1))
            continue
        break

    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ToolError(f"GEO FTP returned HTTP {resp.status_code} for {url}.")

    body = resp.text
    # The one place a change at NCBI's end would look like a real answer. This is
    # an Apache autoindex page, not an API; if it is ever restyled the row
    # pattern stops matching and every record silently reports zero files.
    if AUTOINDEX_MARKER not in body:
        raise ToolError(
            f"{url} did not return a directory listing this server can read (no "
            f"{AUTOINDEX_MARKER!r} marker). NCBI may have changed the GEO FTP "
            f"index pages. Open the URL directly rather than reading this as "
            f"'no files'."
        )
    return parse_autoindex(body, url)


def _esummary_records(uids: list) -> list:
    """Fetch gds summary records for up to MAX_ESUMMARY_UIDS uids, in order."""
    if not uids:
        return []
    if len(uids) > MAX_ESUMMARY_UIDS:
        uids = uids[:MAX_ESUMMARY_UIDS]
    data = _eutils("esummary", {"id": ",".join(uids), "version": "2.0"})
    result = data.get("result") or {}
    return [
        result[uid]
        for uid in result.get("uids", [])
        if isinstance(result.get(uid), dict)
    ]


def _normalise(record: dict) -> dict:
    """Reshape one esummary db=gds record into stable, named fields.

    The raw field names are GEO-internal (`gdstype`, `pdat`, `n_samples`) and
    several are type-dependent empty strings. Fields that are absent stay absent
    rather than becoming empty, so "not recorded" does not read as "zero".
    """
    accession = (record.get("accession") or "").strip()
    entry_type = (record.get("entrytype") or "").strip().upper()

    out = {
        "accession": accession,
        "uid": str(record.get("uid") or ""),
        "entry_type": entry_type,
        "entry_type_label": ENTRY_TYPES.get(entry_type.lower(), entry_type),
        "title": (record.get("title") or "").strip(),
        "summary": (record.get("summary") or "").strip(),
        "taxon": (record.get("taxon") or "").strip(),
        "experiment_type": (record.get("gdstype") or "").strip(),
        "date_public": (record.get("pdat") or "").strip(),
    }

    platform = (record.get("gpl") or "").strip()
    if platform:
        # The summary gives the bare number; every other GEO surface uses the GPL
        # form, so both are reported rather than leaving the caller to guess.
        out["platform"] = f"GPL{platform}"
    if record.get("platformtitle"):
        out["platform_title"] = record["platformtitle"].strip()

    n_samples = record.get("n_samples")
    if n_samples not in (None, ""):
        out["sample_count"] = int(n_samples)

    samples = [
        {
            "accession": (s.get("accession") or "").strip(),
            "title": (s.get("title") or "").strip(),
        }
        for s in record.get("samples") or []
        if isinstance(s, dict)
    ]
    if samples:
        out["samples"] = samples
        if out.get("sample_count") not in (None, len(samples)):
            out["samples_note"] = (
                f"esummary reports {out['sample_count']} samples but listed "
                f"{len(samples)}. Treat the list as partial."
            )

    if record.get("bioproject"):
        out["bioproject"] = record["bioproject"].strip()

    pubmed_ids = [str(p) for p in record.get("pubmedids") or []]
    out["pubmed_ids"] = pubmed_ids
    if not pubmed_ids:
        out["pubmed_note"] = NO_PUBMED_NOTE

    ftplink = (record.get("ftplink") or "").strip()
    https = https_from_ftplink(ftplink) if ftplink else None
    if https:
        out["ftp_directory_url"] = https
    elif accession:
        out["ftp_directory_url"] = ftp_directory(accession)

    suppfile = (record.get("suppfile") or "").strip()
    if suppfile:
        # Formats only. Measured 'CSV' for GSE309890, whose sole supplementary
        # file is GSE309890_FPKMs_allSamples.csv.gz -- the name and URL are
        # nowhere in the record.
        out["supplementary_file_types"] = [
            part.strip() for part in suppfile.split(",") if part.strip()
        ]

    return out


# ---------------------------------------------------------------- GEO TOOLS


@mcp.tool()
def geo_search(
    organism: str = "",
    term: str = "",
    entry_type: str = "gse",
    max_results: int = 10,
) -> dict:
    """Search GEO for expression studies by organism and topic.

    GEO holds the processed gene expression data that no other source on this
    board has: what genes changed, under what treatment. Use it for questions
    about expression, transcriptomics, RNA-seq or microarray studies.

    The entry-type filter is not optional and defaults to Series. The `gds`
    database holds Series, Platforms, Samples and DataSets in one index, so an
    unfiltered count sums four different kinds of thing and answers no question.

    Args:
        organism: Scientific name, e.g. "Escherichia coli". Note this explodes
            down the taxonomy, so it includes every strain.
        term: Free text, e.g. "ciprofloxacin", "antibiotic resistance".
        entry_type: One of gse (Series, the usual unit of reuse), gpl (Platform),
            gsm (Sample), gds (curated DataSet, thinly populated and mostly old).
        max_results: Studies to return, 1-20. Default 10. The total count is
            reported separately and is not limited by this.

    Returns:
        The total count, NCBI's own `query_translation`, and the matching
        records with accession, title, taxon, experiment type, sample count and
        FTP directory. Read `query_translation` before trusting the count: NCBI
        silently rewrites terms through MeSH, so "antibiotic" becomes
        "anti-bacterial agents"[MeSH Terms] OR antibiotic[All Fields].

    Example questions:
        "What E. coli expression studies involve ciprofloxacin?"
        "Find RNA-seq studies of antibiotic resistance in E. coli"
        "How many GEO Series exist for Escherichia coli?"
    """
    key = (entry_type or "gse").strip().lower()
    if key not in ENTRY_TYPES:
        raise ToolError(
            f"entry_type must be one of {', '.join(sorted(ENTRY_TYPES))}, not "
            f"{entry_type!r}."
        )
    clauses = []
    if organism:
        clauses.append(f'"{organism}"[Organism]')
    if term:
        clauses.append(f"({term})")
    if not clauses:
        raise ToolError("Provide at least one of organism or term.")
    clauses.append(f'"{key}"[Filter]')
    query = " AND ".join(clauses)

    max_results = max(1, min(int(max_results), MAX_ESUMMARY_UIDS))
    # retmax is always explicit: the default is 20 and truncates silently, and
    # the count must be read separately from the returned id list.
    search = _eutils("esearch", {"term": query, "retmax": max_results})
    block = search.get("esearchresult") or {}
    count = int(block.get("count", 0))
    translation = block.get("querytranslation", query)
    uids = block.get("idlist") or []

    records = [_normalise(r) for r in _esummary_records(uids)] if uids else []

    result = {
        "query": query,
        "query_translation": translation,
        "entry_type": ENTRY_TYPES[key],
        "total_count": count,
        "returned": len(records),
        "results": records,
        "expression_values": PROCESSED_VALUES_NOTE,
        "api_call": {
            "url": f"{EUTILS_API}/esearch.fcgi",
            "params": {"db": GEO_DB, "term": query, "retmax": max_results},
        },
    }
    if translation.strip() != query.strip():
        result["query_translation_note"] = (
            "NCBI rewrote the query before running it, usually through MeSH. The "
            "count reflects the rewritten query, not the one requested."
        )
    if count > len(records):
        result["truncated"] = True
    return result


@mcp.tool()
def geo_series(accession: str, list_files: bool = True) -> dict:
    """Fetch one GEO record and locate the files holding its expression values.

    Accepts any GEO accession -- GSE (Series), GPL (Platform), GSM (Sample) or
    GDS (curated DataSet) -- or a raw gds UID.

    The record NCBI returns is checked against the accession requested. That
    check matters: the accession-to-UID rule differs for GDS, and getting it
    wrong returns a real record of another type rather than an error.

    Args:
        accession: e.g. "GSE309890", "GPL24659", "GSM9284462", "GDS5163".
        list_files: When true (default), also list the record's FTP directory,
            which is the only place the processed values exist. Set false to skip
            that request.

    Returns:
        The study metadata -- title, summary, taxon, experiment type, platform,
        samples, BioProject, PubMed links -- plus `supplementary_files` with the
        real download URLs and sizes. An absent PubMed link is reported as "no
        link registered", never as an error.

    Example questions:
        "Tell me about GSE309890"
        "Where are the expression values for GSE309890?"
        "What platform and how many samples does this study use?"
    """
    uid, canonical = resolve_uid(accession)
    records = _esummary_records([uid])
    if not records:
        raise ToolError(
            f"GEO returned no record for {accession!r} (gds UID {uid}). Check the "
            f"accession, or find one with geo_search."
        )
    record = _normalise(records[0])

    returned = record.get("accession", "")
    if canonical and returned and returned.upper() != canonical.upper():
        # The GDS trap, caught. gds UID 100005163 is a real record, GPL5163, so
        # a wrong prefix yields a plausible answer about the wrong thing.
        raise ToolError(
            f"Asked for {canonical} but gds UID {uid} returned {returned}. The "
            f"accession-to-UID rule did not hold for this record; do not trust "
            f"the result. Look the UID up with geo_search instead."
        )

    result = {
        "record": record,
        "geo_url": geo_browser_url(canonical) if canonical else None,
        "expression_values": PROCESSED_VALUES_NOTE,
        "api_call": {
            "url": f"{EUTILS_API}/esummary.fcgi",
            "params": {"db": GEO_DB, "id": uid, "version": "2.0"},
        },
    }

    if not list_files:
        return result

    prefix = (canonical or returned or "GSE")[:3].upper()
    base = record.get("ftp_directory_url") or (
        ftp_directory(canonical) if canonical else None
    )
    if not base:
        return result

    file_dir = urljoin(base, FILE_SUBDIR.get(prefix, "suppl") + "/")
    listing = list_ftp_directory(file_dir)
    result["supplementary_directory_url"] = file_dir
    if listing is None:
        result["supplementary_files"] = []
        result["supplementary_files_note"] = (
            f"This record registered no files: {file_dir} does not exist. For a "
            f"Sample that is normal -- its data is usually published at the "
            f"Series level. This is an answer, not a failure."
        )
    else:
        result["supplementary_files"] = listing
        result["supplementary_files_note"] = (
            f"{len(listing)} entries. These files are the only place the "
            f"processed expression values exist."
        )
    return result


@mcp.tool()
def geo_resolve_accession(accessions: str) -> dict:
    """Translate GEO accessions into the gds Entrez UIDs that raw E-utilities needs.

    Costs no NCBI request: the mapping is arithmetic. Use this instead of
    searching for the accession. A `gds` search matches the accession as free
    text inside every related record, so GSE309890 returns 8 hits, GPL24659
    returns 491, and for a GSM accession the first hit is its Series rather than
    the sample asked for.

    Args:
        accessions: One or more GEO accessions, space or comma separated,
            e.g. "GSE309890 GPL24659 GSM9284462 GDS5163".

    Returns:
        Each accession with its gds UID, FTP directory and GEO page URL. One bad
        accession does not lose the good ones; it comes back with its own error.

    Example questions:
        "What is the Entrez UID for GSE309890?"
        "Convert these GEO accessions to UIDs"
        "Where is the FTP directory for GSE309890?"
    """
    items = [p for p in re.split(r"[,\s]+", (accessions or "").strip()) if p]
    if not items:
        raise ToolError("Provide at least one GEO accession.")

    resolved = []
    for item in items:
        try:
            uid, canonical = resolve_uid(item)
        except ToolError as exc:
            resolved.append({"query": item, "uid": None, "error": str(exc)})
            continue
        entry = {"query": item, "uid": uid}
        if canonical:
            entry["accession"] = canonical
            entry["entry_type"] = canonical[:3]
            entry["ftp_directory_url"] = ftp_directory(canonical)
            entry["geo_url"] = geo_browser_url(canonical)
        else:
            entry["note"] = (
                "Already a numeric UID, passed through unchanged. A bare number "
                "is also what a GDS UID looks like, so it is not rewritten."
            )
        resolved.append(entry)

    ok = sum(1 for r in resolved if r.get("uid"))
    result = {
        "requested": len(items),
        "resolved": ok,
        "results": resolved,
        "rule": (
            "UID = type digit + accession number padded to 8 digits. GPL=1, "
            "GSE=2, GSM=3, and GDS takes no digit at all."
        ),
    }
    failed = [r["query"] for r in resolved if not r.get("uid")]
    if failed:
        result["note"] = f"Not valid GEO accessions: {', '.join(failed)}."
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NCBI GEO MCP server")
    parser.add_argument("--stdio", action="store_true", help="run over stdio")
    parser.add_argument("--port", type=int, default=8009, help="HTTP port")
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
    else:
        mcp.settings.port = args.port
        print(f"GEO MCP Server starting on http://localhost:{args.port}/mcp-geo ...")
        mcp.run(transport="streamable-http")
