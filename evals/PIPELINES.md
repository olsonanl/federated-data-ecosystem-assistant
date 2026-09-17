# The pipelines, and how to prove each one works

A researcher does not ask for a tool. They ask a question, and the system has to decide
which sources can answer it, in what order, and — the part most systems skip — **which
parts cannot be answered, and why**.

This document names the eight pipelines that answer the board's questions, gives each one
a verification question with a ground truth, and states what a researcher would have done
instead. Every benchmark, test and demo in this repo should point back to a row here. If
a claim about the system cannot be traced to a pipeline and a verification question, it
is not yet a claim worth making.

Traceability: board question → pipeline → verification question → ground truth → the call
that produced it.

---

## Where this comes from

The team's day-1 whiteboard set two biological questions and four TODOs. TODO 4 is the
one that shapes this document:

> **"Why can't we answer certain questions with these resources?"**

That is not a caveat at the end of a demo. It is a deliverable, and it is the hardest one,
because a system that cannot say *no* convincingly cannot be trusted when it says *yes*.

The board's questions:

1. **"How many methicillin-resistant strains of *E. coli* are there?"** with four branches
   — read genome queries · what groups study these · tell me about strain X · structural
   implications
2. **"What is the latest in AMR for ←→"**

---

## The eight pipelines

| # | Pipeline | The researcher's question | Primary source |
|---|---|---|---|
| P1 | **Discovery** | "Where is there data about X?" | NDE |
| P2 | **ID crosswalk** | "Give me the actual accessions and files" | NCBI + ENA |
| P3 | **Expression** | "What genes changed under this treatment?" | GEO |
| P4 | **AMR genotype** | "How many isolates carry this resistance gene?" | NCBI Pathogen Detection |
| P5 | **Compute planning** | "What can I run, on which genome?" | BRC Analytics |
| P6 | **Literature** | "What is the latest on X?" | PubMed |
| P7 | **Protein & structure** | "What does this protein do?" | UniProt, MyGene, STRING |
| P8 | **Gap** | any question the board cannot answer | none — this is the point |

---

## P1 · Discovery — "where is there data about X?"

**Chain:** `nde_search_datasets` → `nde_facet_counts` → `nde_get_record` → accessions out.

**Verification question:** *"Where is there data on E. coli antimicrobial resistance, and
who funded it?"*

| | |
|---|---|
| Ground truth | 3,644 records; NIAID funds 44, Wellcome 30, NIGMS 26 |
| The trap | Pathogens live in `infectiousAgent.name`, hosts in `species.name`. The wrong field returns **1** where the right one returns **60,107**. Casing is irrelevant — the 16 Sep survey records this as a lowercase rule and that is wrong. |
| Without the system | You would query `species.name`, get 1 hit, and conclude NDE has no E. coli data |
| Honest limit | **NDE is not wired into `chatbot.py`.** It exists as a standalone stdio package. Until someone gives it an HTTP transport, P1 cannot run in the demo at all. |

**Status: blocked.** This is the single biggest hole in the system and it is nobody's lane.

---

## P2 · ID crosswalk — "give me the actual accessions and files"

**Chain:** `ncbi_taxonomy_lookup` → `ncbi_sra_search` → `ncbi_sra_runs_for_project` →
`brc_ena_study` → FASTQ URLs.

**Verification question:** *"What is actually in BioProject PRJNA715470?"*

| | |
|---|---|
| Ground truth | 382 SRA experiments · 369 ENA runs · **13 distinct organisms** |
| The trap | The project is labelled *E. coli*. It contains *Citrobacter*, *Enterobacter* and eleven other genera. **A project-level organism label is not a run-level organism.** |
| Without the system | You would take the project label at face value and build a cohort with three genera in it |
| Honest limit | ENA and NCBI disagree on run counts (369 vs 382 experiments — different units, different indexes). Say which you are quoting. |

---

## P3 · Expression — "what genes changed under this treatment?"

**Chain:** `geo_search` → `geo_series` → supplementary file URLs → (`ncbi_sra_runs_for_project`
for the raw reads behind it).

**Verification question:** *"Which E. coli expression studies involve ciprofloxacin, and
where are the numbers?"*

| | |
|---|---|
| Ground truth | 37 Series; GSE309890 ranks 1; `GSE309890_FPKMs_allSamples.csv.gz`, 510K |
| The traps | An unfiltered `gds` count returns **513**, mixing four record types. A GEO accession is not a UID, and guessing the GDS prefix returns a real record of the wrong type. The numbers are in **no** API response — only in FTP files. |
| Without the system | You would report 513, or fetch the wrong record silently, or conclude the expression values do not exist because `suppfile` says `'CSV'` |
| Honest limit | The supplementary file format is whatever the submitter uploaded. No schema, no units contract, no guarantee of a matrix. |

**This is the pipeline no other source on the board can serve.** GEO is the only
expression data here.

---

## P4 · AMR genotype — "how many isolates carry this gene?"

**Chain:** `ncbi_pathogen_organisms` → `ncbi_pathogen_isolate_count` →
`ncbi_pathogen_amr_genes` → `ncbi_pathogen_isolates`.

**Verification question:** *"How many E. coli isolates carry gyrA_S83L?"*

| | |
|---|---|
| Ground truth | **170,726** of 581,464 · `blaCTX-M-15` 75,487 · `mecA` **2** |
| The traps | The group name is `"E.coli and Shigella"`, not `"Escherichia coli"` — a wrong value returns a confident **0**. `totalCount` is 2× the distinct count for E. coli and **1×** for *S. aureus*, so halving unconditionally under-reports *S. aureus* by half. |
| Without the system | You would query the wrong group name and get zero, and read it as absence |
| Honest limit | `AST_phenotypes` — the measured susceptibility calls — is **returned per isolate but not filterable**. `ncbi_pathogen_isolates` asks for it by name in its field list (`ncbi_lib/server.py:1545`); `_pathogen_filter` builds its query from five fields and that is not one of them, so a phenotype count needs client-side filtering over pages. For *E. coli* that filtering would come back empty anyway: of the first 200 rows retrieved for `"E.coli and Shigella"` (171 distinct isolates), **0** carried the field, against **136 of 200** for *S. aureus*, which is how we know the field name works and the absence is real. Genotype yes; phenotype only where the submitter measured it. |

---

## P5 · Compute planning — "what can I run, on which genome?"

**Chain:** `search_organisms` → `get_assemblies` → `get_compatible_workflows` →
`check_compatibility` → `resolve_workflow_inputs`.

**Verification question:** *"Can I run AMR gene detection on the E. coli reference genome,
and what inputs does it need?"*

| | |
|---|---|
| Ground truth | `compatible: true`; 17 workflows for haploid taxid 562; `ASSEMBLY_FASTA_URL` resolves, nothing unresolved |
| The traps | Only **2** E. coli assemblies exist — this is a reference catalogue, not a strain collection. You ask about taxid 562 and the assembly comes back as 511145. |
| Without the system | You would assume a genome-analysis platform has many E. coli genomes, and design around strains that are not there |
| Honest limit | Read-only. Launching a workflow needs a Galaxy key and a separate human yes, and read and execute deliberately live in different servers. |

---

## P6 · Literature — "what is the latest in AMR for X?"

**Chain:** `pubmed_search_articles` → `pubmed_get_article` → link out to the data.

**Verification question:** *"What is the latest on ciprofloxacin resistance in E. coli?"*

| | |
|---|---|
| Ground truth | **5,273** hits for "Escherichia coli ciprofloxacin resistance", verified 17 Sep |
| The trap | NCBI rewrites the query through MeSH silently. That search becomes `("escherichia coli"[MeSH Terms] OR ("escherichia"[All Fields] AND "coli"[All Fields]) ...) AND ("ciprofloxacin"[Supplementary Concept] ...)`. The count follows the rewrite, so it is meaningless without reading `query_translation` — which the tool does return. |
| Without the system | A count with no idea which query produced it |
| Honest limit | The paper-to-data hop is sparse: only 137 of 3,644 E. coli AMR records in NDE carry a PMID. |

**Status: spot-checked, not ours.** This is Everaldo's server; the ground truth above is one
observation, not a suite. Its owner should set the rest.

---

## P7 · Protein & structure — "what does this protein do?"

**Chain:** `mygene_search_genes` → `uniprot_search` → `uniprot_get_entry`.

**Verification question:** *"What does E. coli GyrA do, and where are the
fluoroquinolone-resistance mutations?"*

| | |
|---|---|
| Ground truth | `P0AES4` · gyrA · 875 aa · *E. coli* K12 · cytoplasm · 2 function blocks · 10 GO terms, verified 17 Sep |
| The trap | `uniprot_search("gyrA", organism="Escherichia coli")` returns 3 hits, and two of them are `ccdB` and `parC` — related proteins, not the one asked for. Take the hit whose gene symbol matches, never hit 1. |
| Without the system | Manual UniProt browsing |
| Honest limit | "Structural implications" — the board's fourth branch — is the weakest area on the whole board. BRC lists `PROTEIN_FOLDING` with **0** workflows, marked coming soon, and `uniprot_get_entry` returns no structure link. Measured 17 Sep by `board-structure`: the **20** PDB cross-references for `P0AES4` are already in the entry UniProt returns by default. `ENTRY_FIELDS` (`uniprot.py:55`) does not name `xref_pdb`, so they are trimmed on our side rather than missing upstream. |

**Status: spot-checked, not ours.** An earlier draft of this file reported that
`uniprot_get_entry`'s docstring promised interactions and an AlphaFold URL it did not
return. **That was already fixed** before this was written — the returned keys now match
the docstring exactly. The claim was stale and is withdrawn.

---

## P8 · Gap — the board's TODO 4

Not a fallback. The pipeline that runs when the honest answer is *no*, and the one that
decides whether this system can be trusted.

A good refusal has four parts: **the number that proves it**, **why the question is
malformed**, **the nearest answerable question**, and **the source that could answer it**.

**Verification question:** *"How many methicillin-resistant strains of E. coli are there?"*
— the board's own headline question.

A bad system says "I don't know." A worse one invents a number. This system should say:

> **2** — out of 581,464 *E. coli* and Shigella isolates in NCBI Pathogen Detection carry
> `mecA`. For comparison, 93,260 of 171,412 *S. aureus* isolates carry it.
>
> Two in half a million is the noise floor, and it is telling you the question is
> malformed rather than the data being missing: methicillin resistance is mediated by
> `mecA`/`mecC`, which are staphylococcal. Gram-negatives like *E. coli* are
> intrinsically resistant to methicillin, so no one types for it.
>
> The question you probably want is one of:
> - **MRSA** — 93,260 *S. aureus* isolates carry `mecA`
> - **ESBL *E. coli*** — 75,487 carry `blaCTX-M-15`
> - **Fluoroquinolone-resistant *E. coli*** — 170,726 carry `gyrA_S83L`
>
> Measured susceptibility (MIC values) is a different question again, and this system
> cannot reach it for *E. coli*: `ncbi_pathogen_isolates` does return `AST_phenotypes`
> per isolate, but nothing can filter on it, and none of the 171 *E. coli* isolates
> sampled carried it at all. BV-BRC or CARD would answer it.

Every number in that answer carries the call that produced it. **That paragraph is the
product.** It is what a researcher cannot get from an LLM alone, from the API docs, or
from a search engine.

### The four sub-branches, measured

The whiteboard hung four sub-branches off the headline question, and until 17 Sep only
the headline was measured. Each now has a harness case, so the verdicts below are
observations rather than opinions. Every ground truth was re-verified live on 17 Sep
2026; these are live counts, so re-run before quoting one.

One inconsistency in this document, named rather than propagated: the board's questions
at the top of this file list the headline's fourth branch as *read genome queries*, while
this section has always tracked *latest in AMR for X* in that slot. Both are on the
whiteboard — the second is the board's own question 2. *Read genome queries* is not
unmeasured; it is what P2 and P3 do, with eight harness cases between them.

**What "answered" means here, exactly.** These four cases call E-utilities, Datasets
and UniProt REST directly on both sides, because the thing being compared is the shape
of the call. They do **not** exercise our MCP servers. The NCBI server on this branch
already exposes `ncbi_bioproject_summary`, `ncbi_taxonomy_lookup`, `ncbi_assembly_info`
and `ncbi_pubmed_search`, which are the natural homes for three of these chains —
whether those tools carry the guards measured here is **untested**, and it belongs to
their owner, not to this file.

| Board sub-branch | Case | Ground truth (17 Sep) | Verdict | The trap the case measures |
|---|---|---|---|---|
| "What groups study these?" | `board-groups` | PRJNA715470 → `submitter_organization` = **University of Pennsylvania** | **answerable** | `esummary db=bioproject id=PRJNA715470` returns **HTTP 200** with `"Invalid uid PRJNA715470 at position= 0"` buried in the body and zero records — which reads as a project with no submitter, not as a bad call. The accession has to be resolved to UID **715470** through `esearch term=PRJNA715470[Project Accession]` first. |
| "Tell me about strain X" | `board-strain` | GCF_000005845.2 → taxid **511145**, 4,641,652 bp, 1 contig | **answerable** | NCBI Taxonomy holds no `"Escherichia coli K-12 MG1655"`: **0 hits**. Dropping the substrain to `"Escherichia coli K-12"` returns **83333**, a different substrain, with nothing to say you moved. The record is `Escherichia coli str. K-12 substr. MG1655`. Enter through the assembly accession and the taxid is stated rather than guessed. |
| "Structural implications" | `board-structure` | **20** PDB cross-references for `P0AES4` | **gap** | The one case on this board where the obvious call is the right one: the whole entry carries all 20. `ENTRY_FIELDS` (`uniprot.py:55`) never names `xref_pdb`, so `uniprot_get_entry` returns none of them and nothing downstream can ask. The fix is one field name in a file this branch does not own, so it is reported here, not patched. |
| "Latest in AMR for X" | `board-latest` | **283** hits with a 2026 publication date for "Escherichia coli ciprofloxacin resistance", against 5,273 with no window | **answerable** | Two silent substitutions. An unrecognised sort value — `sort="Publication Date"`, which is PubMed's own web-UI label — is not refused: esearch returns HTTP 200 in **relevance** order, so "latest" becomes "best match". And omitting `datetype` does not drop the window, it switches it to `edat`, the date PubMed indexed the record: **274** instead of 283. Nine hits apart is close enough to look right. |
| MIC / susceptibility values | — | 0 of 171 *E. coli* isolates sampled carry `AST_phenotypes`; 136 of 200 *S. aureus* do | **cannot, for E. coli** | Not a wrapper problem. The field is returned per isolate and simply is not populated for this organism — see P4's honest limit. No tool can filter on it either. |

One caveat on `board-latest` worth saying out loud: under `sort=pub_date` the top hits
are dated **2026 Dec** while their electronic publication dates are June. PubMed sorts
on the citation's issue date, so "latest" surfaces ahead-of-print articles in
future-dated issues before papers actually posted last week. The sort is correct.
"Latest" here means the issue date, not the date the paper became readable, and an
answer that quotes it should say which one it means.

---

## How to use this document

1. **Every benchmark points at a pipeline.** A test that cannot name its pipeline and its
   verification question is measuring something nobody asked for.
2. **Every ground truth carries the call that produced it**, and a date. These are live
   counts; BRC's workflow catalogue changed mid-build between 16 and 17 Sep.
3. **A pipeline with no ground truth is not verified**, and should be labelled that way in
   any demo. P6 and P7 are in that state right now.
4. **P8 is scored like the others.** "Cannot answer" is a correct answer and should pass,
   not be excluded from the denominator. Where the system genuinely cannot answer a board
   question, the case still runs, still scores, and is listed in `EXPECTED_GAPS` in
   `run_eval.py`: it prints as `GAP`, keeps its place in the denominator, and does not
   fail the run. That list is for holes the board already knows about. Adding an id to it
   to quiet a real failure would turn the one honest number in this repo into a decoration.

Coverage today:

| | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 |
|---|---|---|---|---|---|---|---|---|
| ground truth | — | yes | yes | yes | yes | one | one | yes |
| in the harness | — | yes | yes | yes | yes | — | — | **5 cases** |
| offline tests | — | — | 52 | — | 26 | — | — | — |
| ours to own | — | part | **yes** | part | **yes** | no | no | shared |

P1 is blocked and unowned. P3 and P5 are the two this branch owns outright and they are
the two with regression tests. P6 and P7 belong to other people; the single observations
recorded above are spot checks, not suites, and their owners should set the rest.

P8's five cases are the headline question plus the four sub-branches. Two of them,
`board-latest` and `board-structure`, reach into P6's and P7's sources — PubMed and
UniProt — but they call those APIs directly and prove nothing about the servers that
wrap them. The row for P6 and P7 stays empty on purpose.

Harness totals on 17 Sep 2026: **14 cases, baseline 1/14, tools 13/14, one measured
gap.** The single baseline pass is `board-structure`, the case where the raw API is
right and we are the ones losing the answer.
