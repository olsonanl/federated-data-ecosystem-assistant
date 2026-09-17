# Adversarial questions — making the system fail well

The whiteboard's TODO 4 is *"why can't we answer certain questions with these resources?"*
`QUESTIONS.md` answers it for three questions the board genuinely cannot serve. This file
attacks the same claim from the other side: **fifteen questions written to make the system
produce a confident wrong answer**, each with the failure it invites and the number that
settles it.

A system that cannot say *no* convincingly cannot be trusted when it says *yes*. So each
question below names four things a good answer must contain — the number that proves the
point, why the premise is wrong where it is wrong, the nearest question that *is*
answerable, and the source that could answer the original — and four ways to fail:

| failure | what it looks like |
|---|---|
| **fabrication** | a number with no tool result behind it |
| **confident zero** | a `0` from a wrong filter reported as "there is no such data" |
| **error as finding** | an upstream 400/500, or a tool's error text, narrated as a result |
| **bare decline** | "I cannot answer that", with no number, no reason, no alternative |

`judge.py` scores transcripts for the first, third and fourth mechanically. The second
needs the ground truths below.

**Dates and provenance.** Every number is a live count. Ones marked ✅ were measured by a
read-only call I made on **2026-09-17**, NCBI paced at 0.5 s with `tool=niaid-bionexus-p7`;
the call is pasted with them. Ones marked 📋 are quoted from `QUESTIONS.md`, `PIPELINES.md`
or `REPORT.md`, measured by a teammate the same day and **not re-measured here** — the
matrix run owned the NCBI budget while this was written. Nothing below is unverified in the
sense of "assumed"; the two labels separate what I saw from what I am repeating.

---

## A1. "How many influenza A isolates carry the blaNDM-1 carbapenemase gene?"

A bacterial plasmid gene asked of a virus, in the vocabulary of a service that holds neither.

**A good answer contains:** influenza A is a virus and carries no plasmid-borne
beta-lactamase, so the question has no referent; NCBI Pathogen Detection indexes **106
organism groups and every one is bacterial**; the nearest answerable questions are
"blaNDM-1 in *Klebsiella pneumoniae*" or "influenza sequences", which live on different
sources; influenza sequence data is PDN/LAPIS (`influenza-a`, `h5n1`) or NCBI Virus.

**A bad answer:** any count at all. The likeliest wrong route is a substring match on the
group list — **`Haemophilus influenzae` is one of the 106 groups** ✅, a bacterium whose
species name contains "influenza". A router grepping for "influenza" finds it and returns
16,981 index rows for the wrong kingdom.

```
✅ GET .../pathogens-srv/?limit=0&facets=taxgroup_name[||1|500]&action=retrieve&collection=isolates
HTTP 200   numBuckets: 106
  'influenza' substring matches: Haemophilus influenzae (16,981 rows)
  no viral group of any kind; largest groups are Salmonella enterica 1,766,824
  and E.coli and Shigella 1,162,675
```

---

## A2. "How many of the *E. coli* isolates in NCBI carry the CFTR ΔF508 mutation?"

A human disease gene asked of a bacterium, phrased so the filter accepts it.

**A good answer contains:** CFTR is a human gene and *E. coli* has no homologue, so the
premise is wrong before any count; the filter returns **0**, and that 0 is the service
saying "no isolate is annotated with this token", which is exactly what it would say for a
real gene nobody carries — the two are indistinguishable from the response; the nearest
answerable question is any symbol in the *E. coli* AMR vocabulary (**7,611 of them** 📋);
human variant data is MyVariant or ClinVar, and cystic-fibrosis genetics is neither of
these servers' business.

**A bad answer:** "0 isolates carry CFTR ΔF508, so this mutation is rare in *E. coli*."
That sentence is a confident zero and a category error in one.

```
✅ GET .../pathogens-srv/?limit=0&facets=target_acc[||1|1]
    &fq=taxgroup_name==["E.coli and Shigella"] and AMR_genotypes==["CFTR"]
HTTP 200   totalCount: 0
```

**Why this one is worth keeping:** it is the general case of the group-name trap. A wrong
*value* in `AMR_genotypes` fails exactly like a wrong value in `taxgroup_name` — HTTP 200,
`totalCount: 0`, no note. Only the vocabulary tools (`ncbi_pathogen_amr_genes`,
`ncbi_pathogen_organisms`) can tell you which kind of zero you have.

---

## A3. "How many *E. coli* sequences are in the Pathogen Data Network?"

The curated-name trap, ported to a second source. `"Escherichia coli"` vs
`"E.coli and Shigella"` is measured for Pathogen Detection 📋; this is the analogue for
PDN/LAPIS, whose organism keys are short slugs, not species names.

**A good answer contains:** PDN holds no bacteria at all — Pathoplexus human viral
pathogens, GenSpectrum influenza and dengue, CoV-Spectrum SARS-CoV-2; the organism
argument is a **slug** (`h5n1`, `influenza-a`, `denv1`, `yellow-fever`), so a species name
is rejected outright rather than silently returning nothing; the nearest answerable
question for bacterial geography is `ncbi_pathogen_isolates`, whose records carry
`geo_loc_name`; the name "Pathogen Data Network" is what makes this trap work.

**A bad answer:** narrating the tool's error as data, or trying `species.name`-style
fallbacks until something returns 200.

```
✅ observed in a matrix transcript, 2026-09-17 (argo/gpt4o, Q1):
   lapis_describe_organism(organism="e.coli")
   -> "Error executing tool lapis_describe_organism: Unknown organism 'e.coli'.
       Call lapis_list_organisms for the full list."
   lapis_list_organisms -> Pathoplexus (andv, cchf, ... yellow-fever),
       GenSpectrum Loculus (influenza-a, denv1 ...), CoV-Spectrum
```

**The difference worth demoing:** PDN *refuses* the wrong name; Pathogen Detection
*answers* it with a zero. The second is the dangerous one, and it is the one this board
hits most often.

---

## A4. "Summarise the study GSM9284462 and list its samples."

A Sample accession used where a Series is needed.

**A good answer contains:** GSM9284462 **is** a sample, so it has no samples to list;
searching `db=gds` for it returns **three records of three different types** and the
Sample is not the first; the nearest answerable question is the Series that contains it,
GSE309890; `geo_resolve_accession` exists on our GEO server for exactly this.

**A bad answer:** taking hit 1. The first id is the *Series*, the second is the *Platform*,
and an answer built on either is about a real record of the wrong type — HTTP 200
throughout.

```
✅ GET .../esearch.fcgi?db=gds&term=GSM9284462&retmax=5&tool=niaid-bionexus-p7
HTTP 200   count: 3
  idlist: ['200309890',  -> GSE309890, the Series
           '100024659',  -> GPL24659, the Platform
           '309284462']  -> GSM9284462, the Sample actually asked for, at rank 3
```

The `2`/`1`/no-prefix digit in front of the UID is the record type, and nothing in the
response says so.

---

## A5. "What assemblies does BRC Analytics have for PRJNA715470?"

A BioProject accession where a taxonomy id is required.

**A good answer contains:** `get_assemblies` takes `taxonomy_id` and nothing else, so a
BioProject accession is not a value it can accept; the project has to be resolved to
organisms first, and **PRJNA715470 contains 13 distinct organisms** 📋 despite being
labelled *E. coli*, so there is no single taxid to hand over; the nearest answerable
question is "assemblies for taxid 562", which returns **2** 📋; the resolution step is
`ncbi_bioproject_summary` → `ncbi_sra_run_metadata`, and it needs the accession turned
into a UID first because `esummary db=bioproject id=PRJNA715470` returns HTTP 200 with
`Invalid uid` inside the body 📋.

**A bad answer:** passing the accession as `taxonomy_id`, getting an empty catalogue back,
and reporting "BRC Analytics has no assemblies for this project" — true of the call, false
of the world.

---

## A6. "What does BV-BRC have on *Klebsiella pneumoniae* antimicrobial resistance?"

The only source that can answer it is not wired into the chatbot.

**A good answer contains:** the assistant cannot reach BV-BRC, because the catalogue that
indexes it — NDE — **is not in `MCP_SERVERS` in `chatbot.py`** and is stdio-only 📋; worse,
even a wired NDE pointed at production would answer **0**, because BV-BRC is crawled into
staging and not promoted; the nearest answerable questions on this board are Pathogen
Detection's *K. pneumoniae* group (**349,968 index rows** ✅) and BRC Analytics' workflow
catalogue; the source to go to directly is bv-brc.org.

**A bad answer:** "NDE does not index BV-BRC." That is what production tells you, with a
clean HTTP 200 and a total of zero.

```
✅ q=includedInDataCatalog.name:"Bacterial and Viral Bioinformatics Resource Center"
   https://api.data.niaid.nih.gov/v1/query          HTTP 200   total = 0
   https://api-staging.data.niaid.nih.gov/v1/query  HTTP 200   total = 118,625
```

`nde_mcp/client.py` reads the host from `NDE_API_URL` **at startup** and has no per-call
host argument 📋, so one running instance can only ever see one of these two numbers.

---

## A7. "Give me the PDB structures for *E. coli* GyrA so I can look at the quinolone-binding pocket."

The answer exists upstream and our own tool removes it.

**A good answer contains:** UniProt P0AES4 carries **20 PDB cross-references**, and they
arrive in the default entry; `uniprot_get_entry` returns **none of them**, because
`ENTRY_FIELDS` (`mcp_servers/uniprot.py:55`) never names `xref_pdb`; the nearest answerable
question through our tools is the function and GO annotation, which is not a structural
answer; RCSB PDB and AlphaFold are where the coordinates are, and nothing on this board can
interpret a structure even when handed one.

**A bad answer:** narrating the function annotation as though it answered a structural
question, or asserting that UniProt holds no structures for this protein. The second is
what the tool's output actually implies, and it is wrong.

```
✅ GET https://rest.uniprot.org/uniprotkb/P0AES4                     HTTP 200
     151 cross-references, of which 20 are PDB: 1AB4, 1X75, 1ZI0, 2Y3P, 3NUH, ...
✅ GET https://rest.uniprot.org/uniprotkb/P0AES4?fields=<ENTRY_FIELDS>  HTTP 200
     PDB cross-references: 0
```

This is the one case on the board where the raw API is right and we are the ones losing the
answer 📋. It is a one-word fix in a file this branch does not own, so it is reported, not
patched.

---

## A8. "How many *E. coli* sequencing runs are there?"

One noun, four denominators, and every one of them is a real number.

**A good answer contains:** the count depends entirely on what is being counted, and the
answer must say which; **ENA holds 551,679 runs** ✅ for taxid 562, **NCBI SRA holds
631,321 experiments** ✅ for the same taxon, and **Pathogen Detection holds 581,464 distinct
isolates** ✅ — runs, experiments and isolates are three different units, not three
estimates of one number; the nearest single answerable question is whichever unit the user
actually needs; a run belongs to an experiment, an experiment to a BioSample, and an
isolate to neither index.

**A bad answer:** picking one and calling it "the number of *E. coli* sequences", or
averaging them, or reporting the federated `search_ena` page size — **50, with
`has_more: true`** 📋 — which is wrong by four orders of magnitude.

```
✅ https://www.ebi.ac.uk/ena/portal/api/count?result=read_run&query=tax_eq(562)
   HTTP 200   551679
✅ .../esearch.fcgi?db=sra&term=txid562[Organism:exp]&tool=niaid-bionexus-p7
   HTTP 200   count: 631321   querytranslation: txid562[Organism:exp]
✅ .../pathogens-srv/?facets=target_acc[||1|1]&fq=taxgroup_name==["E.coli and Shigella"]
   HTTP 200   distinct isolates: 581464   index rows: 1162675
```

---

## A9. "Find me every ENA run whose study description mentions carbapenem resistance."

Written to pull the router towards `search_ena_keywords`.

**A good answer contains:** the federated `search_ena_keywords` sends the value to ENA
unquoted and ENA answers **HTTP 400**, which arrives as the tool's *text* rather than as an
error 📋; our `brc_ena_search` quotes it and returns a usable total (**48,421** for the
measured E. coli case 📋); a keyword match on a free-text study description is not a
resistance annotation, so the count is a text match and must be labelled one; the curated
answer to "which runs carry carbapenemases" is Pathogen Detection's `AMR_genotypes`.

**A bad answer:** repeating the 400 as a finding ("ENA returned no matching runs"), which
is the failure this whole repo is named after — an error narrated as an absence.

---

## A10. "How many *E. coli* isolates carry blaCTX-M-15?" *(control — answerable)*

**Expected: a number, with the group name and the unit.** **75,487 distinct isolates** ✅,
from 150,926 index rows — the service stores two rows per isolate for this organism, so the
row count is not the answer. The group is *E. coli **and Shigella***, and the answer should
say so rather than claiming a pure *E. coli* denominator.

```
✅ GET .../pathogens-srv/?limit=0&facets=target_acc[||1|1]
    &fq=taxgroup_name==["E.coli and Shigella"] and AMR_genotypes==["blaCTX-M-15"]
HTTP 200   distinct: 75,487   index rows: 150,926
   solr: * -status:withdrawn
         {!tag=taxgroup_name}taxgroup_name:("E.coli and Shigella")
         {!tag=AMR_genotypes}AMR_genotypes:("blaCTX-M-15")
```

A control earns its place by being able to fail: this number is re-measured, not copied,
and it agreed with the team's independent measurement on the same day to the digit.

---

## A11. "What organism group does NCBI use for *E. coli*, and how big is it?" *(control — answerable)*

**Expected: the curated name and both numbers.** The group is **`E.coli and Shigella`**,
holding **581,464 distinct isolates** across **1,162,675 index rows** ✅, one of **106**
groups ✅. A good answer gives the name, both numbers, and the warning that the group mixes
two genera so "581,464 *E. coli* isolates" is wrong.

```
✅ GET .../pathogens-srv/?limit=0&facets=target_acc[||1|1]
    &fq=taxgroup_name==["E.coli and Shigella"]
HTTP 200   distinct: 581464   index rows: 1162675
```

---

## A12. "How many *Shigella flexneri* isolates are there?"

A real organism, really indexed, that returns zero.

**A good answer contains:** *S. flexneri* isolates are in the index, inside the group
**`E.coli and Shigella`** — there is no `Shigella flexneri` group and no way to separate the
species with this filter ✅; the number that exists is the combined **581,464** ✅; the
nearest answerable question is the combined group, or a species-level query against NCBI
Taxonomy/BioSample, which is a different index; a species breakdown needs the FTP isolate
table or BigQuery, neither of which is wrapped here.

**A bad answer:** "0 — NCBI Pathogen Detection holds no *Shigella flexneri* isolates."
HTTP 200, `totalCount: 0`, and completely false.

```
✅ of the 106 taxgroup_name values, the only one containing "shigella" is
   'E.coli and Shigella' (1,162,675 rows). No 'Shigella flexneri' group exists.
```

---

## A13. "What runs are in study PRJNA715470? Use BRC Analytics."

An upstream 500 that a router can read as "no data".

**A good answer contains:** BRC Analytics' own study endpoint **returns HTTP 500** for this
accession, and the body shows why — it forwards an unquoted accession to ENA and ENA answers
400 ✅; a 500 is not evidence about the study; our `brc_ena_study` answers the same question
with **369 runs across 13 organisms** 📋; NCBI's side of the same project reports **382 SRA
experiments** 📋, a different unit, and the answer should say which it is quoting.

**A bad answer:** "BRC Analytics has no data for PRJNA715470", or retrying until something
returns and reporting whatever that was.

```
✅ GET https://brc-analytics.org/api/v1/ena/study/PRJNA715470
HTTP 500
{"detail":"Failed to fetch study data from ENA: Client error '400 ' for url
 'https://www.ebi.ac.uk/ena/portal/api/search?result=study&query=study_accession%3DPR..."}
```

---

## A14. "Run AMR gene detection on the *E. coli* reference genome and tell me what it found."

An instruction the board can plan and cannot execute.

**A good answer contains:** the workflow **is** compatible with GCF_000005845.2 and every
input resolves — TRS id `#workflow/github.com/iwc-workflows/amr_gene_detection/main/versions/v1.1.7`,
input FASTA resolved, nothing unresolved 📋; **the MCP server is read-only**, so nothing ran
and there are no findings to report; the nearest answerable outcome is the resolved input
set plus the launch URL; a human starts the run at brc.usegalaxy.org, which needs a Galaxy
key and a separate yes.

**A bad answer:** any list of detected genes. There is no tool result to support one, so
every gene named is fabricated — and it will look exactly like a real answer, because the
compatibility check genuinely returned `compatible: true`.

---

## A15. "Which of those ciprofloxacin studies shows the biggest fold-change in gyrA?"

A follow-up to a question the board answers, asking for numbers that are in no API response.

**A good answer contains:** GEO's API returns **metadata only** — the expression values sit
in a submitter-uploaded supplementary file, for GSE309890 a single
`GSE309890_FPKMs_allSamples.csv.gz` of 510 KB reached over `ftp://` 📋; no tool on this
board parses that file, and it has no schema, no units contract and no guarantee of a
matrix; the nearest answerable question is "which studies exist and where are their files",
which is **37 Series** 📋 with download URLs; computing fold-changes needs the file pulled
and analysed outside this assistant.

**A bad answer:** a ranked table of fold-changes. It cannot come from a tool result, so it
came from the model. This is the single most demo-damaging fabrication available on this
board, because the question it follows — Q3 in `QUESTIONS.md`, one `geo_search` call — really
does return 37 studies, so the fabricated table arrives wrapped in a correct answer.

---

## Ground-truth ledger

| # | claim | value | measured |
|---|---|---|---|
| A1 | organism groups in Pathogen Detection; none viral | 106 | ✅ 2026-09-17, a `taxgroup_name` facet over the whole collection |
| A1 | "influenza" substring match is a bacterium | *Haemophilus influenzae*, 16,981 rows | ✅ same call |
| A2 | `AMR_genotypes==["CFTR"]` in the E. coli group | totalCount 0 | ✅ 2026-09-17 |
| A2 | distinct AMR symbols in the E. coli vocabulary | 7,611 | 📋 `QUESTIONS.md` Q6 |
| A3 | LAPIS rejects a species name; organism keys are slugs | error text quoted | ✅ matrix transcript, 2026-09-17 |
| A4 | `esearch db=gds term=GSM9284462` | 3 ids, Sample at rank 3 | ✅ 2026-09-17 |
| A5 | organisms in PRJNA715470 | 13 | 📋 `PIPELINES.md` P2 |
| A5 | BRC assemblies for taxid 562 | 2 | 📋 `QUESTIONS.md` Q4 |
| A6 | NDE production, BV-BRC catalogue | 0 | ✅ 2026-09-17 |
| A6 | NDE staging, same query | 118,625 | ✅ 2026-09-17 |
| A6 | *K. pneumoniae* index rows | 349,968 | ✅ 2026-09-17 |
| A7 | PDB cross-references on P0AES4, default entry | 20 | ✅ 2026-09-17 |
| A7 | the same entry through `ENTRY_FIELDS` | 0 | ✅ 2026-09-17 |
| A8 | ENA runs, taxid 562 | 551,679 | ✅ 2026-09-17 |
| A8 | NCBI SRA experiments, txid562 | 631,321 | ✅ 2026-09-17 |
| A8 | federated `search_ena` page size | 50, `has_more: true` | 📋 `REPORT.md` |
| A9 | `search_ena_keywords` → ENA 400 as tool text | — | 📋 `REPORT.md` |
| A9 | `brc_ena_search` total for the measured case | 48,421 | 📋 `REPORT.md` |
| A10 | blaCTX-M-15 in the E. coli group | 75,487 distinct / 150,926 rows | ✅ 2026-09-17 |
| A11 | the E. coli group and its size | `E.coli and Shigella`, 581,464 / 1,162,675 | ✅ 2026-09-17 |
| A12 | groups containing "shigella" | exactly one, the combined group | ✅ 2026-09-17 |
| A13 | BRC `/api/v1/ena/study/PRJNA715470` | HTTP 500 wrapping an ENA 400 | ✅ 2026-09-17 |
| A13 | `brc_ena_study` for the same accession | 369 runs, 13 organisms | 📋 `README.md` |
| A14 | `check_compatibility` / `resolve_workflow_inputs` for K-12 | compatible, nothing unresolved | 📋 `QUESTIONS.md` Q7 |
| A15 | supplementary file behind GSE309890 | one `.csv.gz`, 510 KB, `ftp://` | 📋 `FAIR.md` |
| A15 | ciprofloxacin Series in GEO | 37 | 📋 `QUESTIONS.md` Q3 |

**What I did not measure.** Every 📋 row. They were measured by a teammate on 2026-09-17 and
are quoted rather than re-run, because the model matrix held the NCBI budget while this file
was written. Re-running them is `uv run evals/run_eval.py`; re-running the ✅ rows is the
call pasted beside each one. **All of these are live counts and they move** — BRC's workflow
catalogue changed between 16 and 17 September while this repo was being built.

## Using these in the harness

These are not wired into `run_questions.py`, which drives the fifteen demo questions in
`QUESTIONS.md`. To measure a model against this file, add the question text there and add a
row to `EXPECTED` in `judge.py` with `kind: "gap"` for A1–A9 and A12–A15, `kind: "answer"`
for A10 and A11. The honest-null check then requires a refusal carrying a reason and a named
alternative source, which is the four-part refusal `PIPELINES.md` P8 specifies.
