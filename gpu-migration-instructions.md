# genom-beast GPU Migration & Pipeline Optimization

## Agent Instructions

You are migrating a family genomics platform from a CPU-only GCE instance (`c3-standard-44-lssd`) to a GPU-enabled instance, and rewriting the PGS scoring pipeline to use plink2-native scoring instead of custom Python iteration. The goals are:

1. Replace the GCE machine with a GPU-enabled instance for fast DeepVariant
2. Replace custom Python PGS scoring with plink2-native scoring (100x+ speedup)
3. Precompute and cache 1000 Genomes reference panel distributions
4. Update the web application to work with the new pipeline

The codebase is at `/home/nimo/genomics/` on the server. Backend is FastAPI (Python 3.13), frontend is React+Vite. All bioinformatics tools are in conda env `genomics`.

---

## Phase 0: GCE Instance Migration

### 0.1 Snapshot current disk

```bash
# On local machine or Cloud Shell
gcloud compute disks snapshot genom-beast \
  --project=nimo-gpt \
  --zone=us-central1-c \
  --snapshot-names=genom-beast-pre-gpu-$(date +%Y%m%d)
```

### 0.2 Create new GPU-enabled instance

Cannot add a GPU to c3-series. Create a new n1-standard instance with a T4 GPU.

```bash
gcloud compute instances create genom-beast-gpu \
  --project=nimo-gpt \
  --zone=us-central1-c \
  --machine-type=n1-standard-32 \
  --accelerator=type=nvidia-tesla-t4,count=1 \
  --maintenance-policy=TERMINATE \
  --boot-disk-size=3000GB \
  --boot-disk-type=pd-ssd \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --local-ssd=interface=NVME \
  --local-ssd=interface=NVME \
  --local-ssd=interface=NVME \
  --local-ssd=interface=NVME \
  --metadata=startup-script='#!/bin/bash
    # RAID the local SSDs
    mdadm --create /dev/md0 --level=0 --raid-devices=4 /dev/nvme0n1 /dev/nvme0n2 /dev/nvme0n3 /dev/nvme0n4
    mkfs.ext4 -F /dev/md0
    mkdir -p /scratch
    mount /dev/md0 /scratch
    chmod 1777 /scratch'
```

Machine rationale:
- n1-standard-32: 32 vCPUs, 120 GB RAM. Enough for DeepVariant (GPU mode uses less RAM than CPU-44-shard mode) and parallel plink2 scoring. Cheaper than the c3-standard-44.
- 1x T4: Best price/performance for DeepVariant inference. $0.35/hr on preemptible, ~$1.40/hr on-demand.
- 4x local NVMe SSDs: ~1.5 TB scratch RAID for fast I/O during variant calling.
- 3 TB boot SSD: matches current persistent storage.

### 0.3 Install NVIDIA drivers and CUDA

```bash
# Install NVIDIA driver
sudo apt-get update
sudo apt-get install -y linux-headers-$(uname -r) build-essential
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb -o cuda-keyring.deb
sudo dpkg -i cuda-keyring.deb
sudo apt-get update
sudo apt-get install -y cuda-drivers nvidia-container-toolkit

# Verify
nvidia-smi
```

### 0.4 Install nvidia-container-toolkit for Apptainer/Singularity

```bash
# Apptainer GPU support
sudo apt-get install -y apptainer
# Verify GPU passthrough works
apptainer exec --nv docker://nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

### 0.5 Transfer data from old instance

```bash
# From old instance, rsync to new
gcloud compute ssh genom-beast-gpu --zone=us-central1-c --command="mkdir -p /data /scratch"

# Transfer persistent data
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/data/aligned_bams/ genom-beast-gpu:/data/aligned_bams/
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/data/pgs2/ genom-beast-gpu:/data/pgs2/
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/data/pgs_cache/ genom-beast-gpu:/data/pgs_cache/
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/data/genom-nimo/reference.fasta* genom-beast-gpu:/data/genom-nimo/
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/home/nimo/genomics/ genom-beast-gpu:/home/nimo/genomics/

# Transfer scratch outputs if needed
gcloud compute scp --recurse --zone=us-central1-c \
  genom-beast:/scratch/nimog_output/ genom-beast-gpu:/scratch/nimog_output/
```

### 0.6 Recreate conda environment on new instance

```bash
# Install miniconda
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda3
eval "$($HOME/miniconda3/bin/conda shell.bash hook)"

# Recreate genomics env
conda create -n genomics python=3.13 -y
conda activate genomics
conda install -c bioconda -c conda-forge \
  bcftools=1.22 samtools=1.22 plink2 bwa minimap2 -y
pip install fastapi uvicorn sqlalchemy redis pysam aiofiles websockets
```

### 0.7 Swap external IP

```bash
# Move the static IP from old to new instance
gcloud compute instances delete-access-config genom-beast \
  --zone=us-central1-c --access-config-name="external-nat"
gcloud compute instances add-access-config genom-beast-gpu \
  --zone=us-central1-c --address=34.63.131.11
```

---

## Phase 1: DeepVariant GPU Mode

### 1.1 Pull GPU-enabled DeepVariant container

```bash
# Pull the GPU version (not the CPU version you currently use)
apptainer pull docker://google/deepvariant:1.6.1-gpu
# This creates deepvariant_1.6.1-gpu.sif
mv deepvariant_1.6.1-gpu.sif /data/containers/
```

### 1.2 Update the nimog pipeline to use GPU mode

The nimog pipeline (port 8502) currently runs DeepVariant in CPU mode with 44 shards. Update it to use GPU mode.

Find the DeepVariant execution command in the nimog codebase (likely in one of the 2 files, ~1,439 lines total at `/home/nimo/genomics/nimog/`). The current command likely looks like:

```bash
apptainer run \
  -B /data:/data -B /scratch:/scratch \
  /data/containers/deepvariant_1.6.1.sif \
  /opt/deepvariant/bin/run_deepvariant \
  --model_type=WGS \
  --ref=/data/genom-nimo/reference.fasta \
  --reads=/data/aligned_bams/SAMPLE.bam \
  --output_vcf=/scratch/nimog_output/SAMPLE.vcf.gz \
  --output_gvcf=/scratch/nimog_output/SAMPLE.g.vcf.gz \
  --num_shards=44
```

Replace with:

```bash
apptainer run --nv \
  -B /data:/data -B /scratch:/scratch \
  /data/containers/deepvariant_1.6.1-gpu.sif \
  /opt/deepvariant/bin/run_deepvariant \
  --model_type=WGS \
  --ref=/data/genom-nimo/reference.fasta \
  --reads=/data/aligned_bams/SAMPLE.bam \
  --output_vcf=/scratch/nimog_output/SAMPLE.vcf.gz \
  --output_gvcf=/scratch/nimog_output/SAMPLE.g.vcf.gz \
  --num_shards=16
```

Key changes:
- `--nv` flag: passes GPU through to the container
- GPU container: `deepvariant_1.6.1-gpu.sif`
- `--num_shards=16`: fewer CPU shards needed since `call_variants` runs on GPU. The `make_examples` step still uses CPU shards but 16 is sufficient to keep the GPU fed. With 32 vCPUs, 16 shards leaves headroom for other processes.

### 1.3 Expected DeepVariant performance with GPU

| Sample | BAM Size | CPU (44 shards) | GPU (T4, 16 shards) |
|--------|----------|-----------------|---------------------|
| Nimo | 93 GB | ~2.5 hr | ~40 min |
| Others (~30x) | 52-58 GB | ~1.5-2 hr | ~25-30 min |
| All 6 sequential | - | ~12-14 hr | ~2.5-3.5 hr |

### 1.4 Update resource estimation in the web UI

In `api/runs.py` (the run estimation endpoint), update time estimates for DeepVariant to reflect GPU speedup. Look for any hardcoded time-per-GB or time-per-shard constants and adjust by ~4x.

---

## Phase 2: plink2-Native PGS Scoring (Critical Optimization)

This is the highest-impact change. Current throughput: ~600 variants/sec in Python. Target: ~100,000+ variants/sec via plink2.

### 2.1 Create a gVCF-to-plink2 conversion utility

Create a new file: `scoring/plink2_convert.py`

```python
"""Convert DeepVariant gVCF to plink2 binary format for fast PGS scoring."""

import subprocess
import os
import logging

logger = logging.getLogger(__name__)


def gvcf_to_pgen(gvcf_path: str, output_prefix: str, ref_fasta: str = None) -> dict:
    """
    Convert a gVCF file to plink2 pgen/pvar/psam format.
    
    This is the key step that enables 100x+ faster PGS scoring.
    plink2 can score a 1M-variant PGS against a pgen file in ~5 seconds,
    vs ~30 minutes in the current Python engine.
    
    Args:
        gvcf_path: Path to input .g.vcf.gz file
        output_prefix: Output path prefix (will create .pgen, .pvar.zst, .psam)
        ref_fasta: Optional reference FASTA for resolving REF alleles in blocks
        
    Returns:
        dict with paths to output files and variant count
    """
    output_dir = os.path.dirname(output_prefix)
    os.makedirs(output_dir, exist_ok=True)
    
    # Step 1: Normalize the gVCF - expand block records into individual sites
    # and ensure consistent REF/ALT representation
    normalized_vcf = f"{output_prefix}.norm.vcf.gz"
    
    norm_cmd = [
        "bcftools", "view",
        "--exclude", 'ALT="<NON_REF>" | ALT="<*>"',  # Remove gVCF block records
        "-Oz", "-o", normalized_vcf,
        gvcf_path
    ]
    logger.info(f"Normalizing gVCF: {' '.join(norm_cmd)}")
    subprocess.run(norm_cmd, check=True, capture_output=True, text=True)
    
    # Index the normalized VCF
    subprocess.run(["bcftools", "index", "-t", normalized_vcf], check=True)
    
    # Step 2: Convert to plink2 format
    plink_cmd = [
        "plink2",
        "--vcf", normalized_vcf,
        "--make-pgen", "vzs",
        "--out", output_prefix,
        "--vcf-half-call", "m",  # Treat half-calls as missing
        "--allow-extra-chr",
        "--set-all-var-ids", "@:#:\\$r:\\$a",  # chr:pos:ref:alt format
        "--new-id-max-allele-len", "1000",
    ]
    logger.info(f"Converting to pgen: {' '.join(plink_cmd)}")
    result = subprocess.run(plink_cmd, check=True, capture_output=True, text=True)
    
    # Count variants
    var_count = 0
    pvar_path = f"{output_prefix}.pvar.zst"
    if not os.path.exists(pvar_path):
        pvar_path = f"{output_prefix}.pvar"
    
    count_cmd = ["plink2", "--pfile", output_prefix, "--write-snplist", "--out", f"{output_prefix}.tmp"]
    subprocess.run(count_cmd, check=True, capture_output=True, text=True)
    snplist = f"{output_prefix}.tmp.snplist"
    if os.path.exists(snplist):
        with open(snplist) as f:
            var_count = sum(1 for _ in f)
        os.remove(snplist)
    for tmp in [f"{output_prefix}.tmp.log"]:
        if os.path.exists(tmp):
            os.remove(tmp)
    
    # Clean up intermediate normalized VCF
    os.remove(normalized_vcf)
    if os.path.exists(normalized_vcf + ".tbi"):
        os.remove(normalized_vcf + ".tbi")
    
    return {
        "pgen": f"{output_prefix}.pgen",
        "pvar": pvar_path,
        "psam": f"{output_prefix}.psam",
        "variant_count": var_count,
    }


def check_pgen_exists(output_prefix: str) -> bool:
    """Check if pgen conversion has already been done for this sample."""
    return (
        os.path.exists(f"{output_prefix}.pgen") and
        (os.path.exists(f"{output_prefix}.pvar.zst") or os.path.exists(f"{output_prefix}.pvar")) and
        os.path.exists(f"{output_prefix}.psam")
    )
```

### 2.2 Create the plink2-native scoring engine

Create a new file: `scoring/plink2_scorer.py`

```python
"""
PGS scoring via plink2 native --score command.

This replaces the variant-by-variant Python iteration in engine.py.
Performance: ~5 seconds per 1M-variant PGS vs ~30 minutes in Python.
"""

import subprocess
import os
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Directory for precomputed reference panel stats
REF_PANEL_CACHE_DIR = "/data/pgs2/ref_panel_stats"

# Reference panel plink2 files
REF_PANEL_PREFIX = {
    "GRCh38": "/data/pgs2/ref_panel/1000G_GRCh38",
    "GRCh37": "/data/pgs2/ref_panel/1000G_GRCh37",
}

# Population sample lists (create these once - see Phase 3)
POP_SAMPLE_DIR = "/data/pgs2/ref_panel/pop_samples"


def prepare_plink2_scoring_file(pgs_harmonized_path: str, output_path: str) -> dict:
    """
    Convert a PGS Catalog harmonized scoring file to plink2 --score format.
    
    PGS Catalog format: chr_name, chr_position, effect_allele, other_allele, effect_weight, ...
    plink2 --score format: variant_id, allele, weight
    
    We use chr:pos:ref:alt as variant ID to match the --set-all-var-ids format
    used during gVCF-to-pgen conversion.
    
    Returns dict with metadata (variant count, build, trait, etc.)
    """
    import gzip
    
    metadata = {}
    header_lines = []
    data_lines = []
    
    opener = gzip.open if pgs_harmonized_path.endswith('.gz') else open
    with opener(pgs_harmonized_path, 'rt') as f:
        for line in f:
            if line.startswith('#'):
                header_lines.append(line.strip())
                # Parse metadata from header
                if '=' in line:
                    key, _, val = line.lstrip('#').strip().partition('=')
                    metadata[key.strip()] = val.strip()
                continue
            
            parts = line.strip().split('\t')
            if parts[0] == 'chr_name':
                # This is the column header row
                col_names = parts
                continue
            
            # Build the scoring line
            # Find column indices
            try:
                chr_idx = col_names.index('chr_name')
                pos_idx = col_names.index('chr_position')
                ea_idx = col_names.index('effect_allele')
                weight_idx = col_names.index('effect_weight')
            except ValueError:
                # Try hm_ prefixed columns (harmonized)
                chr_idx = col_names.index('hm_chr')
                pos_idx = col_names.index('hm_pos')
                ea_idx = col_names.index('hm_inferOtherAllele') if 'hm_inferOtherAllele' in col_names else col_names.index('other_allele')
                weight_idx = col_names.index('effect_weight')
            
            chrom = parts[chr_idx]
            pos = parts[pos_idx]
            ea = parts[ea_idx]
            weight = parts[weight_idx]
            
            if not chrom or not pos or chrom == 'NA' or pos == 'NA':
                continue
            
            # Normalize chromosome naming
            if not chrom.startswith('chr'):
                chrom = f"chr{chrom}"
            
            # We need to try both orientations for variant ID matching.
            # The pgen was built with ID format chr:pos:ref:alt.
            # The PGS file gives us effect_allele. We don't always know
            # which is ref vs alt, so we use the allele code column approach.
            # plink2 --score with 'cols=+scoresums' handles allele matching.
            
            # Use a position-based ID that plink2 can match
            var_id = f"{chrom}:{pos}"
            data_lines.append(f"{var_id}\t{ea}\t{weight}")
    
    # Write plink2-compatible scoring file
    with open(output_path, 'w') as f:
        f.write("ID\tA1\tWEIGHT\n")
        for line in data_lines:
            f.write(line + "\n")
    
    metadata['variant_count'] = len(data_lines)
    return metadata


def score_sample_plink2(
    sample_pfile_prefix: str,
    scoring_file_path: str,
    output_prefix: str,
    pgs_id: str,
) -> dict:
    """
    Score a single sample against a single PGS using plink2 --score.
    
    This runs in ~3-10 seconds for a 1M-variant PGS. Compare to the current
    Python engine at ~30 minutes for the same task.
    
    Args:
        sample_pfile_prefix: Path prefix to sample's .pgen/.pvar/.psam files
        scoring_file_path: Path to plink2-format scoring file (from prepare_plink2_scoring_file)
        output_prefix: Output path prefix for results
        pgs_id: PGS identifier for logging
        
    Returns:
        dict with raw_score, matched_variants, total_variants
    """
    os.makedirs(os.path.dirname(output_prefix), exist_ok=True)
    
    cmd = [
        "plink2",
        "--pfile", sample_pfile_prefix,
        "--score", scoring_file_path,
        "header-read",          # First line is header
        "1",                    # Variant ID column
        "2",                    # Allele column  
        "3",                    # Score column
        "cols=+scoresums",      # Output sum of scores
        "--score-col-nums", "3",
        "--allow-extra-chr",
        "--out", output_prefix,
    ]
    
    logger.info(f"Scoring {pgs_id}: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        logger.error(f"plink2 scoring failed for {pgs_id}: {result.stderr}")
        raise RuntimeError(f"plink2 scoring failed: {result.stderr}")
    
    # Parse the .sscore output file
    sscore_path = f"{output_prefix}.sscore"
    score_data = parse_sscore(sscore_path, result.stderr)
    
    # Parse match stats from log
    matched = 0
    total = 0
    for line in result.stderr.split('\n') + result.stdout.split('\n'):
        if 'score loaded' in line.lower() or 'variants processed' in line.lower():
            # Extract counts from plink2 log output
            import re
            numbers = re.findall(r'(\d+)', line)
            if numbers:
                if 'loaded' in line.lower():
                    total = int(numbers[0])
        if '--score:' in line and 'valid predictor' in line.lower():
            numbers = re.findall(r'(\d+)', line)
            if numbers:
                matched = int(numbers[0])
    
    score_data['pgs_id'] = pgs_id
    score_data['matched_variants'] = matched
    score_data['total_variants'] = total
    score_data['match_rate'] = matched / total if total > 0 else 0
    
    return score_data


def parse_sscore(sscore_path: str, log_output: str = "") -> dict:
    """Parse plink2 .sscore output file."""
    if not os.path.exists(sscore_path):
        raise FileNotFoundError(f"Score file not found: {sscore_path}")
    
    with open(sscore_path) as f:
        header = f.readline().strip().split('\t')
        values = f.readline().strip().split('\t')
    
    data = dict(zip(header, values))
    
    # The score column name varies. Look for SCORE or SCORESUM columns.
    raw_score = None
    for key in data:
        if 'SCORE' in key.upper() and 'SUM' in key.upper():
            raw_score = float(data[key])
            break
        elif 'SCORE' in key.upper():
            raw_score = float(data[key])
    
    return {
        'raw_score': raw_score,
        'sample_id': data.get('#IID', data.get('IID', 'unknown')),
        'allele_count': int(data.get('ALLELE_CT', 0)),
        'missing_count': int(data.get('MISSING_CT', 0)),
    }


def get_ref_panel_stats(
    pgs_id: str,
    scoring_file_path: str,
    population: str,
    genome_build: str = "GRCh38",
) -> dict:
    """
    Get precomputed reference panel statistics (mean, std) for a PGS.
    
    If not cached, compute them by scoring the 1000G reference panel
    and save for reuse.
    
    Args:
        pgs_id: PGS identifier
        scoring_file_path: Path to plink2-format scoring file
        population: Population code (EUR, EAS, AFR, SAS, AMR, ALL)
        genome_build: GRCh37 or GRCh38
        
    Returns:
        dict with mean, std, n_samples, population
    """
    cache_key = f"{pgs_id}_{population}_{genome_build}"
    cache_path = os.path.join(REF_PANEL_CACHE_DIR, f"{cache_key}.json")
    
    # Check cache
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            stats = json.load(f)
        logger.info(f"Loaded cached ref panel stats for {cache_key}")
        return stats
    
    # Compute: score the entire 1000G panel
    logger.info(f"Computing ref panel stats for {cache_key} (will cache for future use)")
    
    ref_prefix = REF_PANEL_PREFIX[genome_build]
    pop_keep_file = os.path.join(POP_SAMPLE_DIR, f"{population}.txt") if population != "ALL" else None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        out_prefix = os.path.join(tmpdir, "ref_score")
        
        cmd = [
            "plink2",
            "--pfile", ref_prefix,
            "--score", scoring_file_path,
            "header-read", "1", "2", "3",
            "cols=+scoresums",
            "--score-col-nums", "3",
            "--allow-extra-chr",
            "--out", out_prefix,
        ]
        
        if pop_keep_file and os.path.exists(pop_keep_file):
            cmd.extend(["--keep", pop_keep_file])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Ref panel scoring failed: {result.stderr}")
        
        # Parse all scores from .sscore
        sscore_path = f"{out_prefix}.sscore"
        scores = []
        with open(sscore_path) as f:
            header = f.readline().strip().split('\t')
            score_col = None
            for i, h in enumerate(header):
                if 'SCORE' in h.upper():
                    score_col = i
                    # Prefer SCORESUM over SCORE_AVG
                    if 'SUM' in h.upper():
                        break
            
            for line in f:
                vals = line.strip().split('\t')
                if score_col is not None:
                    try:
                        scores.append(float(vals[score_col]))
                    except (ValueError, IndexError):
                        continue
    
    import numpy as np
    scores_arr = np.array(scores)
    
    stats = {
        "pgs_id": pgs_id,
        "population": population,
        "genome_build": genome_build,
        "mean": float(np.mean(scores_arr)),
        "std": float(np.std(scores_arr)),
        "median": float(np.median(scores_arr)),
        "n_samples": len(scores),
        "min": float(np.min(scores_arr)),
        "max": float(np.max(scores_arr)),
    }
    
    # Cache to disk
    os.makedirs(REF_PANEL_CACHE_DIR, exist_ok=True)
    with open(cache_path, 'w') as f:
        json.dump(stats, f, indent=2)
    
    logger.info(f"Cached ref panel stats: {cache_key} (n={len(scores)}, mean={stats['mean']:.6f}, std={stats['std']:.6f})")
    return stats


def compute_percentile(raw_score: float, ref_stats: dict) -> dict:
    """
    Compute Z-score and percentile from raw score and reference stats.
    Same method as the current engine but using precomputed stats.
    """
    from scipy import stats as scipy_stats
    
    mean = ref_stats['mean']
    std = ref_stats['std']
    
    if std == 0 or std < 1e-12:
        return {
            'z_score': 0.0,
            'percentile': 50.0,
            'raw_score': raw_score,
            'ref_mean': mean,
            'ref_std': std,
        }
    
    z_score = (raw_score - mean) / std
    percentile = float(scipy_stats.norm.cdf(z_score) * 100)
    
    return {
        'z_score': round(z_score, 4),
        'percentile': round(percentile, 2),
        'raw_score': raw_score,
        'ref_mean': mean,
        'ref_std': std,
        'ref_n_samples': ref_stats['n_samples'],
        'ref_population': ref_stats['population'],
    }
```

### 2.3 Create the orchestrator that ties it all together

Create a new file: `scoring/fast_pipeline.py`

```python
"""
Fast PGS scoring pipeline using plink2-native operations.

Replaces the variant-by-variant Python scoring in engine.py for gVCF inputs.
The BAM-direct pipeline (pipeline_e_plus.py) is preserved as a fallback
for scoring without variant calling.

Typical performance:
  - gVCF to pgen conversion: ~60 seconds per sample (one-time)
  - PGS scoring: ~3-10 seconds per PGS per sample
  - Reference panel stats: ~30 seconds per PGS (cached after first run)
  - Total for 6 samples x 49 PGS: ~25 minutes (vs ~12 hours in Python engine)
"""

import asyncio
import os
import time
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from .plink2_convert import gvcf_to_pgen, check_pgen_exists
from .plink2_scorer import (
    prepare_plink2_scoring_file,
    score_sample_plink2,
    get_ref_panel_stats,
    compute_percentile,
)

logger = logging.getLogger(__name__)

# Where converted pgen files live
PGEN_DIR = "/data/pgen_cache"

# Where plink2-format scoring files live
SCORING_FILE_DIR = "/data/pgs2/plink2_scoring_files"

# Thread pool for parallel scoring
executor = ThreadPoolExecutor(max_workers=8)


async def run_fast_scoring(
    source_files: list[dict],
    pgs_ids: list[str],
    pgs_cache_dir: str,
    progress_callback=None,
) -> list[dict]:
    """
    Main entry point for fast PGS scoring.
    
    Args:
        source_files: List of dicts with keys:
            - path: path to gVCF file
            - sample_name: sample identifier
            - population: reference population (EUR, EAS, etc.)
            - type: "gvcf" (only gVCF supported in fast pipeline)
        pgs_ids: List of PGS IDs to score
        pgs_cache_dir: Directory containing downloaded PGS scoring files
        progress_callback: async callable(step, total, message) for progress updates
        
    Returns:
        List of result dicts, one per (sample, pgs) combination
    """
    total_tasks = len(source_files) * len(pgs_ids)
    completed = 0
    results = []
    
    async def report(msg):
        nonlocal completed
        completed += 1
        if progress_callback:
            await progress_callback(completed, total_tasks, msg)
    
    # Phase 1: Ensure all gVCFs are converted to pgen format
    for sf in source_files:
        if sf['type'] != 'gvcf':
            logger.warning(f"Fast pipeline only supports gVCF. Skipping {sf['path']}")
            continue
        
        sample = sf['sample_name']
        pgen_prefix = os.path.join(PGEN_DIR, sample, sample)
        
        if not check_pgen_exists(pgen_prefix):
            if progress_callback:
                await progress_callback(0, total_tasks, f"Converting {sample} gVCF to pgen format...")
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                executor,
                gvcf_to_pgen,
                sf['path'],
                pgen_prefix,
            )
            logger.info(f"Converted {sample} gVCF to pgen")
        else:
            logger.info(f"Using cached pgen for {sample}")
        
        sf['pgen_prefix'] = pgen_prefix
    
    # Phase 2: Prepare plink2-format scoring files for each PGS
    plink2_scoring_files = {}
    for pgs_id in pgs_ids:
        p2_score_path = os.path.join(SCORING_FILE_DIR, f"{pgs_id}.tsv")
        
        if not os.path.exists(p2_score_path):
            # Find the harmonized scoring file in cache
            harmonized = find_harmonized_file(pgs_id, pgs_cache_dir)
            if not harmonized:
                logger.error(f"No harmonized scoring file found for {pgs_id}")
                continue
            
            os.makedirs(SCORING_FILE_DIR, exist_ok=True)
            meta = prepare_plink2_scoring_file(harmonized, p2_score_path)
            logger.info(f"Prepared plink2 scoring file for {pgs_id}: {meta['variant_count']} variants")
        
        plink2_scoring_files[pgs_id] = p2_score_path
    
    # Phase 3: Score all (sample, PGS) combinations
    # Run in parallel via thread pool
    scoring_futures = []
    
    for sf in source_files:
        if 'pgen_prefix' not in sf:
            continue
        
        sample = sf['sample_name']
        population = sf.get('population', 'EUR')
        
        for pgs_id in pgs_ids:
            if pgs_id not in plink2_scoring_files:
                continue
            
            scoring_futures.append({
                'sample': sample,
                'pgs_id': pgs_id,
                'pgen_prefix': sf['pgen_prefix'],
                'scoring_file': plink2_scoring_files[pgs_id],
                'population': population,
                'source_path': sf['path'],
            })
    
    loop = asyncio.get_event_loop()
    
    for task in scoring_futures:
        sample = task['sample']
        pgs_id = task['pgs_id']
        
        out_prefix = os.path.join(PGEN_DIR, sample, f"score_{pgs_id}")
        
        try:
            # Score the sample
            score_result = await loop.run_in_executor(
                executor,
                score_sample_plink2,
                task['pgen_prefix'],
                task['scoring_file'],
                out_prefix,
                pgs_id,
            )
            
            # Get reference panel stats (cached after first computation)
            ref_stats = await loop.run_in_executor(
                executor,
                get_ref_panel_stats,
                pgs_id,
                task['scoring_file'],
                task['population'],
                "GRCh38",
            )
            
            # Compute percentile
            percentile_data = compute_percentile(score_result['raw_score'], ref_stats)
            
            result = {
                'sample_name': sample,
                'source_path': task['source_path'],
                'source_type': 'gvcf',
                'pgs_id': pgs_id,
                'pipeline': 'plink2_native',
                **score_result,
                **percentile_data,
            }
            results.append(result)
            
            await report(f"Scored {sample} x {pgs_id}: percentile={percentile_data['percentile']:.1f}%")
            
        except Exception as e:
            logger.error(f"Scoring failed for {sample} x {pgs_id}: {e}")
            await report(f"Failed: {sample} x {pgs_id}: {str(e)}")
    
    return results


def find_harmonized_file(pgs_id: str, cache_dir: str) -> Optional[str]:
    """Find the harmonized scoring file for a PGS ID in the cache directory."""
    # Try common naming patterns
    patterns = [
        f"{pgs_id}_hmPOS_GRCh38.txt.gz",
        f"{pgs_id}_hmPOS_GRCh38.txt",
        f"{pgs_id}.txt.gz",
        f"{pgs_id}.txt",
    ]
    for pattern in patterns:
        path = os.path.join(cache_dir, pattern)
        if os.path.exists(path):
            return path
    
    # Search for any file starting with the PGS ID
    if os.path.isdir(cache_dir):
        for fname in os.listdir(cache_dir):
            if fname.startswith(pgs_id):
                return os.path.join(cache_dir, fname)
    
    return None
```

### 2.4 Integrate into the existing API

Modify `api/runs.py` to use the fast pipeline for gVCF inputs. The key change: when all source files are gVCFs, use `fast_pipeline.run_fast_scoring()` instead of the existing `engine.py` scoring loop.

Find the scoring run creation/execution logic in `api/runs.py`. It likely has an endpoint like `POST /api/runs/` that creates a run and spawns a background task. Inside that background task, add routing logic:

```python
# In the background scoring task (likely called something like run_scoring_task or execute_run)

from scoring.fast_pipeline import run_fast_scoring

async def execute_run(run_id: str, config: dict):
    """Execute a scoring run. Route to fast pipeline when possible."""
    
    source_files = config['source_files']
    pgs_ids = config['pgs_ids']
    
    # Check if all sources are gVCF - if so, use fast pipeline
    all_gvcf = all(sf.get('type') == 'gvcf' for sf in source_files)
    has_bam = any(sf.get('type') == 'bam' for sf in source_files)
    
    if all_gvcf:
        # FAST PATH: plink2-native scoring (~100x faster)
        logger.info(f"Run {run_id}: Using plink2 fast pipeline (all gVCF inputs)")
        results = await run_fast_scoring(
            source_files=source_files,
            pgs_ids=pgs_ids,
            pgs_cache_dir="/data/pgs_cache",
            progress_callback=lambda step, total, msg: update_progress(run_id, step, total, msg),
        )
    elif has_bam:
        # MIXED or BAM-only: use existing engine
        # Split: score gVCFs via fast pipeline, BAMs via existing pipeline_e_plus
        logger.info(f"Run {run_id}: Using mixed pipeline (BAM + gVCF)")
        
        gvcf_sources = [sf for sf in source_files if sf['type'] == 'gvcf']
        bam_sources = [sf for sf in source_files if sf['type'] == 'bam']
        
        results = []
        
        if gvcf_sources:
            gvcf_results = await run_fast_scoring(
                source_files=gvcf_sources,
                pgs_ids=pgs_ids,
                pgs_cache_dir="/data/pgs_cache",
                progress_callback=lambda step, total, msg: update_progress(run_id, step, total, msg),
            )
            results.extend(gvcf_results)
        
        if bam_sources:
            # Use existing engine.py / pipeline_e_plus.py for BAM scoring
            bam_results = await run_existing_scoring(bam_sources, pgs_ids, run_id)
            results.extend(bam_results)
    else:
        # Standard VCF: use existing engine
        results = await run_existing_scoring(source_files, pgs_ids, run_id)
    
    # Save results to DB (existing code)
    await save_results(run_id, results)
```

### 2.5 Important: variant ID matching

The biggest gotcha in plink2 scoring is variant ID matching between the sample pgen and the scoring file. The approach above uses `chr:pos` as the join key via plink2's `--set-all-var-ids` during conversion.

However, the PGS Catalog scoring files use various formats. You may need to handle:

1. **ID format mismatch**: If plink2 reports 0 matched variants, the IDs don't align. Debug by comparing the first few lines of the `.pvar` file and the scoring file.

2. **Allele flip**: plink2 handles allele flipping automatically with `--score`, but palindromic variants (A/T, C/G) can still cause issues. For maximum accuracy, add `--score ... 'no-mean-imputation'` and handle missing variants explicitly.

3. **Multi-allelic sites**: plink2 splits multi-allelics differently than bcftools. If match rates are lower than expected, normalize both the sample pgen and scoring file to biallelic representation.

**Debug command** to check matching:
```bash
# See what variant IDs look like in the sample pgen
plink2 --pfile /data/pgen_cache/Nimo/Nimo --write-snplist --out /tmp/sample_ids
head /tmp/sample_ids.snplist

# Compare to scoring file
head /data/pgs2/plink2_scoring_files/PGS000327.tsv
```

If IDs don't match, adjust the `--set-all-var-ids` format string in `gvcf_to_pgen()` or the ID construction in `prepare_plink2_scoring_file()` until they align.

---

## Phase 3: Precompute Reference Panel Statistics

### 3.1 Create population sample lists

The 1000 Genomes `.psam` file contains population labels. Extract per-population sample lists:

```bash
mkdir -p /data/pgs2/ref_panel/pop_samples

# Extract population sample lists from the psam file
# The psam file has columns: #IID, SID, PAT, MAT, SEX, SuperPop, Population
# We need to create keep files with format: IID IID (for plink2 --keep)

PSAM="/data/pgs2/ref_panel/1000G_GRCh38.psam"

for POP in EUR EAS AFR SAS AMR; do
    awk -v pop="$POP" '$6 == pop {print $1, $1}' "$PSAM" \
        > /data/pgs2/ref_panel/pop_samples/${POP}.txt
    echo "$POP: $(wc -l < /data/pgs2/ref_panel/pop_samples/${POP}.txt) samples"
done
```

### 3.2 Batch precompute all 49 cached PGS stats

Create a script: `scripts/precompute_ref_stats.py`

```python
"""
One-time script to precompute reference panel statistics for all cached PGS.
Run this once after migration. Takes ~30 minutes for 49 PGS x 5 populations.
After this, all scoring runs skip the reference panel computation step.
"""

import os
import sys
import time

sys.path.insert(0, '/home/nimo/genomics')

from scoring.plink2_scorer import (
    get_ref_panel_stats,
    prepare_plink2_scoring_file,
)
from scoring.fast_pipeline import find_harmonized_file

PGS_CACHE = "/data/pgs_cache"
SCORING_DIR = "/data/pgs2/plink2_scoring_files"
POPULATIONS = ["EUR", "EAS", "AFR", "SAS", "AMR", "ALL"]

os.makedirs(SCORING_DIR, exist_ok=True)

# Find all cached PGS files
pgs_files = {}
for fname in os.listdir(PGS_CACHE):
    if fname.startswith("PGS") and ("hmPOS" in fname or fname.endswith(".txt.gz") or fname.endswith(".txt")):
        pgs_id = fname.split("_")[0].split(".")[0]
        pgs_files[pgs_id] = os.path.join(PGS_CACHE, fname)

print(f"Found {len(pgs_files)} PGS files to process")

for i, (pgs_id, harmonized_path) in enumerate(sorted(pgs_files.items())):
    print(f"\n[{i+1}/{len(pgs_files)}] Processing {pgs_id}...")
    
    # Prepare plink2 scoring file
    p2_path = os.path.join(SCORING_DIR, f"{pgs_id}.tsv")
    if not os.path.exists(p2_path):
        meta = prepare_plink2_scoring_file(harmonized_path, p2_path)
        print(f"  Prepared scoring file: {meta.get('variant_count', '?')} variants")
    
    # Compute stats for each population
    for pop in POPULATIONS:
        t0 = time.time()
        try:
            stats = get_ref_panel_stats(pgs_id, p2_path, pop, "GRCh38")
            elapsed = time.time() - t0
            print(f"  {pop}: mean={stats['mean']:.6f}, std={stats['std']:.6f}, "
                  f"n={stats['n_samples']}, {elapsed:.1f}s")
        except Exception as e:
            print(f"  {pop}: FAILED - {e}")

print("\nDone. All reference panel stats cached.")
```

Run it:
```bash
conda activate genomics
cd /home/nimo/genomics
python scripts/precompute_ref_stats.py
```

### 3.3 Preconvert all existing gVCFs to pgen

```bash
# For each sample that already has a gVCF, convert to pgen
conda activate genomics
python -c "
from scoring.plink2_convert import gvcf_to_pgen, check_pgen_exists
import os, glob

GVCF_DIR = '/scratch/nimog_output'
PGEN_DIR = '/data/pgen_cache'

for gvcf in glob.glob(f'{GVCF_DIR}/*.g.vcf.gz'):
    sample = os.path.basename(gvcf).replace('.g.vcf.gz', '')
    prefix = os.path.join(PGEN_DIR, sample, sample)
    
    if check_pgen_exists(prefix):
        print(f'{sample}: already converted')
        continue
    
    print(f'{sample}: converting...')
    result = gvcf_to_pgen(gvcf, prefix)
    print(f'{sample}: done, {result[\"variant_count\"]} variants')
"
```

---

## Phase 4: Update Web Application

### 4.1 Update time estimation

In `api/runs.py`, find the `/api/runs/estimate` endpoint. Update the estimation logic:

```python
# Old estimation (Python engine): ~0.0015 s/variant
# New estimation (plink2 native): ~0.00001 s/variant for gVCF
# BAM scoring is unchanged: ~0.0015 s/variant

def estimate_run_duration(source_files, pgs_scores):
    total_variants = sum(p['variant_count'] for p in pgs_scores)
    
    gvcf_count = sum(1 for sf in source_files if sf['type'] == 'gvcf')
    bam_count = sum(1 for sf in source_files if sf['type'] == 'bam')
    
    # gVCF via plink2: ~10 sec per PGS per sample (includes overhead)
    gvcf_time = gvcf_count * len(pgs_scores) * 10
    
    # First-time gVCF-to-pgen conversion: ~60 sec per sample
    # Check which samples need conversion
    unconverted = sum(1 for sf in source_files 
                      if sf['type'] == 'gvcf' and not check_pgen_exists(get_pgen_prefix(sf)))
    conversion_time = unconverted * 60
    
    # BAM via existing pipeline: ~0.0015 s/variant
    bam_time = bam_count * total_variants * 0.0015
    
    # Reference panel: ~30 sec per uncached PGS
    uncached_pgs = sum(1 for p in pgs_scores if not is_ref_stats_cached(p['pgs_id']))
    ref_time = uncached_pgs * 30
    
    return {
        'estimated_seconds': conversion_time + max(gvcf_time, bam_time) + ref_time,
        'breakdown': {
            'gvcf_to_pgen_conversion': conversion_time,
            'pgs_scoring_gvcf': gvcf_time,
            'pgs_scoring_bam': bam_time,
            'reference_panel': ref_time,
        }
    }
```

### 4.2 Update the results format

The existing `ResultsPanel.jsx` expects results in a specific format. The fast pipeline must output compatible data. Ensure the result dicts from `fast_pipeline.py` match the schema expected by `run_results` DB table:

```python
# The result dict should match this structure (check existing engine.py output):
{
    "pgs_id": "PGS000327",
    "sample_name": "Nimo",
    "source_path": "/scratch/nimog_output/Nimo.g.vcf.gz",
    "source_type": "gvcf",
    "raw_score": 0.12345,
    "z_score": 1.23,
    "percentile": 89.1,
    "matched_variants": 34500,
    "total_variants": 35087,
    "match_rate": 0.983,
    "ref_mean": 0.0543,
    "ref_std": 0.0562,
    "ref_population": "EUR",
    "ref_n_samples": 503,
    "pipeline": "plink2_native",  # NEW: identifies which pipeline was used
}
```

Check the existing `engine.py` output format and align `fast_pipeline.py` output accordingly. The frontend should not need changes if the output schema matches.

### 4.3 Add pipeline indicator to the UI

In `ResultsPanel.jsx`, the results comparison grid should show which pipeline was used. Add a small badge next to each result:

- "plink2" badge (blue) for fast pipeline results
- "pysam" badge (gray) for BAM-direct pipeline results
- "bcftools" badge (gray) for legacy VCF pipeline results

This helps Nimo verify that the fast pipeline is being used.

### 4.4 Update the Server panel

The `ServerPanel.jsx` (live monitoring) should show GPU utilization when DeepVariant is running. Add an `nvidia-smi` call to the system stats endpoint:

In `api/system.py`, add GPU stats:

```python
import subprocess

def get_gpu_stats():
    """Get NVIDIA GPU stats via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(', ')
            return {
                "gpu_utilization": int(parts[0]),
                "gpu_memory_used_mb": int(parts[1]),
                "gpu_memory_total_mb": int(parts[2]),
                "gpu_temp_c": int(parts[3]),
                "gpu_available": True,
            }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    return {"gpu_available": False}
```

Add this to the `/api/system/stats` response and display it in ServerPanel.jsx.

---

## Phase 5: Validation & Testing

### 5.1 Validate plink2 scoring matches Python engine

**This is critical.** Before switching over, run both pipelines on the same inputs and compare:

```bash
# Score Nimo's gVCF with PGS000327 using BOTH pipelines

# 1. Fast pipeline (plink2)
python -c "
import asyncio
from scoring.fast_pipeline import run_fast_scoring

results = asyncio.run(run_fast_scoring(
    source_files=[{
        'path': '/scratch/nimog_output/Nimo.g.vcf.gz',
        'sample_name': 'Nimo',
        'population': 'EUR',
        'type': 'gvcf',
    }],
    pgs_ids=['PGS000327'],
    pgs_cache_dir='/data/pgs_cache',
))
print('PLINK2 result:', results[0])
"

# 2. Existing engine (Python)
# Trigger via the API or run directly from engine.py
# Compare: raw_score, z_score, percentile, match_rate
```

Expected: scores should be very close but not necessarily identical due to:
- Different handling of multi-allelic sites
- Different allele matching logic
- Different missing-value imputation

**Acceptable tolerance**: z-scores within 0.1, percentiles within 2 percentage points. If larger discrepancies exist, debug the variant ID matching (see section 2.5).

### 5.2 Benchmark the full pipeline

Run the complete 6-sample x 49-PGS workload and record wall times:

```python
# Create a test run via the API
import httpx, time

t0 = time.time()
response = httpx.post("http://localhost:8600/api/runs/", json={
    "source_files": [
        {"path": "/scratch/nimog_output/Nimo.g.vcf.gz", "type": "gvcf", "sample_name": "Nimo", "population": "EUR"},
        # ... add all 6 samples once all gVCFs are generated
    ],
    "pgs_ids": [
        "PGS000327", "PGS002790", # ... all 49 cached PGS IDs
    ],
})
# Monitor via WebSocket until complete
elapsed = time.time() - t0
print(f"Total wall time: {elapsed:.0f} seconds")
```

**Target**: under 25 minutes for all 6 samples x 49 PGS (vs ~12 hours with Python engine).

### 5.3 Validate DeepVariant GPU output

Compare GPU DeepVariant output to CPU output for the same sample:

```bash
# Already have CPU output for Nimo
# Run GPU DeepVariant on Nimo and compare VCFs

bcftools stats /scratch/nimog_output/Nimo.vcf.gz > /tmp/cpu_stats.txt
bcftools stats /scratch/nimog_output/Nimo_gpu.vcf.gz > /tmp/gpu_stats.txt

# Compare variant counts - should be nearly identical
# Small differences (<0.1%) are normal due to floating point in the neural net
bcftools isec -p /tmp/isec_compare \
    /scratch/nimog_output/Nimo.vcf.gz \
    /scratch/nimog_output/Nimo_gpu.vcf.gz

echo "CPU-only variants: $(wc -l < /tmp/isec_compare/0000.vcf)"
echo "GPU-only variants: $(wc -l < /tmp/isec_compare/0001.vcf)"
echo "Shared variants: $(wc -l < /tmp/isec_compare/0002.vcf)"
```

---

## Summary: File Changes

| File | Action | Description |
|------|--------|-------------|
| `scoring/plink2_convert.py` | CREATE | gVCF to pgen conversion |
| `scoring/plink2_scorer.py` | CREATE | plink2-native PGS scoring + ref panel caching |
| `scoring/fast_pipeline.py` | CREATE | Orchestrator for fast pipeline |
| `scripts/precompute_ref_stats.py` | CREATE | One-time ref panel precomputation |
| `api/runs.py` | MODIFY | Route gVCF inputs to fast pipeline, update time estimates |
| `api/system.py` | MODIFY | Add GPU stats to system monitoring |
| `nimog/*.py` | MODIFY | Switch to GPU DeepVariant container, `--nv` flag, reduce shards |
| `frontend/src/ResultsPanel.jsx` | MODIFY | Add pipeline badge |
| `frontend/src/ServerPanel.jsx` | MODIFY | Show GPU utilization |

## Summary: New Directories

| Directory | Purpose |
|-----------|---------|
| `/data/pgen_cache/` | Converted pgen files per sample |
| `/data/pgs2/plink2_scoring_files/` | plink2-format scoring files |
| `/data/pgs2/ref_panel_stats/` | Cached mean/std per PGS per population |
| `/data/pgs2/ref_panel/pop_samples/` | Per-population sample keep files |
| `/data/containers/` | Apptainer/Singularity container images |

## Expected Performance After Migration

| Operation | Before | After | Speedup |
|-----------|--------|-------|---------|
| DeepVariant per sample | 2-3 hr | 25-40 min | ~4x |
| PGS scoring (1M variants, gVCF) | 30 min | 5-10 sec | ~200x |
| PGS scoring (35K variants, gVCF) | 60 sec | 3 sec | ~20x |
| Reference panel (cached) | 30-40 sec | 0 sec | instant |
| Full pipeline (6 samples x 49 PGS) | ~26 hr | ~2.5 hr | ~10x |

## GCE Monthly Cost Estimate

| Instance | Type | Monthly (on-demand) |
|----------|------|---------------------|
| Current | c3-standard-44-lssd | ~$1,505 |
| New | n1-standard-32 + T4 | ~$1,230 |
| Savings | | ~$275/mo (18%) |

The n1+T4 is both faster and cheaper than the c3-standard-44.
