"""Offline tests for the BRC Analytics complement server. No network access.

This server exists only to close gaps in BRC's own public MCP server, so most of
these tests pin the specific behaviour that differs from it. Each gap was
reproduced live on 2026-09-17; see `evals/REPORT.md`.
"""

from __future__ import annotations

import pytest
from conftest import ena_row

from mcp.server.fastmcp.exceptions import ToolError


# ------------------------------------------------- ENA gives unusable FTP paths


def test_scheme_less_ftp_paths_become_usable_urls(brc):
    """ENA returns `ftp.sra.ebi.ac.uk/vol1/...` with no scheme at all.

    That is not a URL any client can follow. The same host serves the same paths
    over TLS, verified with a range request on 2026-09-17.
    """
    urls = brc._fastq_https(
        "ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_1.fastq.gz;"
        "ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_2.fastq.gz"
    )
    assert urls == [
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_1.fastq.gz",
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR1/ERR1_2.fastq.gz",
    ]


def test_an_already_schemed_path_is_not_double_prefixed(brc):
    assert brc._fastq_https("ftp://ftp.sra.ebi.ac.uk/x.gz") == \
        ["https://ftp.sra.ebi.ac.uk/x.gz"]
    assert brc._fastq_https("https://ftp.sra.ebi.ac.uk/x.gz") == \
        ["https://ftp.sra.ebi.ac.uk/x.gz"]


def test_absent_fastq_is_an_empty_list_not_a_broken_url(brc):
    assert brc._fastq_https("") == []
    assert brc._fastq_https(None) == []


# --------------------------------------------- the query that BRC gets wrong


def test_every_value_is_quoted(brc):
    """The whole reason this tool exists.

    BRC's own search_ena_keywords builds `scientific_name=Escherichia coli AND
    antimicrobial resistance` with no quotes, and ENA answers HTTP 400. Quoting
    the value returns 200.
    """
    q = brc._ena_query("Escherichia coli", None, "RNA-Seq", None)
    assert 'scientific_name="Escherichia coli"' in q
    assert 'library_strategy="RNA-Seq"' in q


def test_taxonomy_id_uses_the_taxon_operator(brc):
    q = brc._ena_query(None, "562", None, None)
    assert q == "tax_eq(562)"


def test_title_search_is_wrapped_in_wildcards(brc):
    q = brc._ena_query(None, "562", None, "resistance")
    assert 'study_title="*resistance*"' in q


def test_a_non_numeric_taxid_is_refused(brc):
    with pytest.raises(ToolError) as exc:
        brc._ena_query(None, "E. coli", None, None)
    assert "must be numeric" in str(exc.value)


def test_an_unfiltered_query_is_refused(brc):
    """Without a filter the query would match the whole archive."""
    with pytest.raises(ToolError) as exc:
        brc._ena_query(None, None, None, None)
    assert "at least one" in str(exc.value)


# ----------------------------------------------------- insecure-link flagging


def test_plain_http_links_are_reported_not_silently_rewritten(brc):
    """The BRC catalog hands out datacache.galaxyproject.org URLs over plain http.

    Rewriting them silently would hide a real downgrade from the caller, so they
    are collected and returned instead.
    """
    found: list = []
    brc._flag_insecure(
        {"a": "https://ok.example", "b": {"c": ["http://datacache.galaxyproject.org/x"]}},
        found,
    )
    assert found == ["http://datacache.galaxyproject.org/x"]


def test_runs_carry_insecure_links_only_when_there_are_some(brc, brc_record):
    brc_record([{"results": [ena_row("ERR1")]}, "42"])
    out = brc.brc_ena_runs("562", limit=1)
    assert "insecure_links" not in out


# -------------------------------------------------------------- brc_ena_runs


def test_runs_report_the_real_total_not_the_page_size(brc, brc_record):
    """BRC's search_ena reports 50 with has_more and never a total.

    50 is a page size. Reporting it as the answer is wrong by four orders of
    magnitude: the real figure for taxid 562 is 551,679.
    """
    brc_record([{"results": [ena_row("ERR1"), ena_row("ERR2")]}, "551679"])
    out = brc.brc_ena_runs("562", limit=2)
    assert out["total_in_ena"] == 551679
    assert out["returned"] == 2


def test_runs_page_past_the_federated_fifty_row_cap(brc, brc_record):
    """The proxy takes a limit but no offset, so fetch through and slice."""
    rows = [ena_row(f"ERR{i}") for i in range(60)]
    recorder = brc_record([{"results": rows}, "551679"])
    out = brc.brc_ena_runs("562", limit=2, offset=55)
    assert recorder.calls[0].params["limit"] == 57      # offset + limit
    assert [r["run_accession"] for r in out["results"]] == ["ERR55", "ERR56"]
    assert out["offset"] == 55


def test_runs_clamp_the_limit_to_the_ena_ceiling(brc, brc_record):
    recorder = brc_record([{"results": []}, "0"])
    brc.brc_ena_runs("562", limit=99999)
    assert recorder.calls[0].params["limit"] == brc.MAX_LIMIT == 1000


def test_an_offset_past_the_ceiling_says_what_to_do_instead(brc, brc_record):
    brc_record()
    with pytest.raises(ToolError) as exc:
        brc.brc_ena_runs("562", limit=10, offset=5000)
    assert "brc_ena_search" in str(exc.value)


def test_runs_refuse_a_non_numeric_taxid(brc, brc_record):
    brc_record()
    with pytest.raises(ToolError):
        brc.brc_ena_runs("Escherichia coli")


def test_every_run_gets_download_links(brc, brc_record):
    brc_record([{"results": [ena_row("ERR1")]}, "1"])
    out = brc.brc_ena_runs("562", limit=1)
    assert out["results"][0]["fastq_https"][0].startswith("https://")


# ------------------------------------------------------------ brc_ena_search


def test_search_says_when_more_matched_than_it_returned(brc, brc_record):
    brc_record([[ena_row("ERR1")], "13838"])
    out = brc.brc_ena_search(organism="Escherichia coli", limit=1)
    assert out["total_matching"] == 13838
    assert "13838 runs match" in out["note"]


def test_search_echoes_the_query_it_actually_ran(brc, brc_record):
    brc_record([[ena_row("ERR1")], "5"])
    out = brc.brc_ena_search(organism="Escherichia coli", library_strategy="RNA-Seq")
    assert out["query"] == 'scientific_name="Escherichia coli" AND library_strategy="RNA-Seq"'
    assert out["api_call"]["params"]["query"] == out["query"]


# ------------------------------------------------------------- brc_ena_study


def test_a_project_label_is_not_its_runs_organism(brc, brc_record):
    """PRJNA715470 is labelled E. coli and holds runs from 13 distinct organisms."""
    runs = [ena_row("SRR1"), ena_row("SRR2", scientific_name="Citrobacter koseri"),
            ena_row("SRR3", scientific_name="Enterobacter cloacae")]
    brc_record([[{"study_accession": "PRJNA715470", "study_title": "AMR isolates"}], runs])
    out = brc.brc_ena_study("PRJNA715470")
    assert out["run_count"] == 3
    assert out["organisms_in_runs"] == [
        "Citrobacter koseri", "Enterobacter cloacae", "Escherichia coli"]
    assert "more than one organism" in out["note"]


def test_a_single_organism_study_carries_no_warning(brc, brc_record):
    brc_record([[{"study_accession": "PRJEB8667"}], [ena_row("ERR1")]])
    out = brc.brc_ena_study("PRJEB8667")
    assert "note" not in out


def test_accessions_are_normalised(brc, brc_record):
    recorder = brc_record([[{"study_accession": "PRJEB8667"}], [ena_row("ERR1")]])
    brc.brc_ena_study("  prjeb8667  ")
    assert recorder.calls[0].params["accession"] == "PRJEB8667"


def test_an_unknown_study_says_so_rather_than_returning_an_empty_success(brc, brc_record):
    brc_record([[], []])
    with pytest.raises(ToolError) as exc:
        brc.brc_ena_study("PRJNA000000")
    assert "may not be indexed yet" in str(exc.value)


def test_an_empty_accession_is_refused(brc, brc_record):
    brc_record()
    with pytest.raises(ToolError):
        brc.brc_ena_study("   ")


# ------------------------------------------------------- brc_federation_status


def test_status_reports_the_known_breakages(brc, brc_record):
    """An agent must be able to tell "BRC is down" from "BRC returned a bug"."""
    brc_record([{"version": "0.29.0"}, {"status": "healthy"}])
    out = brc.brc_federation_status()
    assert out["reachable"] is True
    tools = {lim.get("tool") or lim.get("endpoint") for lim in out["known_limitations"]}
    assert "search_ena_keywords" in tools
    assert "search_ena" in tools
    assert "/api/v1/ena/study/{accession}" in tools


def test_status_still_answers_when_brc_is_unreachable(brc, brc_record):
    brc_record(("gateway timeout", 504))
    out = brc.brc_federation_status()
    assert out["reachable"] is False
    assert "504" in out["error"]
    # The limitations are static knowledge, so they survive the outage.
    assert len(out["known_limitations"]) == 5


# ------------------------------------------------------------------ transport


def test_a_4xx_carries_the_body_into_the_error(brc, brc_record):
    brc_record(("Client error '400' for url ...", 400))
    with pytest.raises(ToolError) as exc:
        brc.brc_ena_runs("562", limit=1)
    assert "HTTP 400" in str(exc.value)
