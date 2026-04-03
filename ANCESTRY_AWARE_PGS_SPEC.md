# Ancestry-Aware PGS Pipeline — Coding Agent Spec

## Overview

Two additions to the existing genomics platform (FastAPI + React/Vite, plink2-based PGS scoring, 6 WGS samples):

1. **Ancestry inference** — run on each sample, store ancestry proportions, tag each sample with primary + admixed ancestry labels.
2. **Cross-ancestry PGS scoring** — use PRS-CSx for multi-population weight optimization + linear combination for admixed samples. Surface ancestry context clearly in the UI so users know which scores are well-calibrated vs. degraded for each sample.

---

## Part 1: Ancestry Inference & Tagging

### 1.1 Reference Panel Setup

```bash
# Download 1000 Genomes phase 3 reference panel (2,504 samples, 26 pops, 5 superpops)
# Location: /home/nimo/genomics/reference/1kg/

wget https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/supporting/GRCh38_positions/ALL.chr{1..22}.shapeit2_integrated_snvindels_v2a_27022019.GRCh38.phased.vcf.gz

# Also grab the sample-to-population mapping
wget https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel
```

Superpopulation labels to use: `EUR`, `EAS`, `AFR`, `SAS`, `AMR`

### 1.2 Merge & PCA Pipeline

```bash
# Step 1: Extract overlapping high-quality SNPs between our 6 samples and 1KG
# Use only autosomal, biallelic SNPs, MAF > 1%, missingness < 2%, LD-pruned

plink2 \
  --pfile our_samples \
  --extract-intersect 1kg_snps.txt \
  --maf 0.01 \
  --geno 0.02 \
  --indep-pairwise 1000 50 0.2 \
  --out pruned_snps

plink2 \
  --pfile our_samples \
  --extract pruned_snps.prune.in \
  --out our_pruned

# Step 2: Merge with 1KG reference (after matching strand, removing ambiguous A/T C/G SNPs)
plink2 \
  --pfile merged_our_plus_1kg \
  --pca 20 \
  --out ancestry_pca
```

This produces `ancestry_pca.eigenvec` (PC1-PC20 for all samples including 1KG reference).

### 1.3 Ancestry Proportion Estimation

Two approaches — run both:

**A) Supervised projection with a classifier (fast, deterministic labels)**

```python
# train_ancestry_classifier.py

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
import joblib

# Load PCA results
eigenvec = load_eigenvec("ancestry_pca.eigenvec")
panel = load_panel("integrated_call_samples_v3.20130502.ALL.panel")

# Split: 1KG samples = training, our 6 = prediction targets
ref_mask = eigenvec["sample_id"].isin(panel["sample"])
X_ref = eigenvec.loc[ref_mask, [f"PC{i}" for i in range(1, 11)]].values
y_ref = panel.set_index("sample").loc[eigenvec.loc[ref_mask, "sample_id"], "super_pop"].values

X_our = eigenvec.loc[~ref_mask, [f"PC{i}" for i in range(1, 11)]].values
our_ids = eigenvec.loc[~ref_mask, "sample_id"].values

clf = RandomForestClassifier(n_estimators=500, random_state=42)
clf.fit(X_ref, y_ref)

# Predict probabilities = ancestry proportions
probs = clf.predict_proba(X_our)  # shape: (6, 5) for 5 superpops
labels = clf.classes_  # ['AFR', 'AMR', 'EAS', 'EUR', 'SAS']

for i, sample in enumerate(our_ids):
    print(f"{sample}: {dict(zip(labels, probs[i].round(3)))}")

joblib.dump(clf, "ancestry_classifier.joblib")
```

**B) ADMIXTURE (unsupervised, for finer-grained proportions)**

```bash
# Convert to bed format (ADMIXTURE requires plink1 format)
plink2 --pfile merged_our_plus_1kg --extract pruned_snps.prune.in --make-bed --out for_admixture

# Run K=5 (matching 5 superpopulations)
admixture --cv for_admixture.bed 5 -j4

# Output: for_admixture.5.Q (proportions), for_admixture.5.P (allele freqs)
```

### 1.4 Data Model

```python
# models/ancestry.py

from pydantic import BaseModel

class AncestryProportions(BaseModel):
    EUR: float  # European
    EAS: float  # East Asian
    AFR: float  # African
    SAS: float  # South Asian
    AMR: float  # Admixed American / Native American

class SampleAncestry(BaseModel):
    sample_id: str
    proportions: AncestryProportions
    primary_ancestry: str        # superpop with highest proportion, e.g. "EUR"
    is_admixed: bool             # True if no single superpop > 0.85
    admixture_description: str   # e.g. "EUR/EAS admixed" or "EUR"
    pca_coordinates: list[float] # PC1-PC10 for plotting
    inference_method: str        # "RF_classifier" or "ADMIXTURE_K5"
```

### 1.5 Ancestry Tagging Rules

```python
def tag_ancestry(proportions: AncestryProportions) -> tuple[str, bool, str]:
    """Returns (primary_ancestry, is_admixed, description)"""
    props = proportions.dict()
    sorted_pops = sorted(props.items(), key=lambda x: -x[1])
    primary = sorted_pops[0][0]
    primary_frac = sorted_pops[0][1]

    if primary_frac >= 0.85:
        return primary, False, primary

    # Admixed: list all components > 10%
    components = [(pop, frac) for pop, frac in sorted_pops if frac >= 0.10]
    desc = "/".join(f"{pop}" for pop, _ in components)
    return primary, True, f"{desc} admixed"
```

### 1.6 Backend API

```python
# routes/ancestry.py

@router.get("/api/samples/{sample_id}/ancestry")
async def get_sample_ancestry(sample_id: str) -> SampleAncestry:
    ...

@router.get("/api/ancestry/pca")
async def get_pca_plot_data() -> list[PCAPoint]:
    """Returns PC1/PC2 for all 1KG ref + our 6 samples, labeled by superpop."""
    ...

@router.get("/api/ancestry/all")
async def get_all_ancestries() -> list[SampleAncestry]:
    """Returns ancestry for all 6 samples."""
    ...
```

### 1.7 Store in DB

```sql
CREATE TABLE sample_ancestry (
    sample_id TEXT PRIMARY KEY,
    eur_proportion REAL,
    eas_proportion REAL,
    afr_proportion REAL,
    sas_proportion REAL,
    amr_proportion REAL,
    primary_ancestry TEXT,
    is_admixed BOOLEAN,
    admixture_description TEXT,
    pc1 REAL, pc2 REAL, pc3 REAL, pc4 REAL, pc5 REAL,
    pc6 REAL, pc7 REAL, pc8 REAL, pc9 REAL, pc10 REAL,
    inference_method TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Part 2: Cross-Ancestry PGS Scoring

### 2.1 Install PRS-CSx

```bash
cd /home/nimo/genomics/tools/
git clone https://github.com/getian107/PRScsx.git
cd PRScsx

# Download LD reference panels (these are large, ~15GB each)
# EUR panel:
wget https://personal.broadinstitute.org/hhuang/public/PRS-CSx/Reference/ldblk_1kg_eur.tar.gz
# EAS panel:
wget https://personal.broadinstitute.org/hhuang/public/PRS-CSx/Reference/ldblk_1kg_eas.tar.gz

tar -xzf ldblk_1kg_eur.tar.gz
tar -xzf ldblk_1kg_eas.tar.gz

# Also need the SNP info file
wget https://personal.broadinstitute.org/hhuang/public/PRS-CSx/Reference/snpinfo_mult_1kg_hm3
```

### 2.2 Obtain GWAS Summary Stats

For each trait/disease in the checklist, obtain summary statistics from both EUR and EAS populations where available.

Primary sources:
- **EUR**: UKB, GLGC, CARDIoGRAM, PGC, DIAGRAM, etc.
- **EAS**: Biobank Japan (BBJ), China Kadoorie Biobank, Taiwan Biobank

Format required by PRS-CSx (tab-separated):
```
SNP          A1    A2    BETA    P
rs12345      A     G     0.023   1.2e-8
```

```python
# scripts/fetch_gwas_sumstats.py

GWAS_SOURCES = {
    "CAD": {
        "EUR": "CARDIoGRAM_2022_EUR.txt.gz",
        "EAS": "BBJ_CAD_EAS.txt.gz",
    },
    "T2D": {
        "EUR": "DIAGRAM_2022_EUR.txt.gz",
        "EAS": "BBJ_T2D_EAS.txt.gz",
    },
    "breast_cancer": {
        "EUR": "BCAC_2020_EUR.txt.gz",
        "EAS": "ABCC_2020_EAS.txt.gz",
    },
    # ... add for each trait in checklist
}

# Store at: /home/nimo/genomics/gwas_sumstats/{trait}/{pop}.txt.gz
```

### 2.3 PRS-CSx Execution Per Trait

```bash
#!/bin/bash
# scripts/run_prscsx.sh
# Usage: ./run_prscsx.sh <trait_name>

TRAIT=$1
PRSCSX=/home/nimo/genomics/tools/PRScsx/PRScsx.py
REF_DIR=/home/nimo/genomics/tools/PRScsx
SUMSTATS_DIR=/home/nimo/genomics/gwas_sumstats
OUT_DIR=/home/nimo/genomics/prs_csx_output/${TRAIT}
BIM=/home/nimo/genomics/data/all_samples.bim  # target sample SNP list

mkdir -p ${OUT_DIR}

python ${PRSCSX} \
  --ref_dir=${REF_DIR} \
  --bim_prefix=${BIM%.bim} \
  --sst_file=${SUMSTATS_DIR}/${TRAIT}/EUR.txt.gz,${SUMSTATS_DIR}/${TRAIT}/EAS.txt.gz \
  --n_gwas=400000,200000 \
  --pop=EUR,EAS \
  --out_dir=${OUT_DIR} \
  --out_name=${TRAIT} \
  --chrom=1-22 \
  --phi=1e-2  # global shrinkage; can auto-tune with --phi=auto but slower

# Output: {TRAIT}_EUR_pst_eff_a1_b0.5_phi1e-02_chr*.txt
#         {TRAIT}_EAS_pst_eff_a1_b0.5_phi1e-02_chr*.txt
# These are ancestry-specific posterior effect sizes
```

### 2.4 Score Computation

```bash
# For each population-specific weight file from PRS-CSx, compute raw PGS per sample

# Concatenate per-chrom files
cat ${OUT_DIR}/${TRAIT}_EUR_pst_eff_a1_b0.5_phi1e-02_chr*.txt > ${OUT_DIR}/${TRAIT}_EUR_weights.txt
cat ${OUT_DIR}/${TRAIT}_EAS_pst_eff_a1_b0.5_phi1e-02_chr*.txt > ${OUT_DIR}/${TRAIT}_EAS_weights.txt

# Score with plink2
for POP in EUR EAS; do
  plink2 \
    --pfile /home/nimo/genomics/data/all_samples \
    --score ${OUT_DIR}/${TRAIT}_${POP}_weights.txt 2 4 6 cols=+scoresums \
    --out ${OUT_DIR}/${TRAIT}_${POP}_scores
done

# Output: {TRAIT}_EUR_scores.sscore, {TRAIT}_EAS_scores.sscore
```

### 2.5 Linear Combination for Each Sample

```python
# scripts/combine_ancestry_scores.py

import pandas as pd
from models.ancestry import SampleAncestry

def compute_combined_pgs(
    trait: str,
    sample_ancestry: SampleAncestry,
    eur_score: float,
    eas_score: float,
    # Add more pops as available
) -> dict:
    """
    For each sample, compute:
    1. ancestry-optimal combined score (weighted by admixture proportions)
    2. individual population scores for comparison
    3. confidence flag based on ancestry match
    """
    props = sample_ancestry.proportions

    # Weighted linear combination
    # Only combine populations where we have both GWAS sumstats AND PRS-CSx weights
    combined = (props.EUR * eur_score) + (props.EAS * eas_score)

    # For populations we don't have weights for, their contribution is unscored.
    # Track what fraction of ancestry is "covered" by available GWAS
    covered_fraction = props.EUR + props.EAS
    uncovered_fraction = 1.0 - covered_fraction

    # Confidence tier
    primary = sample_ancestry.primary_ancestry
    if primary in ("EUR", "EAS") and not sample_ancestry.is_admixed:
        confidence = "high"
    elif covered_fraction >= 0.80:
        confidence = "moderate"
    else:
        confidence = "low"  # significant ancestry not represented in GWAS

    return {
        "trait": trait,
        "sample_id": sample_ancestry.sample_id,
        "combined_score": combined,
        "eur_component_score": eur_score,
        "eas_component_score": eas_score,
        "eur_weight_used": props.EUR,
        "eas_weight_used": props.EAS,
        "covered_fraction": covered_fraction,
        "confidence": confidence,
        "method": "PRS-CSx_linear_combination",
    }
```

### 2.6 Percentile Computation (Ancestry-Matched)

```python
# scripts/percentile_computation.py

import numpy as np

# Precompute reference distributions from 1KG samples
# For each trait, score all 1KG samples with the same PRS-CSx weights,
# then build per-superpop percentile distributions.

def compute_percentile(
    score: float,
    primary_ancestry: str,
    reference_scores: dict[str, np.ndarray],  # {"EUR": array, "EAS": array, ...}
) -> dict:
    """
    Place the sample's score against the appropriate reference distribution.
    """
    if primary_ancestry in reference_scores:
        ref = reference_scores[primary_ancestry]
        percentile = (np.sum(ref < score) / len(ref)) * 100
        ref_pop = primary_ancestry
    else:
        # Fallback to EUR (most scores derived there) but flag it
        ref = reference_scores.get("EUR", reference_scores[list(reference_scores.keys())[0]])
        percentile = (np.sum(ref < score) / len(ref)) * 100
        ref_pop = "EUR (fallback)"

    return {
        "percentile": round(percentile, 1),
        "reference_population": ref_pop,
        "reference_n": len(ref),
    }
```

### 2.7 Data Model for Scored Results

```python
# models/pgs_result.py

class PGSResult(BaseModel):
    sample_id: str
    trait: str
    pgs_id: str                     # from checklist, e.g. "PGS003725"
    scoring_method: str             # "plink2_standard" | "PRSCSx_combined" | "PRSCSx_EUR" | "PRSCSx_EAS"

    # Scores
    raw_score: float
    combined_score: float | None    # only for PRSCSx combined
    eur_component: float | None
    eas_component: float | None

    # Percentile
    percentile: float
    reference_population: str       # which pop distribution was used
    reference_n: int

    # Ancestry context
    sample_ancestry: str            # e.g. "EUR", "EUR/EAS admixed"
    confidence: str                 # "high" | "moderate" | "low"
    covered_fraction: float         # what fraction of ancestry is covered by available GWAS
    ancestry_warnings: list[str]    # any caveats

    # PGS metadata
    pgs_training_pop: str           # original PGS training population
    pgs_training_pop_match: bool    # does training pop match sample ancestry?
```

```sql
CREATE TABLE pgs_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT NOT NULL,
    trait TEXT NOT NULL,
    pgs_id TEXT NOT NULL,
    scoring_method TEXT NOT NULL,
    raw_score REAL,
    combined_score REAL,
    eur_component REAL,
    eas_component REAL,
    percentile REAL,
    reference_population TEXT,
    reference_n INTEGER,
    confidence TEXT CHECK(confidence IN ('high', 'moderate', 'low')),
    covered_fraction REAL,
    ancestry_warnings TEXT,  -- JSON array
    pgs_training_pop TEXT,
    pgs_training_pop_match BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sample_id) REFERENCES sample_ancestry(sample_id),
    UNIQUE(sample_id, pgs_id, scoring_method)
);
```

---

## Part 3: UI Requirements

### 3.1 Ancestry Dashboard (New Page: `/ancestry`)

**PCA Scatter Plot (main visual)**
- Plot PC1 vs PC2
- 1KG reference samples as small dots, colored by superpop (EUR=blue, EAS=green, AFR=orange, SAS=purple, AMR=red)
- Our 6 samples as large labeled markers (star or diamond shape)
- Hovering on our samples shows ancestry proportions tooltip
- Toggle: PC1/PC2 vs PC1/PC3 vs PC2/PC3

**Ancestry Summary Table**
| Sample | Primary | EUR | EAS | AFR | SAS | AMR | Status |
|--------|---------|-----|-----|-----|-----|-----|--------|
| Nimo   | EUR     | 0.87| 0.02| 0.01| 0.08| 0.02| Single-ancestry |
| Chichi | EAS     | 0.05| 0.91| 0.01| 0.02| 0.01| Single-ancestry |
| ...    |         |     |     |     |     |     |                 |

Each row has a colored ancestry bar (stacked horizontal bar chart, one per sample).

**Admixture Bar Chart**
- Standard ADMIXTURE-style stacked bar chart (K=5), one bar per sample, 5 colors.

### 3.2 PGS Results Page — Ancestry Integration

For every PGS result displayed, add:

**Ancestry Confidence Badge** — shown next to each score:

```
┌─────────────────────────────────────────────────────┐
│  CAD Risk (PGS003725)                               │
│                                                      │
│  Percentile: 72nd    Score: 0.342                   │
│                                                      │
│  ● HIGH CONFIDENCE         [i]                      │
│  PRS-CSx combined (EUR+EAS)                         │
│  Ref: EUR (n=503)                                   │
│  Ancestry coverage: 98%                             │
└─────────────────────────────────────────────────────┘
```

Badge rules:
- 🟢 `HIGH` — primary ancestry matches training pop, or PRS-CSx combined with >85% coverage
- 🟡 `MODERATE` — PRS-CSx combined with 50-85% coverage, or multi-ancestry PGS used
- 🔴 `LOW` — EUR-only PGS applied to non-EUR sample without PRS-CSx correction
- Gray info icon `[i]` expands to show: scoring method, which GWAS sumstats used, ancestry proportions applied

**Score Comparison View** (expandable per trait):

```
CAD — Nimo (EUR)
├── PRS-CSx combined:    72nd percentile (ref: EUR, n=503)   🟢
├── EUR component:       71st percentile                      
├── EAS component:       68th percentile                      
└── Standard PGS003725:  70th percentile (ref: EUR, n=503)   🟢

CAD — Chichi (EAS)
├── PRS-CSx combined:    45th percentile (ref: EAS, n=504)   🟢
├── EUR component:       52nd percentile                      
├── EAS component:       43rd percentile                      
└── Standard PGS003725:  58th percentile (ref: EUR, n=503)   🔴 ⚠️ ancestry mismatch
```

The `⚠️ ancestry mismatch` flag appears whenever a EUR-trained PGS is applied to a sample whose primary ancestry is not EUR. Tooltip: "This score was trained in European populations. Percentile ranking against a European reference may not be accurate for this sample's ancestry."

### 3.3 Sample Selector — Ancestry Context

Wherever a sample selector dropdown appears in the UI, show ancestry inline:

```
▼ Select Sample
  ┌──────────────────────────────┐
  │ Nimo          EUR        🟢 │
  │ Chichi        EAS        🟢 │
  │ Mina          EUR        🟢 │
  │ Efi           EUR        🟢 │
  │ B2XH          EUR        🟢 │
  │ B3XH          EUR        🟢 │
  └──────────────────────────────┘
```

The dot color reflects how many PGS in the current view have high/moderate/low confidence for that sample.

### 3.4 PGS Checklist Table — Ancestry Column

Add a column to the master checklist view:

| Done | Trait | PGS ID | Training Pop | EAS GWAS Available | PRS-CSx Run | Confidence (per sample) |
|------|-------|--------|--------------|--------------------|-------------|------------------------|
| ✓    | CAD   | PGS003725 | Multi     | ✓                  | ✓           | 🟢🟢🟢🟢🟢🟢        |
| ✓    | T2D   | PGS002308 | Multi     | ✓                  | ✓           | 🟢🟢🟢🟢🟢🟢        |
|      | Melanoma | PGS000743 | EUR    | ✗                  | ✗           | 🟢🟢🟢🟢🟢🔴        |

The 6 dots under "Confidence" are one per sample in fixed order. Quick visual scan of which traits have ancestry coverage gaps.

---

## Part 4: Pipeline Orchestration

### 4.1 Execution Order

```
1. Ancestry inference (run once, before any PGS scoring)
   ├── PCA with 1KG reference
   ├── RF classifier → ancestry proportions
   ├── ADMIXTURE K=5 → proportions (cross-validate with RF)
   ├── Store in sample_ancestry table
   └── Generate PCA plot data

2. For each trait in checklist:
   ├── Check: do we have GWAS sumstats for EUR? EAS? Other?
   ├── If EUR-only sumstats available:
   │   ├── Run standard plink2 scoring with catalog weights
   │   ├── Flag non-EUR samples as confidence=low
   │   └── Compute percentiles against EUR reference only
   ├── If EUR+EAS sumstats available:
   │   ├── Run PRS-CSx → ancestry-specific weights
   │   ├── Score all samples with both weight sets via plink2
   │   ├── Compute linear combination per sample using ancestry proportions
   │   ├── Compute percentiles against ancestry-matched reference
   │   └── Set confidence based on covered_fraction
   └── Store all results in pgs_results table

3. UI renders results with ancestry badges and comparison views
```

### 4.2 File Structure

```
/home/nimo/genomics/
├── reference/
│   └── 1kg/                          # 1000 Genomes reference
├── tools/
│   └── PRScsx/                       # PRS-CSx installation + LD ref panels
├── gwas_sumstats/
│   └── {trait}/
│       ├── EUR.txt.gz
│       └── EAS.txt.gz
├── prs_csx_output/
│   └── {trait}/
│       ├── {trait}_EUR_weights.txt    # concatenated per-chrom weights
│       ├── {trait}_EAS_weights.txt
│       ├── {trait}_EUR_scores.sscore  # plink2 output
│       └── {trait}_EAS_scores.sscore
├── ancestry/
│   ├── ancestry_pca.eigenvec
│   ├── ancestry_classifier.joblib
│   ├── for_admixture.5.Q
│   └── reference_score_distributions/
│       ├── EUR/                       # 1KG EUR scores per trait (for percentiles)
│       └── EAS/
├── platform/
│   ├── backend/
│   │   ├── models/
│   │   │   ├── ancestry.py
│   │   │   └── pgs_result.py
│   │   ├── routes/
│   │   │   ├── ancestry.py
│   │   │   └── pgs.py
│   │   └── scripts/
│   │       ├── run_ancestry_inference.py
│   │       ├── run_prscsx.sh
│   │       ├── combine_ancestry_scores.py
│   │       └── percentile_computation.py
│   └── frontend/
│       ├── src/pages/
│       │   ├── AncestryDashboard.tsx
│       │   └── PGSResults.tsx
│       └── src/components/
│           ├── PCAPlot.tsx
│           ├── AdmixtureBar.tsx
│           ├── AncestryBadge.tsx
│           ├── ScoreComparisonView.tsx
│           └── ConfidenceDots.tsx
```

### 4.3 GWAS Summary Stats Priority List

Start with traits that have both EUR and EAS GWAS available (highest value for cross-ancestry scoring):

| Priority | Trait | EUR Source | EAS Source |
|----------|-------|------------|------------|
| 1 | CAD | CARDIoGRAM+C4D | BBJ |
| 2 | T2D | DIAGRAM | BBJ |
| 3 | Stroke | MEGASTROKE | BBJ |
| 4 | Breast cancer | BCAC | ABCC |
| 5 | Colorectal cancer | GECCO | BBJ |
| 6 | BMI | GIANT | BBJ |
| 7 | LDL/HDL/TG | GLGC | BBJ |
| 8 | Atrial fibrillation | AFGen | BBJ |
| 9 | Prostate cancer | PRACTICAL | BBJ |
| 10 | Schizophrenia | PGC | PGC-EAS |
| 11 | MDD | PGC | PGC + China |
| 12 | Gout/urate | CKDGen | BBJ |
| 13 | Height | GIANT | BBJ |
| 14 | Blood pressure | ICBP | BBJ |
| 15 | CKD/eGFR | CKDGen | BBJ |

Traits without EAS GWAS: run standard EUR PGS only, flag as low confidence for non-EUR samples.

---

## Part 5: Edge Cases & Notes

1. **SAS/AMR ancestry**: If any sample has significant SAS or AMR proportion, PRS-CSx supports SAS and AMR LD panels too. Download those panels and add as additional populations in the `--pop` flag. For now, start with EUR+EAS since those cover the samples.

2. **Ambiguous A/T and C/G SNPs**: Remove these before merging with 1KG to avoid strand issues. They're a small fraction of SNPs and not worth the alignment risk.

3. **PRS-CSx runtime**: ~2-4 hours per trait on a single core. Parallelize across chromosomes with `--chrom` flag, or run traits in parallel across cores. On the A100 instance this should be fine.

4. **phi parameter**: Start with `--phi=1e-2`. If results look off, try `--phi=auto` (slower, uses a grid search). Document which phi was used per trait.

5. **When PRS-CSx weights differ substantially from PGS Catalog weights**: This is expected and fine. PRS-CSx re-estimates effect sizes jointly across populations. The catalog weights are still useful as a "standard EUR" comparison baseline. Show both in the UI.

6. **Sample size in `--n_gwas`**: Use the effective sample size from each GWAS. For case-control studies: `n_eff = 4 / (1/n_cases + 1/n_controls)`.
