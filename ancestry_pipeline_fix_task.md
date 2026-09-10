# Ancestry Pipeline Fix Task

## Context

We have a genomic ancestry inference pipeline that uses Rye (NNLS on PCA) to decompose query samples against a 9-group reference panel (gnomAD HGDP + 1kGP, ~4,091 samples, ~240K LD-pruned variants, 20 PCs). The pipeline recently expanded from 8 to 9 groups by adding SoutheastAsian. AshkenaziJewish was tested as a 10th reference group but caused decomposition collapse (ASJ is admixed EU+ME and introduced collinearity), so it was reverted to a post-hoc pattern flag. The pipeline is mostly working but has several accuracy, correctness, and UX issues to fix.

## Known Samples for Validation

Use these as regression tests — all have known ancestry:

- **Nimo, Mina, Efi**: Ashkenazi Jewish. Should decompose as ~60-75% MiddleEastern, ~17-25% European, ~3-7% African. ASJ pattern flag should fire.
- **B2XH, B3XH**: Half Han Chinese, half Ashkenazi. Should show ~50% EastAsian plus the Ashkenazi ME+EU+AFR pattern on the other half. Currently EA is bleeding into SEA (~25% EA + 24% SEA instead of ~50% EA).

---

## Part A: Pipeline Fixes (in priority order)

### 1. Validate Rye PC Utilization

Check how many PCs the Rye decomposition is actually using during optimization. The pipeline computes 20 PCs but Rye may be configured to use fewer internally. EA vs SEA separation typically requires PC6+. If Rye is using fewer than 20, increase to use all 20. This alone may resolve the EA/SEA bleed.

### 2. Curate SEA Reference Samples

The SEA reference group overlaps with EA in PCA space, causing Han Chinese ancestry to split across both. Identify and remove SEA reference samples that are genetically close to Han Chinese (likely Dai and Kinh populations from HGDP). Approach:

- Plot EA and SEA reference samples on PC1-PC10.
- Identify SEA samples that fall within or very close to the EA cluster.
- Remove those samples from the SEA reference group.
- Re-run joint PCA with the pruned panel.

After curation, the B2XH/B3XH samples should show ~50% EA with minimal SEA leakage.

### 3. Reference Panel Self-Classification Validation

**This is critical and must run automatically every time the reference panel changes.**

After any panel modification (adding/removing groups or samples, re-running PCA), run Rye on every reference sample using leave-one-out and check that each sample classifies back to its own group. Report:

- Per-group accuracy (target: >90% for each group).
- Confusion matrix showing cross-misclassification rates.
- Flag any group pair with >10% cross-classification (e.g., EA↔SEA, EU↔ME).

If self-classification fails, the panel change should not be accepted. Store the confusion matrix output alongside the reference panel as a validation artifact.

### 4. Frontend Multiplier Bug

The pipeline outputs `primary_pct` as a value like `76` (already a percentage). The frontend is multiplying by 100 again, rendering `7600.0%`. Find and remove the redundant `* 100` in the UI presentation layer.

### 5. ROH Unit Test

Add a unit test on the PLINK ROH output parser that asserts:

- `total_mb` is less than 3,088 (size of human genome in Mb).
- `avg_kb` is less than 3,088,000.
- If either exceeds the threshold, fail with a clear error about unit mismatch.

This prevents regression of the previously fixed bp→Mb conversion bug.

### 6. Schema: Keep `pop_proportions`

Do not remove `pop_proportions` from the output JSON. It will be used for the sub-population display (see Part C). Begin populating it with fine-grained population proportions where available.

---

## Part B: Ancestry Signature Detection (Expanded)

### Overview

Expand the existing hardcoded ASJ pattern detection into a general-purpose, config-driven signature detection engine. This system identifies likely population labels from the raw Rye decomposition output and drives the two-tier display in the frontend (see Part C).

### Signature Config

Create a YAML or JSON config file. Each signature defines:

- `required`: Component ranges that must be present.
- `optional`: Components that may be present within range.
- `max_other`: Maximum total proportion in unlisted components.
- `sub_labels`: Human-readable labels for how each Rye component maps to a population-specific term within this signature context.
- `confidence_method`: `range_centrality` — confidence is high when proportions are centered in ranges, moderate at edges, low near boundaries.

```yaml
signatures:

  # Admixed populations (check these first — more specific)

  AshkenaziJewish:
    required:
      MiddleEastern: [0.35, 0.80]
      European: [0.15, 0.45]
    optional:
      African: [0.00, 0.10]
    max_other: 0.05
    sub_labels:
      MiddleEastern: "Levantine"
      European: "Southern European"
      African: "North African"

  SephardicJewish:
    required:
      MiddleEastern: [0.45, 0.85]
      European: [0.10, 0.35]
    optional:
      African: [0.02, 0.15]
    max_other: 0.05
    sub_labels:
      MiddleEastern: "Levantine / North African"
      European: "Southern European / Iberian"
      African: "North African"

  MizrahiJewish:
    required:
      MiddleEastern: [0.75, 0.95]
    optional:
      European: [0.00, 0.15]
      SouthAsian: [0.00, 0.10]
      African: [0.00, 0.10]
    max_other: 0.05
    sub_labels:
      MiddleEastern: "Mesopotamian / Levantine"
      European: "Mediterranean"

  Latino_Caribbean:
    required:
      European: [0.20, 0.70]
      American: [0.10, 0.50]
    optional:
      African: [0.05, 0.40]
    max_other: 0.10
    sub_labels:
      European: "Iberian / Southern European"
      American: "Indigenous American"
      African: "West African"

  Latino_Mestizo:
    required:
      European: [0.20, 0.65]
      American: [0.30, 0.70]
    optional:
      African: [0.00, 0.10]
    max_other: 0.05
    sub_labels:
      European: "Iberian / Southern European"
      American: "Indigenous American"

  Ethiopian:
    required:
      African: [0.40, 0.65]
      MiddleEastern: [0.35, 0.60]
    max_other: 0.05
    sub_labels:
      African: "East African"
      MiddleEastern: "Levantine / Arabian Peninsula"

  EthiopianJewish:
    required:
      African: [0.50, 0.75]
      MiddleEastern: [0.20, 0.45]
    optional:
      European: [0.00, 0.05]
    max_other: 0.05
    sub_labels:
      African: "East African / Beta Israel"
      MiddleEastern: "Levantine"

  Uyghur:
    required:
      EastAsian: [0.35, 0.65]
      European: [0.25, 0.55]
    optional:
      SouthAsian: [0.00, 0.10]
    max_other: 0.10
    sub_labels:
      EastAsian: "Central / East Asian"
      European: "West Eurasian"

  Malagasy:
    required:
      SoutheastAsian: [0.35, 0.65]
      African: [0.35, 0.65]
    max_other: 0.05
    sub_labels:
      SoutheastAsian: "Austronesian"
      African: "East African / Bantu"

  CapeColoured:
    required:
      African: [0.25, 0.55]
      European: [0.15, 0.35]
    optional:
      SouthAsian: [0.05, 0.25]
      SoutheastAsian: [0.05, 0.20]
    max_other: 0.10
    sub_labels:
      African: "Khoisan / Bantu"
      European: "Northern European"
      SouthAsian: "South Asian"
      SoutheastAsian: "Austronesian / Malay"

  AfricanAmerican:
    required:
      African: [0.60, 0.90]
      European: [0.10, 0.35]
    optional:
      American: [0.00, 0.05]
    max_other: 0.05
    sub_labels:
      African: "West / Central African"
      European: "Northwestern European"

  Hazara:
    required:
      EastAsian: [0.30, 0.55]
      SouthAsian: [0.25, 0.50]
    optional:
      European: [0.00, 0.15]
    max_other: 0.10
    sub_labels:
      EastAsian: "Mongolic / Central Asian"
      SouthAsian: "South Asian / Iranian"

  # Continental / unadmixed (check these last — broader, fallback)

  European:
    required:
      European: [0.85, 1.00]
    optional:
      Finnish: [0.00, 1.00]
    max_other: 0.10
    sub_labels:
      European: "European"
      Finnish: "Finnish"

  Finnish:
    required:
      Finnish: [0.70, 1.00]
    optional:
      European: [0.00, 0.30]
    max_other: 0.05
    sub_labels:
      Finnish: "Finnish"

  EastAsian:
    required:
      EastAsian: [0.85, 1.00]
    max_other: 0.10
    sub_labels:
      EastAsian: "East Asian"

  SoutheastAsian:
    required:
      SoutheastAsian: [0.70, 1.00]
    optional:
      EastAsian: [0.00, 0.20]
    max_other: 0.10
    sub_labels:
      SoutheastAsian: "Southeast Asian"

  SouthAsian:
    required:
      SouthAsian: [0.80, 1.00]
    optional:
      European: [0.00, 0.10]
      MiddleEastern: [0.00, 0.10]
    max_other: 0.15
    sub_labels:
      SouthAsian: "South Asian"

  MiddleEastern:
    required:
      MiddleEastern: [0.85, 1.00]
    max_other: 0.10
    sub_labels:
      MiddleEastern: "Middle Eastern"

  WestAfrican:
    required:
      African: [0.90, 1.00]
    max_other: 0.05
    sub_labels:
      African: "West / Central African"

  Oceanian:
    required:
      Oceanian: [0.70, 1.00]
    optional:
      SoutheastAsian: [0.00, 0.20]
    max_other: 0.10
    sub_labels:
      Oceanian: "Oceanian / Papuan"
```

### Detection Logic

1. After Rye decomposition, check the query proportions against all signatures.
2. Evaluate admixed/specific signatures first (ASJ, Latino, Ethiopian, etc.), then fall back to continental.
3. A signature matches when all `required` components are within range, all non-required/non-optional components sum to less than `max_other`.
4. Confidence: `high` if all required components are within the middle 50% of their ranges, `moderate` if within range but near edges, `low` if borderline.
5. Multiple signatures can match — rank by confidence and specificity (admixed > continental).
6. For multi-ancestry samples (e.g., half Chinese half Ashkenazi): detect each ancestry segment. If ~50% maps to one signature and ~50% to another, report both at their respective proportions.

### Output Schema Update

Add to the result JSON:

```json
{
  "detected_populations": [
    {
      "label": "Ashkenazi Jewish",
      "proportion": 1.0,
      "confidence": "high",
      "components": [
        {"rye_group": "MiddleEastern", "label": "Levantine", "proportion": 0.74},
        {"rye_group": "European", "label": "Southern European", "proportion": 0.23},
        {"rye_group": "African", "label": "North African", "proportion": 0.03}
      ]
    }
  ]
}
```

For a half-Chinese half-Ashkenazi sample:

```json
{
  "detected_populations": [
    {
      "label": "East Asian",
      "proportion": 0.50,
      "confidence": "high",
      "components": [
        {"rye_group": "EastAsian", "label": "Han Chinese", "proportion": 0.50}
      ]
    },
    {
      "label": "Ashkenazi Jewish",
      "proportion": 0.50,
      "confidence": "moderate",
      "components": [
        {"rye_group": "MiddleEastern", "label": "Levantine", "proportion": 0.35},
        {"rye_group": "European", "label": "Southern European", "proportion": 0.12},
        {"rye_group": "African", "label": "North African", "proportion": 0.03}
      ]
    }
  ]
}
```

If no signature matches, fall back to listing the raw Rye proportions with group labels only and `"label": "Unresolved Admixture"`.

---

## Part C: Frontend / UX

### C1. Two-Tier Ancestry Display

The results page should show ancestry in two visual layers:

**Top tier — Detected Population(s):**
A prominent bar or donut chart showing the detected population labels and their proportions. This is the human-readable summary. Examples:
- `100% Ashkenazi Jewish` (for Nimo)
- `50% East Asian · 50% Ashkenazi Jewish` (for B3XH)
- `100% European` (for a unadmixed EU sample)
- `Unresolved Admixture` (if no signature matched)

Include the confidence badge (high/moderate/low) next to each label.

**Bottom tier — Reference Component Breakdown:**
Below the top-tier bar, show an expandable or always-visible sub-breakdown of the raw Rye components using the `sub_labels` from the matched signature. This is the fine-grained view. For an Ashkenazi sample:

```
Ashkenazi Jewish (100%)                    [confidence: high]
├── Levantine              74%             ██████████████░░░░░░
├── Southern European      23%             █████░░░░░░░░░░░░░░
└── North African           3%             █░░░░░░░░░░░░░░░░░░
```

For a European sample with sub-population resolution (when `pop_proportions` is populated):

```
European (100%)                            [confidence: high]
├── Northwestern European  45%             █████████░░░░░░░░░░
├── Southern European      32%             ██████░░░░░░░░░░░░░
├── Finnish                18%             ████░░░░░░░░░░░░░░░
└── Eastern European        5%             █░░░░░░░░░░░░░░░░░░
```

For mixed:

```
East Asian (50%)                           [confidence: high]
├── Han Chinese            50%             ██████████░░░░░░░░░

Ashkenazi Jewish (50%)                     [confidence: moderate]
├── Levantine              35%             ███████░░░░░░░░░░░░
├── Southern European      12%             ██░░░░░░░░░░░░░░░░░
└── North African           3%             █░░░░░░░░░░░░░░░░░░
```

### C2. ROH / Inbreeding Section

Add a dedicated visual section for Runs of Homozygosity data. Display:

**Inbreeding Coefficient (F_ROH):**
Calculate `F_ROH = total_ROH_Mb / 3088` (genome size). Display as a single score from 0 to 1 with a color-coded gauge:
- `< 0.005` → Green — "No significant inbreeding detected"
- `0.005 – 0.02` → Yellow — "Elevated ROH consistent with population bottleneck or distant parental relatedness (e.g., 3rd–5th cousins)"
- `0.02 – 0.0625` → Orange — "Significant ROH consistent with close parental relatedness (e.g., 2nd cousins)"
- `> 0.0625` → Red — "High ROH consistent with very close parental relatedness (e.g., 1st cousins or closer)"

**ROH Summary Stats:**
Show in a clean card:
- Total ROH: XX.X Mb
- Number of segments: N
- Average segment length: XX.X kb
- Longest segment: XX.X Mb (add this to the pipeline output if not already present)

**Contextual Explanation:**
A brief, always-visible text block explaining what ROH means in plain language. Something like: "Runs of Homozygosity (ROH) are stretches of DNA where both copies are identical, inherited from a common ancestor. Longer and more numerous ROH segments indicate closer parental relatedness. Many populations (such as Ashkenazi Jewish, Finnish, and Amish) show elevated ROH due to historical population bottlenecks rather than recent consanguinity."

When ROH is `null` (e.g., BAM input where ROH analysis couldn't run), show: "ROH analysis not available for this sample. ROH detection requires VCF or gVCF input."

### C3. Real-Time Pipeline Progress

Replace the current single progress bar with a detailed process view showing what the pipeline is actually doing. Two display modes:

**Collapsed (default):** A progress bar with a descriptive status label that updates at each stage:
- `Extracting variants at reference positions...` (0-15%)
- `Aligning alleles with reference panel...` (15-25%)
- `Merging with reference panel...` (25-35%)
- `Applying QC filters...` (35-40%)
- `Computing principal components...` (40-55%)
- `Running ancestry decomposition...` (55-70%)
- `Detecting population signatures...` (70-75%)
- `Running ROH analysis...` (75-90%)
- `Generating results...` (90-100%)

The status label must update every time a new stage begins. The progress percentage should advance smoothly within each stage based on the actual subprocess progress (not jump from 8% to 60%).

**Expanded (click to expand):** A live process list showing individual threads/tasks, inspired by htop. For stages that parallelize by chromosome (variant extraction, ROH), show per-chromosome status:

```
Pipeline: Extracting variants                          [████████░░░░░░░░] 47%

PID    TASK                    CHR     STATUS      TIME
4012   bcftools mpileup        chr1    Running     0:42
4013   bcftools mpileup        chr2    Complete    0:38
4014   bcftools mpileup        chr3    Complete    0:35
4015   bcftools mpileup        chr4    Running     0:31
4016   bcftools mpileup        chr5    Queued      —
...
4033   bcftools mpileup        chr22   Queued      —
```

For non-parallel stages (PCA, Rye), show a single process entry with elapsed time.

**Implementation:** The backend should emit progress events via WebSocket or SSE. Each event contains:
```json
{
  "stage": "variant_extraction",
  "stage_label": "Extracting variants at reference positions",
  "progress_pct": 47,
  "tasks": [
    {"id": "chr1", "tool": "bcftools mpileup", "status": "running", "elapsed_s": 42},
    {"id": "chr2", "tool": "bcftools mpileup", "status": "complete", "elapsed_s": 38}
  ]
}
```

The frontend subscribes and updates both the collapsed and expanded views in real time. If the WebSocket disconnects, show "Reconnecting..." rather than freezing at a stale percentage.

---

## Do NOT Do

- Do not add AshkenaziJewish as a reference group. It was tested and broke the decomposition due to collinearity. Keep it as pattern detection only.
- Do not migrate to ADMIXTURE/fastSTRUCTURE. That is a future architecture change, not a fix for the current pipeline.
- Do not implement hierarchical (two-pass) decomposition. Try fixes A1 and A2 first. If EA/SEA bleed persists after both, revisit.

## Definition of Done

### Pipeline
- All five known samples (Nimo, Mina, Efi, B2XH, B3XH) produce sensible decompositions.
- Ashkenazi samples: >50% ME, 15-30% EU, <10% AFR, ASJ signature detected.
- Half-Chinese-half-Ashkenazi samples: ~50% EA with <10% SEA, remainder shows ME+EU Ashkenazi pattern.
- Self-classification accuracy >90% for all 9 reference groups.
- ROH unit test passes.
- Signature detection fires correctly for all known samples from config.
- `detected_populations` is populated in result JSON for all samples.

### Frontend
- Two-tier ancestry display renders correctly for unadmixed, admixed, and multi-ancestry samples.
- ROH section shows F_ROH gauge, summary stats, and contextual explanation. Handles null ROH gracefully.
- Frontend displays correct percentages (multiplier bug fixed).
- Pipeline progress shows descriptive stage labels, never stalls silently at a fixed percentage.
- Expanded process view shows per-chromosome task status for parallel stages.
- Progress events stream via WebSocket/SSE; frontend handles disconnection gracefully.
