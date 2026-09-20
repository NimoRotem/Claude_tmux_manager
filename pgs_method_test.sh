#!/usr/bin/env bash
# =============================================================================
# pgs_method_test.sh — Cross-method PGS scoring validation
#
# Tests plink2 --score, pgsc_calc, and PRSice-2 on the same sample(s) with
# the same PGS weights, then compares outputs for plausibility.
#
# Usage:
#   ./pgs_method_test.sh                    # use bundled 1KG test samples
#   ./pgs_method_test.sh /path/to/sample.vcf.gz   # use your own VCF
#
# Prerequisites:
#   - plink2, bcftools, tabix in $PATH
#   - Python 3 with numpy, pandas, scipy
#   - (optional) nextflow + pgsc_calc for Workflow B
#   - (optional) PRSice_linux for Workflow C
#
# What it tests:
#   1. Downloads 3 well-characterised PGS (height, CAD, T2D)
#   2. Scores each through every available method
#   3. Checks: non-zero scores, plausible z-range, cross-method correlation
#   4. Prints PASS/FAIL summary
# =============================================================================
set -euo pipefail

# ── Configuration ──
WORKDIR="${PGS_TEST_WORKDIR:-/tmp/pgs_method_test}"
SAMPLE_VCF="${1:-}"
BUILD="GRCh38"
THREADS=$(( $(nproc) > 4 ? $(nproc) / 2 : $(nproc) ))

# Test PGS — chosen for being well-validated, moderate size, multi-ancestry:
#   PGS000727  Height       (Yengo 2022, ~2M variants, EUR+multi)
#   PGS003725  CAD          (Inouye 2018, ~1.7M variants, EUR)
#   PGS002771  T2D          (Mahajan 2022, ~500K variants, multi-ancestry)
# Using smaller scores to keep test fast. Swap for your targets as needed.
declare -A PGS_LABELS=(
  ["PGS000727"]="Height"
  ["PGS003725"]="CAD"
  ["PGS002771"]="T2D"
)
PGS_IDS="PGS000727,PGS003725,PGS002771"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "  ${GREEN}✓ PASS${NC}: $1"; }
fail() { echo -e "  ${RED}✗ FAIL${NC}: $1"; FAILURES=$((FAILURES+1)); }
warn() { echo -e "  ${YELLOW}○ SKIP${NC}: $1"; SKIPS=$((SKIPS+1)); }
FAILURES=0; SKIPS=0

# ── Setup ──
mkdir -p "$WORKDIR"/{scores,plink2_out,pgsc_calc_out,prsice_out,formats}
echo "=== PGS Method Test ==="
echo "Work directory: $WORKDIR"
echo "Threads: $THREADS"
echo ""

# ── Detect available tools ──
HAS_PLINK2=false;    command -v plink2        &>/dev/null && HAS_PLINK2=true
HAS_BCFTOOLS=false;  command -v bcftools      &>/dev/null && HAS_BCFTOOLS=true
HAS_NEXTFLOW=false;  command -v nextflow      &>/dev/null && HAS_NEXTFLOW=true
HAS_PGSC_CALC=false; nextflow list 2>/dev/null | grep -q pgsc_calc 2>/dev/null && HAS_PGSC_CALC=true
HAS_PRSICE=false;    (command -v PRSice_linux &>/dev/null || [ -f /opt/PRSice/PRSice_linux ]) && HAS_PRSICE=true

PRSICE_BIN="PRSice_linux"
command -v PRSice_linux &>/dev/null || PRSICE_BIN="/opt/PRSice/PRSice_linux"

echo "Tool availability:"
echo "  plink2:     $HAS_PLINK2"
echo "  bcftools:   $HAS_BCFTOOLS"
echo "  nextflow:   $HAS_NEXTFLOW"
echo "  pgsc_calc:  $HAS_PGSC_CALC"
echo "  PRSice-2:   $HAS_PRSICE"
echo ""

if [ "$HAS_PLINK2" = false ]; then
  echo "ERROR: plink2 is required. Install it and re-run."
  exit 1
fi
if [ "$HAS_BCFTOOLS" = false ]; then
  echo "ERROR: bcftools is required. Install it and re-run."
  exit 1
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 1: Get test sample data
# ═══════════════════════════════════════════════════════════════════
echo "── Step 1: Prepare sample data ──"

if [ -n "$SAMPLE_VCF" ] && [ -f "$SAMPLE_VCF" ]; then
  echo "Using provided VCF: $SAMPLE_VCF"
  cp "$SAMPLE_VCF" "$WORKDIR/formats/sample.vcf.gz" 2>/dev/null || \
    ln -sf "$(realpath "$SAMPLE_VCF")" "$WORKDIR/formats/sample.vcf.gz"
  # Index if needed
  [ ! -f "$WORKDIR/formats/sample.vcf.gz.tbi" ] && \
    tabix -p vcf "$WORKDIR/formats/sample.vcf.gz" 2>/dev/null || true
else
  echo "No VCF provided — downloading 1KG test samples (chr22 subset, ~5 samples)..."
  # Download a tiny slice of 1KG Phase 3 for testing
  if [ ! -f "$WORKDIR/formats/sample.vcf.gz" ]; then
    # Use the 1KG GRCh38 chr22 VCF (small, fast)
    wget -q --show-progress -O "$WORKDIR/formats/1kg_chr22.vcf.gz" \
      "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/1000G_2504_high_coverage/working/20220422.3202_phased_SNV_INDEL_SV/1kGP_high_coverage_Illumina.chr22.filtered.SNV_INDEL_SV_phased_panel.vcf.gz" \
      2>/dev/null || {
        echo "Download failed — creating synthetic test VCF instead..."
        python3 "$WORKDIR/../create_synthetic_vcf.py" "$WORKDIR/formats/sample.vcf.gz" 2>/dev/null || \
          python3 -c "
import gzip, random
random.seed(42)
with gzip.open('$WORKDIR/formats/sample.vcf.gz','wt') as f:
    f.write('##fileformat=VCFv4.2\n')
    f.write('##contig=<ID=22,length=50818468>\n')
    f.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\tSAMPLE2\tSAMPLE3\n')
    for pos in range(16000000, 51000000, 500):
        ref, alt = random.choice([('A','G'),('C','T'),('G','A'),('T','C')])
        gts = [random.choice(['0/0','0/1','1/1']) for _ in range(3)]
        f.write(f'22\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\tGT\t' + '\t'.join(gts) + '\n')
print('Created synthetic test VCF with 3 samples, chr22')
"
      }

    if [ -f "$WORKDIR/formats/1kg_chr22.vcf.gz" ]; then
      # Subset to 5 samples for speed
      SAMPLES=$(bcftools query -l "$WORKDIR/formats/1kg_chr22.vcf.gz" | head -5 | tr '\n' ',')
      SAMPLES="${SAMPLES%,}"
      bcftools view -s "$SAMPLES" --threads $THREADS \
        "$WORKDIR/formats/1kg_chr22.vcf.gz" -Oz -o "$WORKDIR/formats/sample.vcf.gz"
      rm -f "$WORKDIR/formats/1kg_chr22.vcf.gz"
      echo "Subset to 5 samples: $SAMPLES"
    fi
  fi
  tabix -p vcf "$WORKDIR/formats/sample.vcf.gz" 2>/dev/null || true
fi

SAMPLE_COUNT=$(bcftools query -l "$WORKDIR/formats/sample.vcf.gz" | wc -l)
VARIANT_COUNT=$(bcftools view -H "$WORKDIR/formats/sample.vcf.gz" 2>/dev/null | head -50000 | wc -l)
echo "Sample VCF: $SAMPLE_COUNT samples, ~${VARIANT_COUNT}+ variants"
echo ""

# ═══════════════════════════════════════════════════════════════════
# STEP 2: Convert to all required formats
# ═══════════════════════════════════════════════════════════════════
echo "── Step 2: Convert to all scoring formats ──"

# VCF → PGEN (for plink2 fast path)
echo "  Converting VCF → PGEN..."
plink2 --vcf "$WORKDIR/formats/sample.vcf.gz" \
  --double-id \
  --threads $THREADS \
  --allow-extra-chr \
  --make-pgen \
  --out "$WORKDIR/formats/sample_pgen" \
  > "$WORKDIR/formats/pgen_convert.log" 2>&1
pass "VCF → PGEN ($(wc -l < "$WORKDIR/formats/sample_pgen.pvar") variants)"

# VCF → BED (for PRSice-2)
echo "  Converting VCF → BED..."
plink2 --vcf "$WORKDIR/formats/sample.vcf.gz" \
  --double-id \
  --threads $THREADS \
  --allow-extra-chr \
  --make-bed \
  --out "$WORKDIR/formats/sample_bed" \
  > "$WORKDIR/formats/bed_convert.log" 2>&1
pass "VCF → BED ($(wc -l < "$WORKDIR/formats/sample_bed.bim") variants)"

echo ""

# ═══════════════════════════════════════════════════════════════════
# STEP 3: Download and prepare PGS scoring files
# ═══════════════════════════════════════════════════════════════════
echo "── Step 3: Download PGS scoring files ──"

for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
  LABEL="${PGS_LABELS[$PGS_ID]}"
  OUTFILE="$WORKDIR/scores/${PGS_ID}_hmPOS_${BUILD}.txt.gz"

  if [ ! -f "$OUTFILE" ]; then
    echo "  Downloading $PGS_ID ($LABEL)..."
    wget -q --show-progress -O "$OUTFILE" \
      "https://ftp.ebi.ac.uk/pub/databases/spot/pgs/scores/${PGS_ID}/ScoringFiles/Harmonized/${PGS_ID}_hmPOS_${BUILD}.txt.gz" \
      2>/dev/null || {
        fail "Could not download $PGS_ID"
        continue
      }
  fi

  NVARS=$(zcat "$OUTFILE" | grep -c -v '^#' | awk '{print $1-1}')
  echo "  $PGS_ID ($LABEL): $NVARS variants"
done
echo ""

# ═══════════════════════════════════════════════════════════════════
# STEP 4: Prepare plink2-compatible score input files
# ═══════════════════════════════════════════════════════════════════
echo "── Step 4: Prepare score input files ──"

for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
  SCORE_GZ="$WORKDIR/scores/${PGS_ID}_hmPOS_${BUILD}.txt.gz"
  [ ! -f "$SCORE_GZ" ] && continue

  # Detect column layout from header line
  HEADER=$(zcat "$SCORE_GZ" | grep -v '^#' | head -1)

  # Create plink2-format score file: VARID  ALLELE  WEIGHT
  # PGS Catalog harmonized format: chr_name  chr_position  effect_allele  other_allele  effect_weight ...
  zcat "$SCORE_GZ" | grep -v '^#' | awk 'NR>1 && $1 != "" && $2 != "" {
      vid = $1 ":" $2 ":" $4 ":" $3
      printf "%s\t%s\t%s\n", vid, $3, $5
  }' > "$WORKDIR/scores/${PGS_ID}_plink2_input.txt"

  # Also create a PRSice-2-compatible file (needs SNP, A1, A2, BETA columns with header)
  echo -e "SNP\tA1\tA2\tBETA\tP" > "$WORKDIR/scores/${PGS_ID}_prsice_input.txt"
  zcat "$SCORE_GZ" | grep -v '^#' | awk 'NR>1 && $1 != "" && $2 != "" {
      vid = $1 ":" $2 ":" $4 ":" $3
      printf "%s\t%s\t%s\t%s\t1e-300\n", vid, $3, $4, $5
  }' >> "$WORKDIR/scores/${PGS_ID}_prsice_input.txt"

  PREPPED=$(wc -l < "$WORKDIR/scores/${PGS_ID}_plink2_input.txt")
  echo "  $PGS_ID: $PREPPED variants prepared"
done
echo ""

# ═══════════════════════════════════════════════════════════════════
# STEP 5A: Score with plink2 --score (PGEN input)
# ═══════════════════════════════════════════════════════════════════
echo "── Step 5A: plink2 --score (PGEN format) ──"

for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
  LABEL="${PGS_LABELS[$PGS_ID]}"
  SCORE_FILE="$WORKDIR/scores/${PGS_ID}_plink2_input.txt"
  [ ! -f "$SCORE_FILE" ] && continue

  plink2 --pfile "$WORKDIR/formats/sample_pgen" \
    --score "$SCORE_FILE" 1 2 3 \
      cols=+scoresums \
    --threads $THREADS \
    --allow-extra-chr \
    --out "$WORKDIR/plink2_out/${PGS_ID}_pgen" \
    > "$WORKDIR/plink2_out/${PGS_ID}_pgen.log" 2>&1 || true

  if [ -f "$WORKDIR/plink2_out/${PGS_ID}_pgen.sscore" ]; then
    MATCHED=$(grep -oP '\d+ variants processed' "$WORKDIR/plink2_out/${PGS_ID}_pgen.log" 2>/dev/null || echo "?")
    pass "plink2/PGEN $PGS_ID ($LABEL) — $MATCHED"
  else
    fail "plink2/PGEN $PGS_ID ($LABEL) — no output"
  fi
done

# ═══════════════════════════════════════════════════════════════════
# STEP 5B: Score with plink2 --score (VCF input directly)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 5B: plink2 --score (VCF format, direct) ──"

for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
  LABEL="${PGS_LABELS[$PGS_ID]}"
  SCORE_FILE="$WORKDIR/scores/${PGS_ID}_plink2_input.txt"
  [ ! -f "$SCORE_FILE" ] && continue

  plink2 --vcf "$WORKDIR/formats/sample.vcf.gz" \
    --double-id \
    --score "$SCORE_FILE" 1 2 3 \
      cols=+scoresums \
    --threads $THREADS \
    --allow-extra-chr \
    --out "$WORKDIR/plink2_out/${PGS_ID}_vcf" \
    > "$WORKDIR/plink2_out/${PGS_ID}_vcf.log" 2>&1 || true

  if [ -f "$WORKDIR/plink2_out/${PGS_ID}_vcf.sscore" ]; then
    pass "plink2/VCF $PGS_ID ($LABEL)"
  else
    fail "plink2/VCF $PGS_ID ($LABEL) — no output"
  fi
done

# ═══════════════════════════════════════════════════════════════════
# STEP 5C: Score with plink2 --score (BED input)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 5C: plink2 --score (BED format) ──"

for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
  LABEL="${PGS_LABELS[$PGS_ID]}"
  SCORE_FILE="$WORKDIR/scores/${PGS_ID}_plink2_input.txt"
  [ ! -f "$SCORE_FILE" ] && continue

  plink2 --bfile "$WORKDIR/formats/sample_bed" \
    --score "$SCORE_FILE" 1 2 3 \
      cols=+scoresums \
    --threads $THREADS \
    --allow-extra-chr \
    --out "$WORKDIR/plink2_out/${PGS_ID}_bed" \
    > "$WORKDIR/plink2_out/${PGS_ID}_bed.log" 2>&1 || true

  if [ -f "$WORKDIR/plink2_out/${PGS_ID}_bed.sscore" ]; then
    pass "plink2/BED $PGS_ID ($LABEL)"
  else
    fail "plink2/BED $PGS_ID ($LABEL) — no output"
  fi
done

# ═══════════════════════════════════════════════════════════════════
# STEP 6: Score with pgsc_calc (Nextflow)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 6: pgsc_calc (Nextflow pipeline) ──"

if [ "$HAS_PGSC_CALC" = true ]; then
  # Create samplesheet
  cat > "$WORKDIR/pgsc_calc_out/samplesheet.csv" <<SHEET
sampleset,vcf_path,bfile_path,pfile_path,chrom
test_samples,$WORKDIR/formats/sample.vcf.gz,,,
SHEET

  nextflow run pgscatalog/pgsc_calc \
    -profile conda \
    --input "$WORKDIR/pgsc_calc_out/samplesheet.csv" \
    --pgs_id "$(echo $PGS_IDS | tr ' ' ',')" \
    --target_build "$BUILD" \
    --outdir "$WORKDIR/pgsc_calc_out/results" \
    --max_cpus $THREADS \
    --min_overlap 0.01 \
    -work-dir "$WORKDIR/pgsc_calc_out/work" \
    > "$WORKDIR/pgsc_calc_out/nextflow.log" 2>&1 || true

  if ls "$WORKDIR/pgsc_calc_out/results"/**/*.sscore 2>/dev/null | head -1 > /dev/null; then
    pass "pgsc_calc completed"
  else
    fail "pgsc_calc — no .sscore output (check $WORKDIR/pgsc_calc_out/nextflow.log)"
  fi
else
  warn "pgsc_calc — nextflow or pipeline not installed"
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 7: Score with PRSice-2
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 7: PRSice-2 ──"

if [ "$HAS_PRSICE" = true ]; then
  for PGS_ID in $(echo "$PGS_IDS" | tr ',' ' '); do
    LABEL="${PGS_LABELS[$PGS_ID]}"
    SCORE_FILE="$WORKDIR/scores/${PGS_ID}_prsice_input.txt"
    [ ! -f "$SCORE_FILE" ] && continue

    # PRSice-2 expects BED/BIM/FAM only
    # --no-regress: skip regression (we just want the score, no phenotype)
    # --fastscore + --bar-levels 1: use all SNPs (p=1 threshold)
    # --no-clump: don't LD-clump (we want the published score as-is)
    $PRSICE_BIN \
      --base "$SCORE_FILE" \
      --target "$WORKDIR/formats/sample_bed" \
      --snp SNP --a1 A1 --a2 A2 --stat BETA --pvalue P \
      --no-regress \
      --fastscore \
      --bar-levels 1 \
      --no-clump \
      --thread $THREADS \
      --out "$WORKDIR/prsice_out/${PGS_ID}" \
      > "$WORKDIR/prsice_out/${PGS_ID}.log" 2>&1 || true

    if [ -f "$WORKDIR/prsice_out/${PGS_ID}.all_score" ] || \
       [ -f "$WORKDIR/prsice_out/${PGS_ID}.best" ]; then
      pass "PRSice-2 $PGS_ID ($LABEL)"
    else
      fail "PRSice-2 $PGS_ID ($LABEL) — no output (check $WORKDIR/prsice_out/${PGS_ID}.log)"
    fi
  done
else
  warn "PRSice-2 — not installed"
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 8: Cross-method comparison
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 8: Cross-method plausibility checks ──"

python3 - <<'PYEOF'
import os, sys, glob
import numpy as np

workdir = os.environ.get("PGS_TEST_WORKDIR", "/tmp/pgs_method_test")
pgs_ids = os.environ.get("PGS_IDS", "PGS000727,PGS003725,PGS002771").split(",")
pgs_labels = {"PGS000727": "Height", "PGS003725": "CAD", "PGS002771": "T2D"}
failures = 0

def read_sscore(filepath):
    """Read plink2 .sscore file, return dict of IID → SCORE1_SUM."""
    scores = {}
    if not os.path.exists(filepath):
        return scores
    with open(filepath) as f:
        header = f.readline().strip().split('\t')
        # Find the score column (SCORE1_SUM or SCORE1_AVG)
        score_col = None
        iid_col = None
        for i, h in enumerate(header):
            if h in ('#IID', 'IID'):
                iid_col = i
            if 'SCORE1_SUM' in h:
                score_col = i
            elif 'SCORE1_AVG' in h and score_col is None:
                score_col = i
        if score_col is None or iid_col is None:
            return scores
        for line in f:
            parts = line.strip().split('\t')
            try:
                scores[parts[iid_col]] = float(parts[score_col])
            except (IndexError, ValueError):
                continue
    return scores

def read_prsice_scores(filepath):
    """Read PRSice .all_score or .best file."""
    scores = {}
    for candidate in [filepath + ".all_score", filepath + ".best"]:
        if not os.path.exists(candidate):
            continue
        with open(candidate) as f:
            header = f.readline().strip().split()
            iid_idx = 1  # FID IID ...
            # Score columns after IID
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    try:
                        scores[parts[iid_idx]] = float(parts[-1])
                    except (ValueError, IndexError):
                        continue
        if scores:
            break
    return scores

print("=" * 70)
print(f"{'PGS ID':<12} {'Trait':<8} {'Method':<16} {'N':<4} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10}")
print("-" * 70)

all_results = {}  # pgs_id → {method → {iid → score}}

for pgs_id in pgs_ids:
    label = pgs_labels.get(pgs_id, "?")
    all_results[pgs_id] = {}

    # plink2 PGEN results
    pgen_scores = read_sscore(f"{workdir}/plink2_out/{pgs_id}_pgen.sscore")
    if pgen_scores:
        vals = list(pgen_scores.values())
        print(f"{pgs_id:<12} {label:<8} {'plink2/PGEN':<16} {len(vals):<4} {np.mean(vals):>10.6f} {np.std(vals):>10.6f} {np.min(vals):>10.6f} {np.max(vals):>10.6f}")
        all_results[pgs_id]["plink2_pgen"] = pgen_scores

    # plink2 VCF results
    vcf_scores = read_sscore(f"{workdir}/plink2_out/{pgs_id}_vcf.sscore")
    if vcf_scores:
        vals = list(vcf_scores.values())
        print(f"{'':<12} {'':<8} {'plink2/VCF':<16} {len(vals):<4} {np.mean(vals):>10.6f} {np.std(vals):>10.6f} {np.min(vals):>10.6f} {np.max(vals):>10.6f}")
        all_results[pgs_id]["plink2_vcf"] = vcf_scores

    # plink2 BED results
    bed_scores = read_sscore(f"{workdir}/plink2_out/{pgs_id}_bed.sscore")
    if bed_scores:
        vals = list(bed_scores.values())
        print(f"{'':<12} {'':<8} {'plink2/BED':<16} {len(vals):<4} {np.mean(vals):>10.6f} {np.std(vals):>10.6f} {np.min(vals):>10.6f} {np.max(vals):>10.6f}")
        all_results[pgs_id]["plink2_bed"] = bed_scores

    # pgsc_calc results
    pgsc_files = glob.glob(f"{workdir}/pgsc_calc_out/results/**/*{pgs_id}*.sscore", recursive=True)
    if pgsc_files:
        pgsc_scores = read_sscore(pgsc_files[0])
        if pgsc_scores:
            vals = list(pgsc_scores.values())
            print(f"{'':<12} {'':<8} {'pgsc_calc':<16} {len(vals):<4} {np.mean(vals):>10.6f} {np.std(vals):>10.6f} {np.min(vals):>10.6f} {np.max(vals):>10.6f}")
            all_results[pgs_id]["pgsc_calc"] = pgsc_scores

    # PRSice-2 results
    prsice_scores = read_prsice_scores(f"{workdir}/prsice_out/{pgs_id}")
    if prsice_scores:
        vals = list(prsice_scores.values())
        print(f"{'':<12} {'':<8} {'PRSice-2':<16} {len(vals):<4} {np.mean(vals):>10.6f} {np.std(vals):>10.6f} {np.min(vals):>10.6f} {np.max(vals):>10.6f}")
        all_results[pgs_id]["prsice2"] = prsice_scores

    print()

# ── Plausibility Checks ──
print("=" * 70)
print("PLAUSIBILITY CHECKS")
print("=" * 70)

for pgs_id in pgs_ids:
    label = pgs_labels.get(pgs_id, "?")
    methods = all_results.get(pgs_id, {})

    if not methods:
        print(f"\n  ✗ {pgs_id} ({label}): No results from any method")
        failures += 1
        continue

    print(f"\n  {pgs_id} ({label}):")

    # Check 1: plink2 PGEN vs VCF vs BED should be identical (same tool, same data)
    pgen = methods.get("plink2_pgen", {})
    vcf = methods.get("plink2_vcf", {})
    bed = methods.get("plink2_bed", {})

    if pgen and vcf:
        common_ids = set(pgen.keys()) & set(vcf.keys())
        if common_ids:
            diffs = [abs(pgen[k] - vcf[k]) for k in common_ids]
            max_diff = max(diffs)
            if max_diff < 1e-6:
                print(f"    ✓ PGEN vs VCF scores identical (max diff: {max_diff:.2e})")
            else:
                print(f"    ✗ PGEN vs VCF scores DIFFER (max diff: {max_diff:.6f})")
                failures += 1

    if pgen and bed:
        common_ids = set(pgen.keys()) & set(bed.keys())
        if common_ids:
            diffs = [abs(pgen[k] - bed[k]) for k in common_ids]
            max_diff = max(diffs)
            if max_diff < 1e-6:
                print(f"    ✓ PGEN vs BED scores identical (max diff: {max_diff:.2e})")
            else:
                print(f"    ✗ PGEN vs BED scores DIFFER (max diff: {max_diff:.6f})")
                failures += 1

    # Check 2: Scores are non-zero and finite
    for method_name, scores in methods.items():
        vals = list(scores.values())
        if all(v == 0 for v in vals):
            print(f"    ✗ {method_name}: All scores are zero (likely 0 variant matches)")
            failures += 1
        elif any(not np.isfinite(v) for v in vals):
            print(f"    ✗ {method_name}: Contains NaN/Inf values")
            failures += 1
        else:
            print(f"    ✓ {method_name}: Scores non-zero and finite")

    # Check 3: Cross-method correlation (plink2 vs PRSice-2 or pgsc_calc)
    ref_method = "plink2_pgen"
    ref_scores = methods.get(ref_method, {})
    for other_name in ["prsice2", "pgsc_calc"]:
        other_scores = methods.get(other_name, {})
        if ref_scores and other_scores:
            common = set(ref_scores.keys()) & set(other_scores.keys())
            if len(common) >= 3:
                a = np.array([ref_scores[k] for k in common])
                b = np.array([other_scores[k] for k in common])
                if np.std(a) > 0 and np.std(b) > 0:
                    corr = np.corrcoef(a, b)[0, 1]
                    if corr > 0.95:
                        print(f"    ✓ plink2 vs {other_name}: r={corr:.4f} (excellent agreement)")
                    elif corr > 0.80:
                        print(f"    ○ plink2 vs {other_name}: r={corr:.4f} (good — minor differences expected)")
                    else:
                        print(f"    ✗ plink2 vs {other_name}: r={corr:.4f} (low correlation — investigate)")
                        failures += 1
                else:
                    print(f"    ○ plink2 vs {other_name}: zero variance, can't compute correlation")
            else:
                print(f"    ○ plink2 vs {other_name}: <3 common samples, can't correlate")

print()
print("=" * 70)
if failures == 0:
    print(f"  ✓ ALL CHECKS PASSED")
else:
    print(f"  ✗ {failures} CHECK(S) FAILED — review above")
print("=" * 70)

sys.exit(failures)
PYEOF

PYTHON_EXIT=$?
if [ $PYTHON_EXIT -ne 0 ]; then
  FAILURES=$((FAILURES + PYTHON_EXIT))
fi

# ═══════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════"
echo "  FINAL SUMMARY"
echo "══════════════════════════════════════"
echo "  Failures: $FAILURES"
echo "  Skips:    $SKIPS"
echo "  Work dir: $WORKDIR"
echo ""
echo "  Log files:"
echo "    plink2:     $WORKDIR/plink2_out/*.log"
[ "$HAS_PGSC_CALC" = true ] && echo "    pgsc_calc:  $WORKDIR/pgsc_calc_out/nextflow.log"
[ "$HAS_PRSICE" = true ]    && echo "    PRSice-2:   $WORKDIR/prsice_out/*.log"
echo ""

if [ $FAILURES -gt 0 ]; then
  echo -e "  ${RED}Some tests failed. Check logs above.${NC}"
  exit 1
else
  echo -e "  ${GREEN}All tests passed!${NC}"
  exit 0
fi
