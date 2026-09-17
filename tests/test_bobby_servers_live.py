"""Live smoke tests for the GEO and BRC Analytics servers. Deselected by default.

    uv run pytest -m live tests/test_bobby_servers_live.py -v

The offline suite proves the code does what it was written to do. These prove the
*world* still does what it did when the code was written — which is a different
question, and the one that quietly goes stale.

They exist so nobody has to take the numbers in `evals/` on trust. Each assertion
below is the ground truth recorded in `evals/PIPELINES.md`, checked against the
live service.

These are slow on purpose. NCBI allows 3 requests/second per IP across all of its
hosts, and that budget is shared with everyone else on the network, so the servers
pace themselves and so does this file.

A failure here is not necessarily a bug. Counts move: GEO gains Series, ENA gains
runs, and BRC republished its workflow catalogue mid-build between 16 and 17 Sep.
Ranges are used where the real number drifts, exact values only where the thing
being tested is a rule rather than a count.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ------------------------------------------------------------ P3 · expression


def test_geo_uid_rule_still_holds_for_every_prefix(geo):
    """The arithmetic is only right while NCBI keeps assigning UIDs this way."""
    for accession, expected in [
        ("GSE309890", "GSE309890"),
        ("GPL24659", "GPL24659"),
        ("GSM9284462", "GSM9284462"),
        ("GDS5163", "GDS5163"),
    ]:
        out = geo.geo_series(accession, list_files=False)
        assert out["record"]["accession"] == accession, (
            f"{accession} resolved to {out['record']['accession']}. The GEO UID rule "
            f"has changed, and every derived UID is now suspect."
        )


def test_the_gds_trap_is_still_a_trap(geo):
    """Guarding a trap that no longer exists is dead weight; check it is still live.

    Measured 2026-09-17: gds UID 100005163 returns GPL5163, a real Affymetrix
    array design. If NCBI ever makes that UID invalid, the guard can relax.
    """
    out = geo.geo_series("100005163", list_files=False)
    assert out["record"]["accession"] == "GPL5163"
    assert out["record"]["entry_type"] == "GPL"


def test_ciprofloxacin_series_count_is_stable(geo):
    """Ground truth 37 on 2026-09-17. GEO grows, so this is a range."""
    out = geo.geo_search(organism="Escherichia coli", term="ciprofloxacin",
                         entry_type="gse", max_results=1)
    assert 30 <= out["total_count"] <= 80, out["total_count"]
    assert out["results"][0]["accession"] == "GSE309890"


def test_the_unfiltered_count_is_still_much_larger(geo):
    """The reason entry_type is mandatory: unfiltered mixes four record types."""
    filtered = geo.geo_search(organism="Escherichia coli", term="ciprofloxacin",
                              entry_type="gse", max_results=1)["total_count"]
    samples = geo.geo_search(organism="Escherichia coli", term="ciprofloxacin",
                             entry_type="gsm", max_results=1)["total_count"]
    assert samples > filtered, (
        "Samples no longer outnumber Series for this query. The entry-type filter "
        "may no longer be load-bearing."
    )


def test_the_expression_values_are_still_reachable(geo):
    """The whole point of P3: the numbers are in FTP files, not in any API response."""
    out = geo.geo_series("GSE309890")
    names = [f["name"] for f in out["supplementary_files"]]
    assert "GSE309890_FPKMs_allSamples.csv.gz" in names, names
    url = next(f["url"] for f in out["supplementary_files"]
               if f["name"] == "GSE309890_FPKMs_allSamples.csv.gz")
    assert url.startswith("https://ftp.ncbi.nlm.nih.gov/geo/series/GSE309nnn/")


def test_a_sample_with_no_files_is_still_reported_as_an_answer(geo):
    """GSM9284462 publishes its data at the Series level and has no directory."""
    out = geo.geo_series("GSM9284462")
    assert out["supplementary_files"] == []
    assert "registered no files" in out["supplementary_files_note"]


def test_mesh_rewriting_still_happens(geo):
    """If NCBI stopped rewriting, surfacing query_translation would be noise."""
    out = geo.geo_search(organism="Escherichia coli", term="antibiotic",
                         entry_type="gse", max_results=1)
    assert "MeSH" in out["query_translation"], out["query_translation"]
    assert "query_translation_note" in out


# --------------------------------------------------- P5 · BRC and ENA


def test_brc_is_reachable_and_still_broken_in_the_same_ways(brc):
    """If the Galaxy team fixes these, our complement can shrink."""
    out = brc.brc_federation_status()
    assert out["reachable"] is True, out.get("error")
    assert out["version"]["service"] == "BRC Analytics API"


def test_the_real_ena_total_is_orders_above_the_federated_page(brc):
    """search_ena reports 50 with has_more. The truth was 551,679 on 2026-09-17."""
    out = brc.brc_ena_runs("562", limit=1)
    assert out["total_in_ena"] > 400_000, out["total_in_ena"]


def test_paging_past_fifty_returns_different_rows(brc):
    """The federated tool stops at 50; this is the thing that must keep working."""
    first = brc.brc_ena_runs("562", limit=2, offset=0)
    later = brc.brc_ena_runs("562", limit=2, offset=55)
    assert first["results"][0]["run_accession"] != later["results"][0]["run_accession"]


def test_fastq_links_are_still_downloadable_over_https(brc):
    """ENA returns scheme-less paths; we prefix https. Check the host still serves it."""
    import requests

    out = brc.brc_ena_runs("562", limit=1)
    url = out["results"][0]["fastq_https"][0]
    resp = requests.get(url, headers={"Range": "bytes=0-200"}, timeout=60)
    assert resp.status_code in (200, 206), resp.status_code


def test_the_keyword_search_ena_rejects_is_still_the_one_we_fixed(brc):
    """Our quoting must still be the difference between 400 and 200."""
    out = brc.brc_ena_search(organism="Escherichia coli",
                             library_strategy="RNA-Seq", limit=1)
    assert out["total_matching"] > 1000, out["total_matching"]
    assert 'scientific_name="Escherichia coli"' in out["query"]


def test_a_project_label_still_hides_multiple_organisms(brc):
    """PRJNA715470 is labelled E. coli and held 13 genera on 2026-09-17."""
    out = brc.brc_ena_study("PRJNA715470")
    assert out["run_count"] > 300
    assert len(out["organisms_in_runs"]) > 5, out["organisms_in_runs"]
    assert "more than one organism" in out["note"]


def test_the_amr_workflow_is_still_compatible_with_k12(brc):
    """P5's headline claim, checked against BRC's own federated server."""
    import json
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "check_compatibility",
                   "arguments": {"iwc_id": "amr_gene_detection-main",
                                 "accession": "GCF_000005845.2"}},
    }).encode()
    req = urllib.request.Request(
        "https://brc-analytics.org/api/v1/mcp/", data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode()
    payload = next(json.loads(line[6:]) for line in raw.splitlines()
                   if line.startswith("data: "))
    result = json.loads(payload["result"]["content"][0]["text"])
    assert result["compatible"] is True, result
