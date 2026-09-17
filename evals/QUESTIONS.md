# Demo questions — routing lane

15 questions for the Project 7 demo, ordered single-source first. Each one gives the tool chain
with real argument names, what comes back, and the part that cannot be answered.

**Everything marked ✅ was run live on 2026-09-17 from this laptop**, read-only, NCBI paced at
0.45 s between calls with `tool=niaid-bionexus-p7`. Everything marked ⚠️ is unverified and says
why. Raw output is in `REPORT.md` and the session scratchpad; the numbers below are pasted from it.

Tool inventory: `ncbi` (8005, Jonathan Gunti's, 20 tools), `geo` (8007, 3 tools), `brc_analytics` (federated
remote, **12 tools ✅**), `brc_analytics_local` (8008, shipped: 4 tools), `uniprot` (8003),
`mygene` (8002), `myvariant` (8004), `pdn` (8001), `nde` (9 tools, **not wired into `chatbot.py`**
— see Q10), plus two remotes already in `MCP_SERVERS` that the briefing did not list and that I
checked live today: `string` (**17 tools ✅**, STRING Database MCP Server 2.14.7) and `expasy`
(**4 tools ✅**, SIB Swiss Institute of Bioinformatics MCP 1.26.0 — a SPARQL gateway, not a
database). Both are wired, so the model holds their tools whether or not a question needs them;
the router prompt gives each of them a line.

---

## Three corrections the questions below are built on

These came out of today's verification and they change the routing story. Detail and proof in
the router prompt §"Corrections"; summarised here because three questions depend on them.

1. **The board does hold AMR data, both genotype and phenotype.** NCBI Pathogen Detection —
   already wrapped in Jonathan's server — indexes 581,464 *E. coli* isolates with AMR genotypes,
   and its `AST_phenotypes` field carries measured susceptibility calls. The surveys' rule
   "resistance phenotype → neither source, route to BV-BRC" is now only half true. See Q9, Q14, Q15.
   **Re-verified independently 2026-09-17** against `pathogens-srv` — 581,464 distinct *E. coli*
   and Shigella isolates, 170,726 carrying `gyrA_S83L`, 75,487 carrying `blaCTX-M-15`.
2. **The NDE "lowercase" trap is not about case.** Capitalisation changes nothing. The trap is
   *field choice*: pathogens live in `infectiousAgent.name`, hosts in `species.name`.
3. **`mecA` in *E. coli* returns 2 isolates out of 581,464.** The headline question is answerable —
   as a number that proves the question is malformed. That is a better demo than declining. See Q14.
   **Re-verified independently 2026-09-17:** 2 distinct isolates, against 93,260 of 171,412 for
   *S. aureus*. Two in half a million is the noise floor, and saying so answers the question
   better than refusing it.

4. **A bug this turned up in `ncbi_pathogen_organisms`, for its author.** The service reports a
   `totalCount` that is twice the distinct isolate count for *E. coli* (1,162,675 vs 581,464) but
   exactly equal to it for *S. aureus* (171,412 vs 171,412). A tool that halves unconditionally
   under-reports *S. aureus* by half. Both measured 2026-09-17. Use the `target_acc` facet's
   `numBuckets`, never an arithmetic correction on `totalCount`.

---

# Single source

## Q1. "What does the *E. coli* GyrA protein do, and where are the fluoroquinolone-resistance mutations in it?"

**Route:** UniProt only.

| step | tool | arguments |
|---|---|---|
| 1 | `uniprot_search` | `query="gyrA"`, `organism="Escherichia coli"`, `reviewed_only=True`, `max_results=5` |
| 2 | `uniprot_get_entry` | `accession="P0AES4"` |

**Answer ✅** (verified against the REST endpoint the tool wraps, with the tool's own `fields` list):

```
GET https://rest.uniprot.org/uniprotkb/search
    ?query=gyrA AND organism_name:Escherichia coli AND reviewed:true&size=3
HTTP 200
  P0AES4 | gyrA | Escherichia coli (strain K12) | 875 aa
  P62554 | ccdB | Escherichia coli (strain K12) | 101 aa
  P0AFI2 | parC | Escherichia coli (strain K12) | 752 aa
```

Step 2 then returns the function, subcellular location, GO terms and any curated DISEASE comments.
Note that `parC` (P0AFI2) comes back in the same search — the other fluoroquinolone-resistance
locus, and the one Q9 counts mutations in.

**Honest failure:** UniProt will not tell you *where* the resistance mutations sit unless the
entry happens to carry them as sequence features — and `uniprot_get_entry` does not return
sequence features at all. Worse, its docstring promises "interactions" and an "AlphaFold
structure URL" in the Returns block and the function returns neither (`mcp_servers/uniprot.py`,
`uniprot_get_entry`) — a router reading the docstring will promise the user a field that never
arrives. The resistance-position question belongs to Pathogen Detection's mutation vocabulary
(Q6, `gyrA_S83L`) or to the literature, not to UniProt.

---

## Q2. "How many *E. coli* isolates has NCBI sequenced?"

**Route:** NCBI Pathogen Detection. Two calls on one server, because the organism name must be
confirmed before it is used.

| step | tool | arguments |
|---|---|---|
| 1 | `ncbi_pathogen_organisms` | `contains="coli"` |
| 2 | `ncbi_pathogen_isolate_count` | `organism="E.coli and Shigella"` |

**Answer ✅:** step 1 returns the group name `E.coli and Shigella` (106 groups exist in total);
step 2 returns **581,464 distinct isolates**, index rows 1,162,675.

```
GET https://www.ncbi.nlm.nih.gov/pathogens/pathogens-srv/?limit=0
    &facets=target_acc[||1|1]&fq=taxgroup_name==["E.coli and Shigella"]
    &action=retrieve&collection=isolates
HTTP 200   distinct isolates: 581464 | index rows: 1162675
```

**Honest failure:** the group is *E. coli **and Shigella***. There is no way to separate them with
this filter, so "581,464 *E. coli* isolates" is wrong and "581,464 isolates in the E. coli and
Shigella group" is right. Say the group name in the answer.

**Bug found ✅ — tell Jonathan.** `ncbi_pathogen_organisms` derives `approx_isolates` as
`count // 2` because the index was measured to double-index every isolate. That no longer holds
uniformly. Measured today: *S. aureus* facet count 171,412 and distinct isolates **171,412** —
a ratio of 1, not 2, so the tool would report 85,706 and be wrong by half. *E. coli* still doubles
(1,162,675 rows / 581,464 distinct). The fix is to stop deriving and let callers use
`ncbi_pathogen_isolate_count`, or to label the field as unreliable per organism.

---

## Q3. "Are there any *E. coli* expression studies about ciprofloxacin?"

**Route:** NCBI GEO. Nothing else on the board holds transcriptomics.

| step | tool | arguments |
|---|---|---|
| 1 | `geo_search` | `organism="Escherichia coli"`, `term="ciprofloxacin"`, `entry_type="gse"` |

**Answer ✅** (verified through the underlying E-utilities call; the tool itself had not landed in
the working tree when I checked):

```
GET .../esearch.fcgi?db=gds&term="Escherichia coli"[Organism] AND "gse"[Filter] AND ciprofloxacin
HTTP 200   count: 37   top uids: 200309890, 200276254, 200283264
querytranslation: "Escherichia coli"[Organism] AND "gse"[Filter]
  AND ("ciprofloxacin"[All Fields] OR "ciprofloxacin"[MeSH Terms] OR ciprofloxacin[All Fields])
```

37 Series, GSE309890 at rank 1 — an adaptive-evolution RNA-seq study of sertraline-driven
resistance in K-12 MG1655.

**Honest failure:** the count is a text-match count. The MeSH rewrite above is mild, but the same
query shape for "antimicrobial resistance" expands to `"drug resistance, microbial"[MeSH Terms]`
and returns 234 ✅ — those are not 234 AMR experiments. The router must surface
`query_translation` rather than quote the number bare. And GEO's API returns metadata only: the
actual FPKM values sit in a submitter-uploaded supplementary file
(`ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE309nnn/GSE309890/` ✅, note the `ftp://` scheme) with no
schema. No tool on this board parses it.

---

## Q4. "Which *E. coli* genomes can I analyse, and what can I run on them?"

**Route:** BRC Analytics. This is the one question it is built for.

| step | tool | arguments |
|---|---|---|
| 1 | `search_organisms` | `query="Escherichia coli"` |
| 2 | `get_assemblies` | `taxonomy_id="562"` |
| 3 | `get_compatible_workflows` | `ploidies=["HAPLOID"]`, `taxonomy_id="562"` |

**Answer ✅:**

```
get_assemblies({"taxonomy_id":"562"}) -> HTTP 200
{"count":2,"assemblies":[
  {"accession":"GCF_000005845.2","strain":"K-12 substr. MG1655","ncbiTaxonomyId":"511145",
   "level":"Complete Genome","ploidy":["HAPLOID"],"length":4641652,"gcPercent":51},
  {"accession":"GCF_000008865.2","strain":"Sakai substr. RIMD 0509952","ncbiTaxonomyId":"386585", ...}]}

get_compatible_workflows({"ploidies":["HAPLOID"],"taxonomy_id":"562"}) -> HTTP 200  {"count":17,...}
```

**Honest failure:** **two** assemblies. BRC Analytics is a curated analysis catalogue, not a strain
collection — NCBI has 452,563 *E. coli* assemblies. If the user wanted strain breadth the router
must say BRC is the wrong source and hand off to NCBI. Also note `ncbiTaxonomyId` on the assembly
is **511145**, the strain, not the 562 that was asked for.

---

## Q5. "How many H5N1 sequences were collected in each country last year?" — and its bacterial twin

**Route:** PDN (`lapis_*`). Included because the near-identical bacterial question must be declined,
and getting that wrong is the most likely live-demo mistake: the server is called "Pathogen Data
Network" and holds no bacteria.

| step | tool | arguments |
|---|---|---|
| 1 | `lapis_list_organisms` | — |
| 2 | `lapis_describe_organism` | `organism="h5n1"`, `field_search="country"` |
| 3 | `lapis_aggregate_samples` | `organism="h5n1"`, `group_by=["country"]`, `filters={...date range...}` |

**Answer ⚠️:** counts per country. Not called live — PDN is a local server and was not running.

**Honest failure, two of them.** First, the docstring on `lapis_aggregate_samples` says it plainly:
a sample count measures sequencing effort, not infection incidence, and must never be reported as
a case count. Second, ask the same question about *E. coli* and the correct answer is a refusal —
PDN covers Pathoplexus human viral pathogens, GenSpectrum influenza and dengue, and CoV-Spectrum
SARS-CoV-2. For bacterial geography the router goes to `ncbi_pathogen_isolates`, whose records
carry `geo_loc_name`.

---

## Q6. "Which AMR genes turn up most often in *E. coli*, and how many isolates carry blaCTX-M-15?"

**Route:** NCBI Pathogen Detection, two calls: vocabulary first, then count.

| step | tool | arguments |
|---|---|---|
| 1 | `ncbi_pathogen_amr_genes` | `organism="E.coli and Shigella"`, `contains="bla"` |
| 2 | `ncbi_pathogen_isolate_count` | `organism="E.coli and Shigella"`, `amr_genes="blaCTX-M-15"` |

**Answer ✅:** the E. coli vocabulary holds **7,611 distinct gene symbols**. Top by index rows:

| gene | index rows |
|---|---:|
| `blaEC` | 1,119,505 |
| `acrF` | 1,065,222 |
| `mdtM` | 868,024 |
| `sul2` | 396,464 |
| `gyrA_S83L` | 341,342 |
| `blaCTX-M-15` | 75,487 distinct isolates (150,926 raw index rows -- the service doubles E. coli) |
| `mcr-1.1` | 20,608 |

**Honest failure, three of them.** `index_rows` is a row count, not an isolate count, and the
doubling is not uniform (Q2) — only `ncbi_pathogen_isolate_count` gives a defensible figure.
The vocabulary mixes acquired genes (`blaCTX-M-15`) with point mutations (`gyrA_S83L`) and with
genes nearly every *E. coli* carries (`blaEC` at 1.1M rows is intrinsic, not an AMR finding).
And carrying a gene is not being resistant — see Q15.

---

# Cross-source

## Q7. "Which *E. coli* expression studies exist for ciprofloxacin resistance, and can I run AMR gene detection on the reference genome?" — NCBI ↔ BRC

The headline chain. Two servers, five calls, and every hop verified.

| step | server | tool | arguments |
|---|---|---|---|
| 1 | ncbi | `geo_search` | `organism="Escherichia coli"`, `term="ciprofloxacin"`, `entry_type="gse"` |
| 2 | ncbi | `geo_series` | `accession="GSE309890"` |
| 3 | brc_analytics | `get_assemblies` | `taxonomy_id="562"` |
| 4 | brc_analytics | `check_compatibility` | `iwc_id="amr_gene_detection-main"`, `accession="GCF_000005845.2"` |
| 5 | brc_analytics | `resolve_workflow_inputs` | same arguments |

**Answer ✅.** Step 1 gives 37 Series with GSE309890 first. Step 2 gives its taxon as
`Escherichia coli str. K-12 substr. MG1655`. Step 3 gives BRC's K-12 assembly as
`GCF_000005845.2`, `ncbiTaxonomyId` **511145** — the *same strain taxid*, so this is an exact
join, not a fuzzy one. Then:

```
check_compatibility({"iwc_id":"amr_gene_detection-main","accession":"GCF_000005845.2"})
HTTP 200  {"compatible":true,"workflow":"AMR Gene Detection","assembly":"GCF_000005845.2"}

resolve_workflow_inputs(same)
HTTP 200  {"workflow_name":"AMR Gene Detection",
  "trs_id":"#workflow/github.com/iwc-workflows/amr_gene_detection/main/versions/v1.1.7",
  "resolved":{"Input sequence fasta":
    "https://hgdownload.soe.ucsc.edu/hubs/GCF/000/005/845/GCF_000005845.2/GCF_000005845.2.fa.gz"},
  "unresolved":[],"compatible":true,"compatibility_issues":[]}
```

**Honest failure:** the two halves do not actually meet. The workflow runs on the *assembly*, not
on the GEO study's samples — it will report the AMR gene content of the stock K-12 reference,
which is the same answer regardless of which of the 37 studies you started from. And BRC's MCP
server is read-only: it resolves every input and then cannot launch anything. The answer ends at
"here is the TRS id and the resolved FASTA URL; a human starts the run at brc.usegalaxy.org."
Say that rather than implying the analysis happened.

---

## Q8. "I want the raw reads behind GSE309890 and I want to variant-call them against K-12." — NCBI ↔ NCBI ↔ BRC

| step | server | tool | arguments |
|---|---|---|---|
| 1 | ncbi | `geo_series` | `accession="GSE309890"` |
| 2 | ncbi | `ncbi_sra_runs_for_project` | `accession="PRJNA1363958"` |
| 3 | ncbi | `ncbi_sra_run_metadata` | `accessions=<the runs>`, `detail="summary"` |
| 4 | brc_analytics | `get_compatible_workflows` | `ploidies=["HAPLOID"]`, `taxonomy_id="562"` |

**Answer ✅.** The GEO record carries the crosswalk in a field people miss:

```
esummary db=gds id=200309890 -> HTTP 200
  accession = 'GSE309890'   gdstype = 'Expression profiling by high throughput sequencing'
  taxon = 'Escherichia coli str. K-12 substr. MG1655'   n_samples = 6
  bioproject = 'PRJNA1363958'   pubmedids = []   ftplink = 'ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE309nnn/GSE309890/'

esearch db=sra term=PRJNA1363958[BioProject] -> HTTP 200   count = 6
```

**Honest failure — and it is the best one in this document.** The variant-calling workflow that
step 4 offers declares its input requirement in the tool result:

```
{"iwcId":"haploid-variant-calling-wgs-pe-main","name":"Paired end variant calling in haploid system",
 "parameters":[{"key":"Paired Collection","variable":"SANGER_READ_RUN_PAIRED",
   "data_requirements":{"library_layout":"PAIRED","library_strategy":["WGS"]}}, ...]}
```

`library_strategy: ["WGS"]`. GSE309890 is an expression study, so its 6 runs are RNA-seq. The
honest answer is "these reads exist, here are their accessions, and they do **not** satisfy this
workflow's stated input requirement — you want WGS runs for that." A router that skips step 3 and
matches on organism alone will happily promise a run that would fail.

Second failure: `pubmedids` is `[]`, and `elink` from this Series to PubMed returns HTTP 200 with
no `linksetdbs` key at all ✅ — "no link registered", not "no paper exists". The router must not
report "this study has never been published".

---

## Q9. "How many *E. coli* isolates carry the gyrA S83L ciprofloxacin-resistance mutation, and could I detect that myself in the reference genome?" — NCBI ↔ BRC

| step | server | tool | arguments |
|---|---|---|---|
| 1 | ncbi | `ncbi_pathogen_amr_genes` | `organism="E.coli and Shigella"`, `contains="gyrA"` |
| 2 | ncbi | `ncbi_pathogen_isolate_count` | `organism="E.coli and Shigella"`, `amr_genes="gyrA_S83L"` |
| 3 | brc_analytics | `check_compatibility` | `iwc_id="amr_gene_detection-main"`, `accession="GCF_000005845.2"` |

**Answer ✅:**

```
step 1 -> gyrA_S83L 341,342 rows | gyrA_D87N 213,504 | gyrA_D87G 24,559 | gyrA_D87Y 12,003
         parC_S80I 249,629 | parC_E84V 58,217
step 2 -> distinct isolates: 170,726   index rows: 341,342
         solr: * -status:withdrawn AND taxgroup_name:("E.coli and Shigella")
               AND AMR_genotypes:("gyrA_S83L")
step 3 -> {"compatible":true,"workflow":"AMR Gene Detection", ...}
```

170,726 of 581,464 isolates — 29% — carry the canonical first-step fluoroquinolone-resistance
mutation. This pairs directly with Q3: 37 GEO studies ask *how* resistance emerges, Pathogen
Detection says *how often* the mutation is already out there.

**Honest failure:** the two numbers are not comparable and must not be presented as a before/after.
BRC's AMR Gene Detection workflow and Pathogen Detection's `AMR_genotypes` are different pipelines
with different vocabularies; running the workflow on K-12 will not reproduce the 170,726 figure or
even necessarily use the same gene names. And `gyrA_S83L` is a point mutation — whether the
workflow reports point mutations at all depends on its AMRFinderPlus configuration, which
`get_workflow_details` does not expose. Say "these are two independent measurements", not "verify".

---

## Q10. "Where is there data on *E. coli* antimicrobial resistance, who funded it, and give me the actual accessions." — NDE ↔ NCBI

The archetype the whole project exists for: NDE routes, NCBI fetches.

| step | server | tool | arguments |
|---|---|---|---|
| 1 | nde | `nde_facet_counts` | `fields="includedInDataCatalog.name,funding.funder.name"`, `query="antimicrobial resistance"`, `repository=None` |
| 2 | nde | `nde_search_datasets` | `pathogen="escherichia coli"`, `query="antimicrobial resistance"`, `repository="NCBI SRA"`, `size=10` |
| 3 | nde | `nde_get_record` | `record_id="ncbi_sra_srp379600"` → read `isBasedOn[]` for PRJNA/SRR/SRX |
| 4 | ncbi | `ncbi_sra_runs_for_project` | `accession=<the PRJNA from step 3>` |

**Answer ⚠️ at the tool layer, ✅ at the API layer.** I did not call the NDE MCP tools; I called the
API they wrap. `infectiousAgent.name:"escherichia coli"` returns **60,107** records on production.
The survey's 3,644-record E. coli + AMR slice facets to Figshare 1,640 · NCBI SRA 1,229 ·
NCBI BioProject 453 · NCBI GEO 114, with NIAID funding 44 and Wellcome 30.

**Honest failure, and it is a repo fact, not a data fact.** `nde` is **not in `MCP_SERVERS` in
`chatbot.py`**, and the server is stdio-only — the chatbot cannot reach it today. Until it is
wired, the router must say "the metadata catalogue that answers 'where is the data' is not
connected to this assistant" rather than improvising from NCBI. Second failure:
`funding.funder.name` is missing on 3,401 of the 3,644 records, so "who funded it" is answerable
for under 7% of them. Report the denominator.

---

## Q11. "What does BV-BRC have on *E. coli*, and can I analyse those genomes?" — NDE ↔ BRC

| step | server | tool | arguments |
|---|---|---|---|
| 1 | nde | `nde_list_repositories` | `name_contains="Bacterial"` |
| 2 | nde | `nde_search_datasets` | `query="Escherichia coli"`, `repository="Bacterial and Viral Bioinformatics Resource Center"` |
| 3 | brc_analytics | `get_assemblies` | `taxonomy_id="562"` |

**Answer ✅ at the API layer:**

```
staging q=includedInDataCatalog.name:"Bacterial and Viral Bioinformatics Resource Center"
  HTTP 200  total = 118625
production, identical query
  HTTP 200  total = 0
```

**Honest failure — two traps in one question.**

First, **production returns a clean zero and no error.** BV-BRC and IEDB are crawled but not
promoted to production. A router that queries prod concludes "NDE does not index BV-BRC", which is
wrong. The fix is not a prompt rule: `nde_mcp/client.py:64` reads the host from the `NDE_API_URL`
environment variable at startup and there is **no per-call host argument**, so one running instance
can only ever see one host. Either run a second instance pointed at staging, or add the `host`
argument the survey's wrapping plan proposed. Until then the honest answer names the limitation.

Second, **BV-BRC and BRC Analytics are different resources** that share four letters. BV-BRC holds
pathogen genomes and AMR phenotypes; BRC Analytics holds 2 *E. coli* assemblies and Galaxy
workflows. The router must not treat one as a source for the other. This is the single most likely
confusion in a live demo with this tool board.

---

## Q12. "Give me the *E. coli* gyrA gene record and its protein sequence." — NCBI ↔ MyGene ↔ UniProt

A pure identifier-crosswalk question, and the only one that exercises MyGene.

| step | server | tool | arguments |
|---|---|---|---|
| 1 | ncbi | `ncbi_taxonomy_lookup` | `query="Escherichia coli"` |
| 2 | mygene | `mygene_search_genes` | `q="gyrA"`, `species="511145"`, `fields="symbol,name,entrezgene,uniprot"` |
| 3 | mygene | `mygene_map_ids` | `ids=["gyrA"]`, `from_type="symbol"`, `species="511145"`, `fields="uniprot"` |
| 4 | uniprot | `uniprot_get_sequence` | `accession=<the UniProt accession from step 3>` |

**Answer ⚠️:** the gene record, and the amino-acid sequence. Not called live; MyGene and UniProt
are local servers and were not running.

**Honest failure:** step 1 is not optional and is where this goes wrong. `ncbi_taxonomy_lookup`
returns **562** for "Escherichia coli", but the gene records and the BRC assembly are under
**511145** (K-12 MG1655). Passing 562 to MyGene returns the wrong set or nothing. There is no
automatic Entrez-UID → UniProt-accession crosswalk anywhere on this board — MyGene is the only
bridge, and if its `uniprot` field is absent for this gene the chain simply ends. Say that instead
of guessing an accession.

---

# Questions this board cannot answer

## Q13. "How many methicillin-resistant *E. coli* strains are there?"

**This is the team's headline example, and the honest answer is better than a refusal.**

The question is malformed, for three separate reasons, and the router should say all three and
then give the numbers that demonstrate it.

1. **Methicillin resistance is a staphylococcal concept.** It is mediated by `mecA`/`mecC`, which
   encode an alternative penicillin-binding protein. *E. coli* beta-lactam resistance runs through
   entirely different genes (`blaEC`, `blaCTX-M`, carbapenemases). "Methicillin-resistant E. coli"
   is not a recognised category.
2. **No source on the board holds curated resistance *phenotypes* per strain.** See Q14.
3. **"Strains" is not what any of these sources count.** They count isolates, assemblies, BioSamples
   and datasets, and those are four different denominators.

**What the board can return ✅, and it settles the point:**

```
fq=taxgroup_name==["E.coli and Shigella"] and AMR_genotypes==["mecA"]
  -> distinct isolates: 2      index rows: 4
fq=taxgroup_name==["E.coli and Shigella"]
  -> distinct isolates: 581,464
fq=taxgroup_name==["Staphylococcus aureus"] and AMR_genotypes==["mecA","mecC"]
  -> distinct isolates: 94,336   (of 171,412 S. aureus isolates)
```

**2 isolates out of 581,464 — 0.0003%.** That is the noise floor, and it is the evidence that the
category does not exist in *E. coli*. Meanwhile the same query against *S. aureus* returns 94,336,
which is MRSA, the question the user probably meant.

**The router's answer should be:** name the flaw, give the 2-out-of-581,464 number as proof, then
offer the three reframings — "MRSA?" (94,336 isolates carrying mecA or mecC ✅; mecA alone is 93,260), "ESBL-producing *E. coli*?"
(`blaCTX-M-15`, 75,487 isolates ✅), or "fluoroquinolone-resistant *E. coli*?" (`gyrA_S83L`,
170,726 isolates ✅) — and say that a curated phenotype call still needs BV-BRC or CARD.

---

## Q14. "Which *E. coli* isolates are clinically resistant to ciprofloxacin, and what are their MICs?"

**Cannot be answered through the current tools — but the data is one parameter away.**

This is a more interesting negative than the surveys expected. NCBI Pathogen Detection **does**
carry measured susceptibility. Verified today:

```
fq=taxgroup_name==["E.coli and Shigella"]  facets=AST_phenotypes[||1|30]
  -> 378 distinct phenotype values. gentamicin 19,922 rows · ampicillin 19,584 ·
     ciprofloxacin 18,072 · ciprofloxacin=S 13,126 · meropenem=S 12,806

fq=taxgroup_name==["E.coli and Shigella"] and AST_phenotypes==["ciprofloxacin=R"]
  -> distinct isolates: 1,548
fq=... and AST_phenotypes==["ciprofloxacin=S"]
  -> distinct isolates: 6,563
```

**1,548 ciprofloxacin-resistant *E. coli* isolates, measured, not inferred.** The service answers
the phenotype question directly.

**Why the board still cannot:** `_pathogen_filter` in `mcp_servers/ncbi/ncbi_mcp/server.py` builds
its `fq` from exactly five fields — `taxgroup_name`, `AMR_genotypes`, `host`, `isolation_source`,
`epi_type`. There is no `AST_phenotypes` argument on `ncbi_pathogen_isolate_count` or
`ncbi_pathogen_isolates`, and `ncbi_entrez_raw` is an E-utilities escape hatch that cannot reach
this service. So the number above is unreachable from the chatbot. **Recommendation: add an
`ast_phenotypes` parameter to `_pathogen_filter`.** It is a two-line change and it converts this
question from "cannot" to "answers".

**What remains genuinely absent even then:** the actual MIC values. Pathogen Detection stores the
S/I/R call, not the measurement. Sparse, too — only 9,036 of 581,464 isolates (1.6%) have any
ciprofloxacin AST result at all ✅, and 0 of the first 20 isolate records carried an
`AST_phenotypes` field ✅. For MIC distributions and breakpoint-curated phenotypes the router must
name **BV-BRC** (bv-brc.org) and **CARD**, neither of which is on this board.

---

## Q15. "What does the PBP2a structure look like, and how does it evade beta-lactams?"

**Cannot be answered. Decline and name the source.**

No structural biology anywhere on this board. NDE indexes 152,669 PDB records but they are
*metadata pointers* — title, DOI, landing page — not coordinates. UniProt's `uniprot_get_entry`
would be the closest thing and, as noted in Q1, its implementation does not return the AlphaFold
URL its docstring promises. Nothing on the board can compute or interpret a structure.

**Route to:** RCSB PDB and the AlphaFold Protein Structure Database.

**Why this question is in the demo:** it is the clean case where the right behaviour is a one-line
refusal plus a named alternative. If the router instead calls `uniprot_search` and narrates the
function annotation as though it answered a structural question, the whole resource-selection
story falls apart. Worth showing the model getting this right.

---

## Coverage check

| requirement | met by |
|---|---|
| 10–15 questions, single-source → cross-source | 15, Q1–Q6 single, Q7–Q12 cross, Q13–Q15 negative |
| ≥4 genuinely multi-server | Q7, Q8, Q9, Q10, Q11, Q12 — six |
| ≥2 crossing NCBI ↔ BRC | Q7, Q8, Q9 — three |
| ≥2 the board cannot answer | Q13, Q14, Q15 — three |
| the methicillin example, with the reframing | Q13, with the 2-of-581,464 proof |

**Since verified:** the two GEO tools (`geo_search`, `geo_series` in `mcp_servers/geo.py`, 52 offline tests, and live in `run_eval.py`
had not landed in `mcp_servers/ncbi/ncbi_mcp/server.py` when I checked — I verified the E-utilities
calls behind them instead); the four `brc_analytics_local` tools; any call through the MCP layer of
the **local** servers (uniprot, mygene, myvariant, pdn, ncbi, nde were not running, so their
argument names come from the source files); and the chatbot end to end.

**Verified by a call I made myself today:** every NCBI E-utilities, NCBI Pathogen Detection, NDE
production, NDE staging and UniProt REST number above, and the BRC Analytics, STRING and ExPASy MCP
servers over their real MCP protocol.
