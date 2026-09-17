"""Offline tests for the GEO MCP server. No network access.

Every case here is one of the traps that made this server worth writing: a call
that returns HTTP 200 and a plausible answer about the wrong thing. The
assertions are against behaviour observed live on 2026-09-17 and recorded in
`evals/REPORT.md`.
"""

from __future__ import annotations

import pytest
from conftest import geo_esearch, geo_esummary

from mcp.server.fastmcp.exceptions import ToolError


# --------------------------------------------------------- accessions to UIDs


@pytest.mark.parametrize(
    "accession,uid",
    [
        ("GSE309890", "200309890"),
        ("GSE1000", "200001000"),
        ("GSE53890", "200053890"),
        ("GPL24659", "100024659"),
        ("GPL96", "100000096"),
        ("GPL570", "100000570"),
        ("GSM9284462", "309284462"),
        ("GSM1000", "300001000"),
        # GDS is the exception: no type digit at all, so the UID is the bare
        # number. Every pair here was checked against esummary on 2026-09-17.
        ("GDS5163", "5163"),
        ("GDS5162", "5162"),
    ],
)
def test_accession_to_uid(geo, accession, uid):
    assert geo.accession_to_uid(accession) == uid


def test_accession_case_and_padding_are_normalised(geo):
    assert geo.accession_to_uid("gse309890") == "200309890"
    assert geo.accession_to_uid("GSE0000001000") == "200001000"


@pytest.mark.parametrize(
    "bad", ["", "GSE", "GSE0", "SRR12345", "12345", "GSE-1", "GSEabc"]
)
def test_malformed_accessions_are_refused(geo, bad):
    """A string that is not an accession must not be passed on as a UID.

    esummary does not error on a wrong UID -- it returns another record, or
    nothing, and both read as answers.
    """
    with pytest.raises(ToolError):
        geo.parse_accession(bad)


def test_numeric_input_passes_through_untouched(geo):
    """A bare number is what a GDS UID looks like, so it must not be rewritten."""
    uid, canonical = geo.resolve_uid("5163")
    assert (uid, canonical) == ("5163", None)


def test_resolve_accession_reports_per_item_errors(geo):
    out = geo.geo_resolve_accession("GSE309890 NOPE GDS5163")
    assert out["requested"] == 3 and out["resolved"] == 2
    bad = [r for r in out["results"] if r["uid"] is None]
    assert len(bad) == 1 and bad[0]["query"] == "NOPE"
    # The good ones survive a bad neighbour.
    assert {r["uid"] for r in out["results"] if r["uid"]} == {"200309890", "5163"}


def test_resolve_accession_costs_no_request(geo, geo_record):
    recorder = geo_record()
    geo.geo_resolve_accession("GSE309890 GPL24659 GSM9284462 GDS5163")
    assert recorder.calls == []


# ------------------------------------------------------------- the GDS trap


def test_the_gds_prefix_trap_is_never_walked_into(geo):
    """Measured: gds UID 100005163 is a real record -- GPL5163, an Affymetrix array.

    A caller who assumed GDS shares the platform prefix gets a microarray design
    returned as if it were a curated dataset, with no error anywhere. The
    arithmetic here must never produce that UID for a GDS accession.
    """
    assert geo.accession_to_uid("GDS5163") == "5163"
    assert geo.accession_to_uid("GDS5163") != "100005163"
    # And the UID that trap produces belongs to a different record type, which
    # the rule agrees with: 100005163 is what GPL5163 maps to.
    assert geo.accession_to_uid("GPL5163") == "100005163"


def test_a_raw_uid_is_returned_under_its_real_accession(geo, geo_record):
    """Passing the trap UID by hand must not be relabelled as what was asked for."""
    geo_record(geo_esummary("100005163", accession="GPL5163", entrytype="GPL",
                            title="[HG-U133] Affymetrix array"))
    out = geo.geo_series("100005163", list_files=False)
    assert out["record"]["accession"] == "GPL5163"
    assert out["record"]["entry_type"] == "GPL"


def test_series_refuses_a_record_for_another_accession(geo, geo_record):
    geo_record(geo_esummary("200309890", accession="GSE999999", entrytype="GSE"))
    with pytest.raises(ToolError) as exc:
        geo.geo_series("GSE309890", list_files=False)
    message = str(exc.value)
    assert "GSE309890" in message and "GSE999999" in message
    assert "do not trust" in message


def test_series_raises_when_nothing_comes_back(geo, geo_record):
    geo_record({"result": {"uids": []}})
    with pytest.raises(ToolError) as exc:
        geo.geo_series("GSE309890", list_files=False)
    assert "no record" in str(exc.value)


# ------------------------------------------------------- errors inside a 200


def test_error_key_in_a_200_body_raises(geo, geo_record):
    """E-utilities reports its own errors inside HTTP 200.

    Verified live: esearch db=nosuchdb returns 200 with
    {"esearchresult": {"ERROR": "Invalid db name specified: nosuchdb"}}.
    Checking the status code catches none of this.
    """
    geo_record({"esearchresult": {"ERROR": "Invalid db name specified: nosuchdb"}})
    with pytest.raises(ToolError) as exc:
        geo.geo_search(organism="Escherichia coli")
    assert "Invalid db name" in str(exc.value)


def test_non_json_body_raises_with_a_usable_excerpt(geo, geo_record):
    geo_record("<!DOCTYPE html><html>NCBI is down for maintenance</html>")
    with pytest.raises(ToolError) as exc:
        geo.geo_search(organism="Escherichia coli")
    assert "non-JSON" in str(exc.value)


# ------------------------------------------------------------------ searching


def test_search_always_sets_retmax_and_retmode(geo, geo_record):
    """Default retmax is 20 and truncates silently; default retmode is XML."""
    recorder = geo_record([geo_esearch(1929, ["200309890"]),
                           geo_esummary("200309890", accession="GSE309890",
                                        entrytype="GSE")])
    geo.geo_search(organism="Escherichia coli", entry_type="gse", max_results=5)
    params = recorder.calls[0].params
    assert params["retmax"] == 5
    assert params["retmode"] == "json"
    assert params["tool"] == "niaid-bionexus-p7"


def test_search_reports_the_total_separately_from_the_page(geo, geo_record):
    geo_record([geo_esearch(1929, ["200309890"]),
                geo_esummary("200309890", accession="GSE309890", entrytype="GSE")])
    out = geo.geo_search(organism="Escherichia coli", entry_type="gse", max_results=5)
    assert out["total_count"] == 1929
    assert out["returned"] == 1
    assert out["truncated"] is True


def test_search_surfaces_the_mesh_rewrite(geo, geo_record):
    """NCBI rewrites terms through MeSH without saying so; the count follows the rewrite."""
    rewritten = ('"Escherichia coli"[Organism] AND ("anti-bacterial agents"[MeSH Terms] '
                 'OR antibiotic[All Fields]) AND "gse"[Filter]')
    geo_record([geo_esearch(444, [], rewritten)])
    out = geo.geo_search(organism="Escherichia coli", term="antibiotic")
    assert out["query_translation"] == rewritten
    assert "query_translation_note" in out


def test_entry_type_filter_is_mandatory_and_validated(geo, geo_record):
    """gds holds four record types at once, so an unfiltered count answers nothing."""
    recorder = geo_record([geo_esearch(0, [])])
    geo.geo_search(organism="Escherichia coli", entry_type="gds")
    assert '"gds"[Filter]' in recorder.calls[0].params["term"]

    with pytest.raises(ToolError) as exc:
        geo.geo_search(organism="Escherichia coli", entry_type="series")
    assert "entry_type must be one of" in str(exc.value)


def test_search_needs_at_least_one_term(geo, geo_record):
    geo_record()
    with pytest.raises(ToolError):
        geo.geo_search()


def test_esummary_batches_are_capped(geo, geo_record):
    """A gds record inlines its abstract and sample list: measured 3.3 KB each."""
    uids = [str(200000000 + i) for i in range(50)]
    recorder = geo_record([geo_esearch(50, uids),
                           {"result": {"uids": []}}])
    geo.geo_search(organism="Escherichia coli", max_results=50)
    requested = recorder.calls[1].params["id"].split(",")
    assert len(requested) == geo.MAX_ESUMMARY_UIDS == 20


# ----------------------------------------------------------------- the FTP tree


@pytest.mark.parametrize(
    "accession,bucket",
    [
        ("GSE309890", "GSE309nnn"),
        ("GSE1000", "GSE1nnn"),
        ("GSE1", "GSEnnn"),          # under 1000 leaves nothing in front
        ("GPL24659", "GPL24nnn"),
        ("GSM9284462", "GSM9284nnn"),
        ("GDS5163", "GDS5nnn"),
    ],
)
def test_ftp_bucket_rule(geo, accession, bucket):
    assert bucket in geo.ftp_directory(accession)


def test_ftp_folder_per_record_type(geo):
    assert "/series/" in geo.ftp_directory("GSE309890")
    assert "/platforms/" in geo.ftp_directory("GPL24659")
    assert "/samples/" in geo.ftp_directory("GSM9284462")
    assert "/datasets/" in geo.ftp_directory("GDS5163")


def test_ftplink_is_rewritten_to_https(geo):
    assert geo.https_from_ftplink(
        "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE309nnn/GSE309890/"
    ) == "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE309nnn/GSE309890/"


def test_empty_ftplink_returns_none(geo):
    """A GSM record carries an empty ftplink, so the URL has to be constructed."""
    assert geo.https_from_ftplink("") is None
    assert geo.https_from_ftplink(None) is None


AUTOINDEX = """<html><head><title>Index of /geo/series/GSE309nnn/GSE309890/suppl</title></head>
<body><h1>Index of /geo/series/GSE309nnn/GSE309890/suppl</h1><pre>
<a href="/geo/series/GSE309nnn/GSE309890/">Parent Directory</a>                             -
<a href="GSE309890_FPKMs_allSamples.csv.gz">GSE309890_FPKMs_allSamples.csv.gz</a> 2025-11-14 11:10  510K
<a href="filelist.txt">filelist.txt</a>                      2025-11-14 11:10  1.2K
<a href="subdir/">subdir/</a>                           2025-11-14 11:10    -
</pre></body></html>"""


def test_autoindex_parsing(geo):
    entries = geo.parse_autoindex(AUTOINDEX, "https://ftp.ncbi.nlm.nih.gov/x/")
    names = [e["name"] for e in entries]
    assert "Parent Directory" not in names          # navigation, not content
    assert "GSE309890_FPKMs_allSamples.csv.gz" in names
    first = entries[0]
    assert first["size"] == "510K"
    assert first["last_modified"] == "2025-11-14 11:10"
    assert first["url"].endswith("GSE309890_FPKMs_allSamples.csv.gz")
    assert [e["name"] for e in entries if e["is_directory"]] == ["subdir/"]


def test_a_listing_we_cannot_read_raises_rather_than_reporting_no_files(geo, geo_record):
    """If NCBI restyles the index pages, the row pattern stops matching.

    Reporting that as zero files would be a silent wrong answer about every
    record, so the absence of the autoindex marker is an error instead.
    """
    geo_record("<html><body>Something else entirely</body></html>")
    with pytest.raises(ToolError) as exc:
        geo.list_ftp_directory("https://ftp.ncbi.nlm.nih.gov/geo/series/x/")
    assert "no files" in str(exc.value)


def test_missing_directory_is_an_answer_not_a_failure(geo, geo_record):
    """GSM9284462 has no FTP directory: its data is published at the Series level."""
    geo_record([geo_esummary("309284462", accession="GSM9284462", entrytype="GSM",
                             ftplink=""),
                ("not found", 404)])
    out = geo.geo_series("GSM9284462")
    assert out["supplementary_files"] == []
    assert "registered no files" in out["supplementary_files_note"]


def test_an_empty_directory_is_not_the_same_as_a_missing_one(geo, geo_record):
    empty = ("<html><title>Index of /x</title><pre>"
             '<a href="/parent/">Parent Directory</a></pre></html>')
    assert geo.list_ftp_directory.__doc__  # documented distinction
    geo_record(empty)
    assert geo.list_ftp_directory("https://ftp.ncbi.nlm.nih.gov/geo/series/x/") == []


# ----------------------------------------------------------------- the record


def test_absent_pubmed_link_is_labelled_not_left_blank(geo, geo_record):
    """An empty link means no link is registered, never that there is no data."""
    geo_record([geo_esummary("200309890", accession="GSE309890", entrytype="GSE",
                             pubmedids=[]),
                ("not found", 404)])
    out = geo.geo_series("GSE309890")
    assert out["record"]["pubmed_ids"] == []
    assert "no link is registered" in out["record"]["pubmed_note"]


def test_platform_is_returned_in_both_forms(geo, geo_record):
    """esummary gives the bare number; every other GEO surface uses the GPL form."""
    geo_record(geo_esummary("200309890", accession="GSE309890", entrytype="GSE",
                            gpl="24659"))
    out = geo.geo_series("GSE309890", list_files=False)
    assert out["record"]["platform"] == "GPL24659"


def test_a_short_sample_list_is_flagged(geo, geo_record):
    """esummary has been seen to report a count and list fewer."""
    geo_record(geo_esummary("200309890", accession="GSE309890", entrytype="GSE",
                            n_samples=6,
                            samples=[{"accession": "GSM1", "title": "a"}]))
    out = geo.geo_series("GSE309890", list_files=False)
    assert out["record"]["sample_count"] == 6
    assert "Treat the list as partial" in out["record"]["samples_note"]


def test_every_record_says_where_the_numbers_are(geo, geo_record):
    geo_record(geo_esummary("200309890", accession="GSE309890", entrytype="GSE"))
    out = geo.geo_series("GSE309890", list_files=False)
    assert "not in this record" in out["expression_values"]


# ------------------------------------------------------------------- throttling


def test_a_rate_limit_is_retried_not_reported_as_no_data(geo, geo_record, monkeypatch):
    """NCBI answers 429 when the shared per-IP ceiling is crossed.

    Turning that into a failed tool call reports a throttle as an absence of
    data, which is the failure this harness caught in its own first run.
    """
    monkeypatch.setattr(geo.time, "sleep", lambda _s: None)
    recorder = geo_record([
        ({"error": "API rate limit exceeded"}, 429, {"Retry-After": "1"}),
        geo_esummary("200309890", accession="GSE309890", entrytype="GSE"),
    ])
    out = geo.geo_series("GSE309890", list_files=False)
    assert out["record"]["accession"] == "GSE309890"
    assert len(recorder.calls) == 2


def test_a_persistent_rate_limit_eventually_raises_with_the_reason(geo, geo_record, monkeypatch):
    monkeypatch.setattr(geo.time, "sleep", lambda _s: None)
    geo_record(({"error": "API rate limit exceeded"}, 429, {}))
    with pytest.raises(ToolError) as exc:
        geo.geo_series("GSE309890", list_files=False)
    assert "429" in str(exc.value)
    assert "3 requests/second" in str(exc.value)
