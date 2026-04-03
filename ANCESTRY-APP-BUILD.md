# Ancestry Pipeline — Standalone App Build Instructions

## What This Builds

A self-contained ancestry inference service that takes WGS input (VCF/gVCF/BAM)
and returns population composition with fine-scale resolution including Middle
Eastern, Jewish pattern detection, and bottleneck analysis.

Stack: Python (FastAPI) + bcftools + plink/plink2 + Rye (R) + React frontend.

---

## 1. Server Requirements

```
CPU:    8+ cores (PCA and mpileup are parallelizable)
RAM:    32 GB minimum (PCA on 4K samples × 200K variants)
Disk:   200 GB (/data partition for reference panels + scratch)
OS:     Ubuntu 22.04 or 24.04
GPU:    Not needed
```

If running on the existing genom-beast server, use `/data/ancestry_app/` as root.
If setting up a new VM, use a GCE n2-standard-8 or n2-standard-16 with a 200 GB
persistent disk mounted at `/data`.

---

## 2. Install Dependencies

```bash
APP_ROOT="/data/ancestry_app"
mkdir -p $APP_ROOT/{reference,tools,scratch,results}

# System packages
sudo apt-get update && sudo apt-get install -y \
  bcftools samtools tabix plink1.9 wget curl git \
  python3-pip python3-venv r-base

# plink2
wget -qO /usr/local/bin/plink2 \
  "https://s3.amazonaws.com/plink2-assets/alpha6/plink2_linux_x86_64_20241222.zip"
# (or get latest from https://www.cog-genomics.org/plink/2.0/)
chmod +x /usr/local/bin/plink2

# Verify
for cmd in bcftools samtools tabix plink plink2; do
  which $cmd && echo "$cmd OK" || echo "$cmd MISSING"
done

# R packages for Rye
R -e 'install.packages(c("nnls","crayon","optparse","Hmisc"), repos="https://cloud.r-project.org")'

# Rye
git clone https://github.com/healthdisparities/rye.git $APP_ROOT/tools/rye
chmod +x $APP_ROOT/tools/rye/rye.R

# Python environment
python3 -m venv $APP_ROOT/venv
source $APP_ROOT/venv/bin/activate
pip install fastapi uvicorn numpy scipy aiofiles
```

---

## 3. Download Reference Panel: gnomAD HGDP+1kGP

This is the primary and only reference panel needed. It contains 4,094 samples
from 80 populations including Druze, Palestinian, Bedouin, Mozabite (Middle Eastern).
WGS-based, GRCh38, individual genotypes, phased with SHAPEIT5.

### 3a. Download BCFs from Google Cloud

```bash
cd $APP_ROOT/reference

# Install gsutil if not present
pip install gsutil 2>/dev/null || \
  curl https://sdk.cloud.google.com | bash -s -- --disable-prompts

# Download phased BCFs (per-chromosome, ~50-100 GB total)
# On GCE this is intra-Google traffic = free and fast
gsutil -m cp "gs://gcp-public-data--gnomad/resources/hgdp_1kg/phased_haplotypes_v2/*.bcf*" .

# Download metadata
gsutil cp gs://gcp-public-data--gnomad/release/3.1/secondary_analyses/hgdp_1kg_v2/metadata_and_qc/gnomad_meta_updated.tsv .

ls *.bcf | wc -l  # should be 22 (one per autosome)
```

### 3b. Convert to PLINK format

```bash
cd $APP_ROOT/reference

# Concatenate all autosomes, filter to biallelic SNPs, set IDs
bcftools concat *.bcf | \
  bcftools view --types snps -m2 -M2 | \
  bcftools annotate --set-id '%CHROM:%POS:%REF:%ALT' \
  -Oz -o hgdp_1kg_all.vcf.gz --threads 8
tabix -p vcf hgdp_1kg_all.vcf.gz

# Convert to PLINK binary
plink2 --vcf hgdp_1kg_all.vcf.gz \
  --maf 0.01 --geno 0.02 --hwe 1e-6 \
  --make-bed --out hgdp_1kg_raw --threads 8

echo "Raw: $(wc -l < hgdp_1kg_raw.bim) variants, $(wc -l < hgdp_1kg_raw.fam) samples"
```

### 3c. LD-prune

```bash
# High-LD regions file (create if not present)
cat > $APP_ROOT/reference/high_ld_regions_hg38.txt << 'EOF'
5 43000000 51500000 r1
6 24000000 34000000 r2
8 7000000 13000000 r3
11 42000000 58000000 r4
EOF

plink2 --bfile hgdp_1kg_raw \
  --exclude range high_ld_regions_hg38.txt \
  --indep-pairwise 1000 50 0.2 \
  --out ld_prune --threads 8

plink2 --bfile hgdp_1kg_raw \
  --extract ld_prune.prune.in \
  --make-bed --out ref_pruned --threads 8

echo "Pruned: $(wc -l < ref_pruned.bim) variants, $(wc -l < ref_pruned.fam) samples"
# Expected: 150K-250K variants, ~4094 samples
```

### 3d. Set population labels in .fam (FID column)

Rye reads population codes from the FID column of the eigenvec file, which
comes from the .fam FID. The gnomAD metadata TSV has per-sample population.

```python
#!/usr/bin/env python3
"""set_fam_labels.py — set FID to population code from gnomAD metadata."""
import csv, sys

meta = {}
with open("gnomad_meta_updated.tsv") as f:
    reader = csv.DictReader(f, delimiter='\t')
    for row in reader:
        sid = row.get("s", row.get("sample", "")).strip()
        # Use the most specific population label available
        pop = row.get("hgdp_tgp_meta.Population", row.get("population", "")).strip()
        if not pop:
            pop = row.get("hgdp_tgp_meta.Genetic.region", "UNK").strip()
        if sid and pop:
            meta[sid] = pop

lines = open("ref_pruned.fam").readlines()
with open("ref_pruned.fam.new", "w") as f:
    mapped = 0
    for line in lines:
        parts = line.strip().split()
        iid = parts[1]
        pop = meta.get(iid, parts[0])
        parts[0] = pop
        f.write("\t".join(parts) + "\n")
        if pop != "0":
            mapped += 1

import shutil
shutil.copy("ref_pruned.fam", "ref_pruned.fam.bak")
shutil.move("ref_pruned.fam.new", "ref_pruned.fam")
print(f"Mapped {mapped}/{len(lines)} samples")

# Print population counts
from collections import Counter
pops = [l.strip().split()[0] for l in open("ref_pruned.fam")]
counts = Counter(pops)
print(f"\n{len(counts)} populations. Top 20:")
for p, c in counts.most_common(20):
    print(f"  {p}: {c}")
```

```bash
cd $APP_ROOT/reference && python3 set_fam_labels.py
```

**Verify** population names match expected HGDP/1KG labels:
```bash
cut -f1 ref_pruned.fam | sort -u | head -30
# Should include: Druze, Palestinian, Bedouin, Mozabite, Han, Japanese, etc.
```

### 3e. Create pop2group mapping

The pop2group file maps each population to a broader ancestry group for Rye.
Adjust population names to match whatever `cut -f1 ref_pruned.fam | sort -u` shows.

```bash
cat > $APP_ROOT/reference/pop2group.txt << 'HEREDOC'
Pop	Group
CEU	European
TSI	European
GBR	European
IBS	European
FIN	Finnish
French	European
Sardinian	European
Tuscan	European
Basque	European
Bergamo	European
BergamoItalian	European
Orcadian	European
Russian	European
Adygei	European
French_Basque	European
CHB	EastAsian
JPT	EastAsian
CHS	EastAsian
CDX	EastAsian
KHV	EastAsian
Han	EastAsian
NorthernHan	EastAsian
Japanese	EastAsian
Dai	EastAsian
She	EastAsian
Tujia	EastAsian
Miao	EastAsian
Naxi	EastAsian
Yi	EastAsian
Tu	EastAsian
Xibo	EastAsian
Mongola	EastAsian
Mongolian	EastAsian
Hezhen	EastAsian
Daur	EastAsian
Oroqen	EastAsian
Cambodian	EastAsian
Lahu	EastAsian
Yakut	EastAsian
YRI	African
LWK	African
GWD	African
MSL	African
ESN	African
ACB	African
ASW	African
Yoruba	African
Mandenka	African
BantuSouthAfrica	African
BantuKenya	African
San	African
Biaka	African
BiakaPygmy	African
Mbuti	African
MbutiPygmy	African
MXL	American
PUR	American
CLM	American
PEL	American
Maya	American
Pima	American
Colombian	American
Karitiana	American
Surui	American
GIH	SouthAsian
PJL	SouthAsian
BEB	SouthAsian
STU	SouthAsian
ITU	SouthAsian
Balochi	SouthAsian
Brahui	SouthAsian
Makrani	SouthAsian
Sindhi	SouthAsian
Pathan	SouthAsian
Burusho	SouthAsian
Hazara	SouthAsian
Uygur	SouthAsian
Kalash	SouthAsian
Druze	MiddleEastern
Palestinian	MiddleEastern
Bedouin	MiddleEastern
BedouinB	MiddleEastern
Mozabite	MiddleEastern
Papuan	Oceanian
PapuanHighlands	Oceanian
PapuanSepik	Oceanian
Bougainville	Oceanian
NAN_Melanesian	Oceanian
HEREDOC
```

**After creating**: verify coverage:
```bash
# Populations in .fam NOT mapped in pop2group (will be excluded from Rye):
comm -23 \
  <(cut -f1 $APP_ROOT/reference/ref_pruned.fam | sort -u) \
  <(tail -n+2 $APP_ROOT/reference/pop2group.txt | cut -f1 | sort -u)
# Add any missing important populations to pop2group.txt
```

### 3f. Verify setup

```bash
echo "=== Reference Panel ==="
echo "Variants: $(wc -l < $APP_ROOT/reference/ref_pruned.bim)"
echo "Samples:  $(wc -l < $APP_ROOT/reference/ref_pruned.fam)"
echo "Populations: $(cut -f1 $APP_ROOT/reference/ref_pruned.fam | sort -u | wc -l)"
echo ""
echo "Middle Eastern samples:"
grep -E "Druze|Palestinian|Bedouin|Mozabite" $APP_ROOT/reference/ref_pruned.fam | \
  cut -f1 | sort | uniq -c | sort -rn
echo ""
echo "Pop2group groups: $(tail -n+2 $APP_ROOT/reference/pop2group.txt | awk -F'\t' '{print $2}' | sort -u | tr '\n' ', ')"
echo ""
echo "=== Tools ==="
for cmd in bcftools plink plink2 tabix R; do which $cmd; done
$APP_ROOT/tools/rye/rye.R -h 2>&1 | head -1
```

---

## 4. Pipeline Logic

The full pipeline is a single Python module. Here is the exact algorithm.

### 4a. Input detection

```python
def detect_input(path: str) -> str:
    p = path.lower()
    if ".g.vcf" in p or "gvcf" in p: return "gvcf"
    if p.endswith((".vcf.gz", ".vcf")): return "vcf"
    if p.endswith(".bam"): return "bam"
    if p.endswith(".cram"): return "cram"
    raise ValueError(f"Unknown: {path}")
```

### 4b. Variant extraction

**For VCF/gVCF:**
```bash
bcftools norm -m-any "$INPUT" | \
  bcftools view --types snps -m2 -M2 | \
  bcftools annotate --set-id '%CHROM:%POS:%REF:%ALT' \
  -Oz -o "$TMP/norm.vcf.gz"
tabix -p vcf "$TMP/norm.vcf.gz"
plink2 --vcf "$TMP/norm.vcf.gz" --chr 1-22 --allow-extra-chr --make-bed --out "$TMP/sample"
```

**For BAM/CRAM:**
```bash
# Create targets from ref panel
awk -v OFS='\t' '{print $1, $4}' "$REF.bim" > "$TMP/targets.tsv"

bcftools mpileup -f "$FASTA" -T "$TMP/targets.tsv" \
  --min-MQ 20 --min-BQ 20 --max-depth 500 --threads 8 "$BAM" | \
bcftools call -m --ploidy GRCh38 --threads 4 | \
bcftools view --types snps -m2 -M2 | \
bcftools annotate --set-id '%CHROM:%POS:%REF:%ALT' | \
bcftools sort -Oz -o "$TMP/called.vcf.gz"
tabix -p vcf "$TMP/called.vcf.gz"
plink2 --vcf "$TMP/called.vcf.gz" --chr 1-22 --allow-extra-chr --make-bed --out "$TMP/sample"
```

**Chromosome naming**: Check sample vs reference. If sample uses `chr1` and ref
uses `1` (or vice versa), rename before intersection:
```bash
# Detect
SAMPLE_CHR=$(head -1 "$TMP/sample.bim" | cut -f1)
REF_CHR=$(head -1 "$REF.bim" | cut -f1)
# If mismatch, rename sample .bim: sed -i 's/^chr//' or sed -i 's/^\([0-9]\)/chr\1/'
```

### 4c. Intersect + align

```bash
comm -12 \
  <(awk '{print $2}' "$TMP/sample.bim" | sort) \
  <(awk '{print $2}' "$REF.bim" | sort) \
  > "$TMP/overlap.txt"

N=$(wc -l < "$TMP/overlap.txt")
[ "$N" -lt 50000 ] && echo "FAIL: only $N overlap" && exit 1

plink2 --bfile "$TMP/sample" --extract "$TMP/overlap.txt" \
  --ref-allele force "$REF.bim" 5 2 \
  --make-bed --out "$TMP/sample_aligned"
```

Expected overlap: 80-130K variants for a 30× WGS sample.

### 4d. Merge + PCA

```bash
# Subset ref to overlapping variants
awk '{print $2}' "$TMP/sample_aligned.bim" > "$TMP/ov_ids.txt"
plink2 --bfile "$REF" --extract "$TMP/ov_ids.txt" --make-bed --out "$TMP/ref_ov"

# Merge with plink 1.9
plink --bfile "$TMP/ref_ov" --bmerge "$TMP/sample_aligned" \
  --make-bed --out "$TMP/merged" --allow-no-sex

# Handle strand errors
if [ -f "$TMP/merged-merge.missnp" ]; then
  plink2 --bfile "$TMP/sample_aligned" --exclude "$TMP/merged-merge.missnp" \
    --make-bed --out "$TMP/sample_clean"
  plink --bfile "$TMP/ref_ov" --bmerge "$TMP/sample_clean" \
    --make-bed --out "$TMP/merged" --allow-no-sex
fi

# QC before PCA
plink2 --bfile "$TMP/merged" --mind 0.1 --geno 0.1 --maf 0.01 \
  --make-bed --out "$TMP/merged_clean"

# PCA
plink2 --bfile "$TMP/merged_clean" --pca 20 --out "$TMP/pca"
```

### 4e. Rye ancestry estimation

```bash
$APP_ROOT/tools/rye/rye.R \
  --eigenvec="$TMP/pca.eigenvec" \
  --eigenval="$TMP/pca.eigenval" \
  --pop2group="$APP_ROOT/reference/pop2group.txt" \
  --rounds=50 --iter=50 --threads=$(nproc) --pcs=10 \
  --out="$TMP/rye_result"
```

**Parse**: the .Q file has one row per sample (same order as eigenvec). The query
sample is the **last row** (appended during merge). Columns correspond to groups
in pop2group, ordered by first appearance.

### 4f. ROH (VCF/gVCF only — skip for BAM/CRAM)

```bash
# Use FULL sample .bed (pre-intersection, dense variants)
plink --bfile "$TMP/sample" --homozyg \
  --homozyg-window-snp 50 --homozyg-snp 50 --homozyg-kb 300 \
  --homozyg-density 50 --homozyg-gap 1000 \
  --out "$TMP/roh"
```

**Why skip for BAM**: mpileup only emits variant sites → sparse, het-biased
calls → ROH detection produces false negatives.

### 4g. Interpretation

```python
def interpret(proportions, roh, panel_name):
    flags = []
    eur = proportions.get("European", 0) + proportions.get("Finnish", 0)
    mid = proportions.get("MiddleEastern", 0)

    # ASJ detection: EUR 40-60% + MID 30-50% + bottleneck ROH
    if 0.30 < eur < 0.70 and mid > 0.25:
        msg = f"EUR+MID pattern ({eur:.0%} + {mid:.0%}). Characteristic of Jewish ancestry."
        if roh and roh.get("bottleneck"):
            msg += f" ROH bottleneck ({roh['total_mb']:.0f} Mb) confirms Ashkenazi Jewish."
        flags.append(msg)

    # Half-EAS/half-ASJ: EAS ~45-50% + EUR ~25% + MID ~20%
    eas = proportions.get("EastAsian", 0)
    if eas > 0.30 and eur > 0.15 and mid > 0.10:
        flags.append(f"Mixed EAS+EUR+MID pattern. Consistent with half East Asian, half Jewish.")

    return flags
```

---

## 5. Critical Rules (Encoded from Debugging)

These are hard-won lessons. Violating any of them produces wrong results.

| # | Rule | Why |
|---|------|-----|
| 1 | **Only WGS-based reference panels** | Array panels (Human Origins, Illumina) have ascertainment bias. WGS VCFs only contain variant sites — filling ref/ref at array positions makes samples look African. Tested 6 approaches, all failed. |
| 2 | **Joint PCA, not FRAPOSA** | FRAPOSA caches `.dat` files keyed on variant ID format. If sample IDs use `CHR:POS:REF:ALT` but cache uses `CHR:POS:col5:col6`, zero variants match. Joint PCA always works. |
| 3 | **plink --bmerge, not bcftools merge** | bcftools merge requires VCF roundtrip (slow for 4K samples). plink --bmerge operates on BED files directly. |
| 4 | **--ref-allele force before merge** | Align sample alleles to reference panel allele encoding. Without this, strand mismatches corrupt PCA. |
| 5 | **--mind 0.1 --geno 0.1 before PCA** | Some samples in the panel have high missingness. PCA produces NaN without this filter. |
| 6 | **ROH on FULL sample, not intersected subset** | ROH needs dense markers. The 100K reference-intersected subset is too sparse. Use the pre-intersection sample.bed. |
| 7 | **Skip ROH for BAM input** | mpileup genotypes are het-biased at scattered positions. ROH detection fails. |
| 8 | **Rye reads FID from eigenvec** | The .fam FID propagates to eigenvec column 1. If FID is "0" for all samples, Rye can't find any population matches. |
| 9 | **pop2group must match .fam FID exactly** | Case-sensitive. "Druze" ≠ "druze". Check with `comm -23`. |
| 10 | **Filter unmapped populations from NNLS/KNN** | If pop2group maps 70 of 80 populations, the 10 unmapped ones leak into KNN as individual "groups", producing noisy results. Only use mapped populations. |

---

## 6. API Service

```python
# ancestry_app/main.py
from fastapi import FastAPI, UploadFile, BackgroundTasks
from pydantic import BaseModel
import subprocess, tempfile, json, os, shutil

app = FastAPI(title="Ancestry Pipeline")

APP_ROOT = os.environ.get("APP_ROOT", "/data/ancestry_app")
REF = f"{APP_ROOT}/reference/ref_pruned"
POP2GROUP = f"{APP_ROOT}/reference/pop2group.txt"
RYE = f"{APP_ROOT}/tools/rye/rye.R"
SCRATCH = f"{APP_ROOT}/scratch"

class AncestryResult(BaseModel):
    sample_name: str
    proportions: dict[str, float]
    primary: str
    primary_pct: float
    is_admixed: bool
    flags: list[str]
    roh: dict | None
    variants_used: int
    panel: str

@app.post("/analyze")
async def analyze(
    sample_name: str,
    vcf: UploadFile | None = None,
    bam_path: str | None = None,  # server-local path
    fasta_path: str | None = None,
):
    """Run full ancestry pipeline. Returns AncestryResult."""
    tmpdir = tempfile.mkdtemp(dir=SCRATCH, prefix=f"anc_{sample_name}_")
    try:
        # Save uploaded VCF or use BAM path
        if vcf:
            vcf_path = os.path.join(tmpdir, vcf.filename)
            with open(vcf_path, "wb") as f:
                shutil.copyfileobj(vcf.file, f)
            input_path = vcf_path
            input_type = "vcf"
        elif bam_path:
            input_path = bam_path
            input_type = "bam"
        else:
            return {"error": "Provide vcf upload or bam_path"}

        result = run_pipeline(sample_name, input_path, input_type, tmpdir, fasta_path)
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def run_pipeline(sample_name, input_path, input_type, tmpdir, fasta_path=None):
    """Core pipeline — see section 4 for full algorithm."""
    # ... implement steps 4b through 4g here ...
    pass
```

### Run the service

```bash
source $APP_ROOT/venv/bin/activate
cd $APP_ROOT
uvicorn ancestry_app.main:app --host 0.0.0.0 --port 8700 --workers 1
# Single worker because plink/bcftools use all cores internally
```

---

## 7. Frontend (Optional)

A minimal React frontend with:
- File upload (VCF/gVCF) or path input (BAM)
- Sample name field
- Submit button → shows progress
- Results: composition bar chart, flags, ROH summary
- Download JSON/PDF report

Use the existing genomics platform's React framework if integrating into the
genom-beast stack. Otherwise, a standalone Vite + React app on port 3000.

---

## 8. Validation Checklist

After setup, run these samples and verify results match expectations:

| Sample | Expected (HGDP+1kGP panel) | Pass if |
|--------|---------------------------|---------|
| Any 1KG CEU | EUR >90% | No MID, no AFR ghost |
| Any 1KG YRI | AFR >90% | Clean single-component |
| Any 1KG JPT | EAS >90% | No SAS contamination |
| Nimo (ASJ) | EUR 45-55%, MID 35-45% | No AFR >5%, no SAS >10% |
| B2XH (½ EAS + ½ ASJ) | EAS 40-50%, EUR 20-30%, MID 15-25% | No SAS >10% |

If Nimo still shows AFR >15% or SAS >10%, the pop2group mapping is wrong or
the Middle Eastern populations aren't in the panel. Debug by checking:
```bash
grep -i "druze\|palest\|bedouin\|mozab" $APP_ROOT/reference/ref_pruned.fam | wc -l
# Must be >0. If 0, the metadata mapping failed.
```

---

## 9. File Manifest

After complete setup, these files must exist:

```
$APP_ROOT/
├── reference/
│   ├── ref_pruned.bed          # PLINK binary genotypes
│   ├── ref_pruned.bim          # variant info (150-250K variants)
│   ├── ref_pruned.fam          # sample info (FID = population code)
│   ├── pop2group.txt           # population → group mapping
│   ├── gnomad_meta_updated.tsv # sample metadata
│   └── high_ld_regions_hg38.txt
├── tools/
│   └── rye/
│       └── rye.R               # Rye script
├── scratch/                    # temp directories (auto-cleaned)
├── results/                    # saved reports
├── venv/                       # Python virtualenv
└── ancestry_app/
    └── main.py                 # FastAPI service
```
