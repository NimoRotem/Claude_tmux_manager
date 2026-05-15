# Changelog (tmux-dashboard scratch / docs)

This repo is primarily the tmux-dashboard FastAPI app. Recent additions
also include the bioinformatics review documentation packets for
23andclaude.com (the genomics service that lives in the sibling
`23andClaude` repository).

## 2026-05-14 / 2026-05-15 — `pipelinesdocsv2/` + `pipelinesdocs/`

Two markdown documentation packets served under nginx at
<https://23andclaude.com/pipelinesdocs/> and
<https://23andclaude.com/pipelinesdocsv2/>.

### `pipelinesdocs/` (v1 — kept for reference)

Original ten-chapter packet covering the 23andclaude PGS pipeline
end-to-end. Predates the safety-gate / advisor-driven rebuild.

### `pipelinesdocsv2/` (v2 — current)

Sixteen-chapter packet plus a `modules/` subdirectory cataloguing
adjacent services. Index:

| # | Doc | Topic |
| --- | --- | --- |
| 00 | `README.md` | Index + TL;DR + EAS-mismatch one-liner |
| 01 | `01-overview.md` | System architecture + request dataflow |
| 02 | `02-input-and-alignment.md` | VCF / gVCF / BAM / CRAM / 23andMe ingestion, variant calling, build validation |
| 03 | `03-pgs-ingestion.md` | PGS Catalog download, parsing, normalization, harmonization, liftover, eligibility |
| 04 | `04-scoring-pipeline.md` | plink2 `--score` pipelines (full-pgen, fast-path, Pipeline E+), match-rate gating |
| 05 | `05-ancestry-inference.md` | PCA cache, projection, super-pop centroids, admixture, reference selection |
| 06 | `06-reference-panel.md` | 1000 Genomes Phase 3 panel + ref-stats stores |
| 07 | `07-percentile-and-stats.md` | Parametric Φ + ECDF percentile, schema contract, sanity gates, scale reconciliation, live overlay |
| 08 | `08-qa-and-validation.md` | Build validation, eligibility gates, cohort sanity, in-batch control, nightly self-test, CI |
| 09 | `09-ancestry-mismatch-incident.md` | The EAS / non-EUR no-percentile failure class — root cause + four candidate fixes |
| 10 | `10-data-layout.md` | Directory inventory, cache schemas, DB tables, JSON formats |
| 11 | `11-examples.md` | Six end-to-end worked examples covering every input type |
| 12 | `12-known-issues.md` | Past incidents, mitigations, drift safeguards |
| 13 | `13-reviewer-questions.md` | 22 specific decisions / open questions for the advisor |
| 14 | `14-advisor-review.md` | First external advisor's response (verbatim, 2026-05-14) |
| 15 | `15-action-plan.md` | Concrete implementation roadmap with per-item code touchpoints, sequencing, evidence |

Modules sub-packet (`modules/`):

| Module | URL on 23andclaude.com | Doc |
| --- | --- | --- |
| simple-genomics internals (compare, profiles, chat, …) | `/` (port 8800) | `simple-genomics-internals.md` |
| bam-converter | `/convert/` (port 8720) | `bam-converter.md` |
| simple-ancestry | `/ancestry/` (port 8710) | `simple-ancestry.md` |
| legacy ancestry (kept as backup) | `/ancestry2/`, `/v1/` (port 8700) | `legacy-ancestry.md` |
| translocation-scanner v2 | `/translocation-scanner-v2/` — **410 Gone since 2026-05-14** | `translocation-scanner-v2.md` |
| translocation-scanner v3 | `/translocation-scanner-v3/` (port 8770) | `translocation-scanner-v3.md` |
| translocation-scanner v4 (current) | `/translocation-scanner-v4/` (port 8780) | `translocation-scanner-v4.md` |
| Open WebUI shortcut | `/LLM` → `llm.23andclaude.com` | `llm-webui.md` |

Chapters 14 + 15 captured two intensive advisor cycles + the Wave 0
safety gate + Wave 1 foundations + Items 1–5 from the second
advisor's "second-opinion" review (continuous PC-regression
normalization, `--chr 1-22,X,Y,XY` filter, pgsc_calc sanity-check
harness, HGDP+1kGP unified panel). All implementation lives in the
sibling `23andClaude` repository — see its `CHANGELOG.md` for the
code-level history.

Headline live evidence captured during the session:

- ADHD PGS002746 EAS replay → previously rendered `null` percentile
  with an LLM-paraphrased catastrophizing message; now renders a
  deterministic `REF_STATS_SCHEMA_INVALID` reason + templated prose
  that names it as a *"pipeline data issue, not a biological finding."*
- pgsc_calc cross-validation on HG002 + PGS000004 → **78.1**
  percentile (PC-normalized); our pre-Item-4 discrete-bucket pipeline
  gave **99.5** (clamped from z=3.27). A 21 pp gap that the new
  PC-regression module closes.
- HGDP+1kGP panel staged on host (`/data/pgsc_refs/pgsc_HGDP+1kGP_v1/`,
  3,942 samples) — first time the platform has 158 Middle Eastern
  reference samples available.
