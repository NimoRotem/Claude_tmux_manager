#!/usr/bin/env python3
"""
Deploy landing page and routing changes to 23andclaude.com (simple-genomics).

Changes:
  1. Adds LANDING_PAGE_HTML constant (the new public homepage)
  2. GET /        → landing page (was: SPA dashboard)
  3. GET /app     → SPA dashboard (new route)
  4. GET /sign-in → login-only auth page (no signup toggle)
  5. GET /login   → 301 redirect to /sign-in
  6. GET /signup  → 301 redirect to /sign-in
  7. Updates all JS auth redirects (/login → /sign-in, / → /app)
  8. Hides the signup toggle link in the auth page

Run on genom-beast-gpu:
  python3 deploy_landing.py
  sudo supervisorctl restart simple-genomics
"""

import shutil
import datetime
import sys

APP = '/home/nimrod_rotem/simple-genomics/app.py'

# ---------------------------------------------------------------------------
# Landing page HTML
# ---------------------------------------------------------------------------
LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>23 &amp; Claude &mdash; Open Source Genomics for AI</title>
<meta name="description" content="Open-source genomics analysis server that turns raw DNA files into structured results AI can read, explain, and reason about.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M8 2C12 8 20 8 24 2' fill='none' stroke='%2306b6d4' stroke-width='2'/%3E%3Cpath d='M8 10C12 16 20 16 24 10' fill='none' stroke='%238b5cf6' stroke-width='2'/%3E%3Cpath d='M8 18C12 24 20 24 24 18' fill='none' stroke='%2306b6d4' stroke-width='2'/%3E%3Cpath d='M8 26C12 32 20 32 24 26' fill='none' stroke='%238b5cf6' stroke-width='2'/%3E%3C/svg%3E">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,sans-serif;background:#0a0e17;color:#e0e6f0;line-height:1.7;-webkit-font-smoothing:antialiased}
a{color:#06b6d4;text-decoration:none;transition:color .2s}
a:hover{color:#22d3ee}
.container{max-width:1140px;margin:0 auto;padding:0 24px}

/* ── Nav ── */
.nav{position:fixed;top:0;left:0;right:0;z-index:100;background:rgba(10,14,23,.85);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,.06)}
.nav-inner{max-width:1140px;margin:0 auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:60px}
.logo{font-size:1.15rem;font-weight:700;color:#f9fafb;display:flex;align-items:center;gap:8px}
.logo svg{width:22px;height:22px}
.nav-links{display:flex;align-items:center;gap:24px;font-size:.9rem}
.nav-links a{color:#9ca3af}
.nav-links a:hover{color:#f9fafb}
.btn{display:inline-flex;align-items:center;gap:8px;padding:8px 18px;border-radius:8px;font-size:.9rem;font-weight:600;transition:all .2s;border:none;cursor:pointer;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#06b6d4,#8b5cf6);color:#fff}
.btn-primary:hover{opacity:.9;color:#fff;transform:translateY(-1px)}
.btn-outline{border:1px solid #374151;color:#d1d5db;background:transparent}
.btn-outline:hover{border-color:#06b6d4;color:#06b6d4}
.btn-sm{padding:6px 14px;font-size:.85rem}
.btn-lg{padding:12px 28px;font-size:1rem}

/* ── Hero ── */
.hero{min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:120px 24px 80px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-40%;left:-20%;width:140%;height:140%;background:radial-gradient(ellipse at 30% 20%,rgba(6,182,212,.08) 0%,transparent 60%),radial-gradient(ellipse at 70% 80%,rgba(139,92,246,.06) 0%,transparent 60%);pointer-events:none}
.hero-inner{position:relative;max-width:800px}
.hero-badge{display:inline-block;padding:6px 16px;border-radius:20px;border:1px solid rgba(6,182,212,.3);color:#06b6d4;font-size:.8rem;font-weight:600;letter-spacing:.03em;margin-bottom:28px;background:rgba(6,182,212,.06)}
.hero h1{font-size:clamp(2.4rem,6vw,4rem);font-weight:800;line-height:1.15;margin-bottom:24px;color:#f9fafb}
.gradient-text{background:linear-gradient(135deg,#06b6d4 0%,#8b5cf6 50%,#06b6d4 100%);background-size:200% auto;-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;animation:shimmer 4s ease-in-out infinite}
@keyframes shimmer{0%,100%{background-position:0% center}50%{background-position:200% center}}
.hero-sub{font-size:clamp(1rem,2.5vw,1.25rem);color:#9ca3af;max-width:640px;margin:0 auto 36px;line-height:1.8}
.hero-ctas{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}

/* ── Sections ── */
section{padding:100px 0}
section:nth-child(even){background:rgba(255,255,255,.015)}
.section-label{font-size:.8rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#06b6d4;margin-bottom:12px}
.section-title{font-size:clamp(1.8rem,4vw,2.5rem);font-weight:800;color:#f9fafb;margin-bottom:16px}
.section-sub{font-size:1.05rem;color:#9ca3af;max-width:600px;margin-bottom:56px}
.text-center{text-align:center}
.section-sub.center{margin-left:auto;margin-right:auto}

/* ── Grid ── */
.grid{display:grid;gap:24px}
.grid-2{grid-template-columns:repeat(2,1fr)}
.grid-3{grid-template-columns:repeat(3,1fr)}
@media(max-width:900px){.grid-3{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.grid-2,.grid-3{grid-template-columns:1fr}}

/* ── Cards ── */
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:32px;transition:border-color .2s,transform .2s}
.card:hover{border-color:#374151;transform:translateY(-2px)}
.card-icon{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.2rem;margin-bottom:16px;font-weight:700;color:#fff}
.card-icon.cyan{background:rgba(6,182,212,.15);color:#06b6d4}
.card-icon.violet{background:rgba(139,92,246,.15);color:#8b5cf6}
.card-icon.green{background:rgba(16,185,129,.15);color:#10b981}
.card-icon.orange{background:rgba(245,158,11,.15);color:#f59e0b}
.card-icon.red{background:rgba(239,68,68,.15);color:#ef4444}
.card-icon.gray{background:rgba(156,163,175,.12);color:#9ca3af}
.card h3{font-size:1.1rem;font-weight:700;color:#f9fafb;margin-bottom:8px}
.card p{color:#9ca3af;font-size:.92rem;line-height:1.7}

/* ── Steps ── */
.steps{display:flex;gap:0;position:relative;flex-wrap:wrap;justify-content:center}
.step{flex:1;min-width:180px;max-width:220px;text-align:center;padding:0 16px;position:relative}
.step-num{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#06b6d4,#8b5cf6);color:#fff;font-weight:800;font-size:1rem;display:flex;align-items:center;justify-content:center;margin:0 auto 14px}
.step h4{font-size:.95rem;font-weight:700;color:#f9fafb;margin-bottom:6px}
.step p{font-size:.82rem;color:#9ca3af;line-height:1.6}
.step-connector{display:none}
@media(min-width:900px){
  .step-connector{display:block;position:absolute;top:20px;right:-16px;width:32px;height:2px;background:linear-gradient(90deg,#06b6d4,#8b5cf6)}
  .step:last-child .step-connector{display:none}
}

/* ── Mock Screenshots ── */
.mock{background:#0d1117;border:1px solid #21262d;border-radius:12px;overflow:hidden}
.mock-bar{height:36px;background:#161b22;display:flex;align-items:center;padding:0 14px;gap:8px;border-bottom:1px solid #21262d}
.mock-dot{width:10px;height:10px;border-radius:50%}
.mock-dot.r{background:#f85149}.mock-dot.y{background:#d29922}.mock-dot.g{background:#3fb950}
.mock-title{margin-left:12px;font-size:.75rem;color:#8b949e;font-weight:600}
.mock-body{padding:20px;font-size:.82rem;line-height:1.7}

/* Mock: test grid */
.mock-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
@media(max-width:600px){.mock-grid{grid-template-columns:repeat(2,1fr)}}
.mock-card{padding:10px;border-radius:6px;font-size:.72rem;font-weight:600;border:1px solid}
.mock-card.mc-cyan{background:rgba(6,182,212,.08);border-color:rgba(6,182,212,.2);color:#06b6d4}
.mock-card.mc-orange{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.2);color:#f59e0b}
.mock-card.mc-green{background:rgba(16,185,129,.08);border-color:rgba(16,185,129,.2);color:#10b981}
.mock-card.mc-violet{background:rgba(139,92,246,.08);border-color:rgba(139,92,246,.2);color:#8b5cf6}
.mock-card.mc-gray{background:rgba(156,163,175,.06);border-color:rgba(156,163,175,.15);color:#8b949e}
.mock-card span{display:block;font-size:.65rem;color:#8b949e;font-weight:400;margin-top:2px}

/* Mock: report */
.mock-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #21262d;font-size:.78rem}
.mock-row:last-child{border-bottom:none}
.mock-label{color:#c9d1d9;font-weight:600}
.mock-val{font-family:'SF Mono',Monaco,Consolas,monospace;font-size:.78rem}
.mock-val.high{color:#3fb950}.mock-val.mid{color:#d29922}.mock-val.low{color:#f85149}
.mock-badge{padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:700}
.mock-badge.badge-green{background:rgba(63,185,80,.15);color:#3fb950}
.mock-badge.badge-cyan{background:rgba(6,182,212,.15);color:#06b6d4}

/* Mock: ancestry bars */
.anc-bar-wrap{margin-bottom:10px}
.anc-bar-label{display:flex;justify-content:space-between;font-size:.78rem;margin-bottom:4px}
.anc-bar-label span:first-child{color:#c9d1d9}.anc-bar-label span:last-child{color:#8b949e}
.anc-bar{height:8px;border-radius:4px;background:#21262d;overflow:hidden}
.anc-bar-fill{height:100%;border-radius:4px;transition:width .6s ease}

/* ── Specs table ── */
.spec-table{width:100%;border-collapse:collapse;font-size:.88rem}
.spec-table th{text-align:left;padding:12px 16px;background:#111827;color:#9ca3af;font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #1f2937}
.spec-table td{padding:12px 16px;border-bottom:1px solid #1f2937;color:#d1d5db}
.spec-table tr:hover td{background:rgba(255,255,255,.02)}
.spec-table code{background:rgba(6,182,212,.1);color:#06b6d4;padding:2px 6px;border-radius:4px;font-size:.82rem}

/* ── CTA ── */
.cta-section{text-align:center;padding:100px 24px;position:relative}
.cta-section::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at center,rgba(139,92,246,.06) 0%,transparent 70%);pointer-events:none}
.cta-inner{position:relative}
.cta-inner h2{font-size:clamp(1.8rem,4vw,2.8rem);font-weight:800;color:#f9fafb;margin-bottom:12px}
.cta-inner p{color:#9ca3af;font-size:1.05rem;margin-bottom:32px}
.cta-inner .btn{font-size:1rem}
.github-icon{width:20px;height:20px;fill:currentColor;vertical-align:middle}
.github-stats{display:flex;gap:32px;justify-content:center;margin-top:36px;flex-wrap:wrap}
.github-stat{text-align:center}
.github-stat .num{font-size:1.6rem;font-weight:800;color:#f9fafb;display:block}
.github-stat .lbl{font-size:.8rem;color:#6b7280}

/* ── Footer ── */
footer{border-top:1px solid #1f2937;padding:32px 24px;text-align:center;font-size:.82rem;color:#6b7280}
footer a{color:#6b7280}
footer a:hover{color:#9ca3af}

/* ── Example output ── */
.example-output{background:#0d1117;border:1px solid #21262d;border-radius:12px;padding:28px;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:.82rem;line-height:1.8;color:#c9d1d9;overflow-x:auto;max-width:700px;margin:0 auto}
.example-output .eo-h2{color:#f9fafb;font-size:.95rem;font-weight:700;margin-bottom:4px;font-family:inherit}
.example-output .eo-h3{color:#d1d5db;font-size:.85rem;font-weight:600;margin-top:12px;margin-bottom:2px;font-family:inherit}
.example-output .eo-key{color:#8b949e}
.example-output .eo-val{color:#06b6d4}
.example-output .eo-val-good{color:#3fb950}
.example-output .eo-val-warn{color:#d29922}
.example-output .eo-val-bad{color:#f85149}

/* ── Responsive ── */
@media(max-width:600px){
  .hero{padding:100px 20px 60px}
  section{padding:60px 0}
  .card{padding:24px}
  .nav-links .hide-mobile{display:none}
}
</style>
</head>
<body>

<!-- ====== NAV ====== -->
<nav class="nav">
  <div class="nav-inner">
    <a href="/" class="logo">
      <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M8 2C12 8 20 8 24 2" stroke="#06b6d4" stroke-width="2.5"/><path d="M8 11C12 17 20 17 24 11" stroke="#8b5cf6" stroke-width="2.5"/><path d="M8 20C12 26 20 26 24 20" stroke="#06b6d4" stroke-width="2.5"/></svg>
      23 &amp; Claude
    </a>
    <div class="nav-links">
      <a href="#features" class="hide-mobile">Features</a>
      <a href="#how-it-works" class="hide-mobile">How It Works</a>
      <a href="https://github.com/NimoRotem/23andClaude" target="_blank" rel="noopener">GitHub</a>
      <a href="/sign-in" class="btn btn-outline btn-sm" id="navSignin">Sign In</a>
    </div>
  </div>
</nav>

<!-- ====== HERO ====== -->
<section class="hero">
  <div class="hero-inner">
    <div class="hero-badge">Open Source &middot; Free Forever</div>
    <h1>Your Genome,<br><span class="gradient-text">Interpreted by AI</span></h1>
    <p class="hero-sub">
      A genomics analysis server that runs real bioinformatics pipelines on your DNA files
      and produces structured results that Claude can read, explain, and reason about.
    </p>
    <div class="hero-ctas">
      <a href="https://github.com/NimoRotem/23andClaude" target="_blank" rel="noopener" class="btn btn-primary btn-lg">
        <svg class="github-icon" viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        View on GitHub
      </a>
      <a href="/sign-in" class="btn btn-outline btn-lg" id="heroSignin">Sign In</a>
    </div>
  </div>
</section>

<!-- ====== WHY THIS EXISTS ====== -->
<section id="why">
  <div class="container text-center">
    <div class="section-label">The Problem</div>
    <h2 class="section-title">Why This Exists</h2>
    <p class="section-sub center">
      Your genome lives in formats that are enormous, binary, and completely opaque to AI.
    </p>
    <div class="grid grid-3">
      <div class="card">
        <div class="card-icon cyan">AI</div>
        <h3>LLMs Cannot Read Your DNA</h3>
        <p>A whole-genome BAM is 50&ndash;100 GB of compressed read alignments. A gVCF packs tens of millions of variant calls in formats optimized for bioinformatics tools, not language models. No AI can open or reason about these files.</p>
      </div>
      <div class="card">
        <div class="card-icon violet">?</div>
        <h3>Consumer Services Are Black Boxes</h3>
        <p>23andMe and Promethease give you fixed reports. You cannot ask follow-up questions, inspect call confidence, drill into star-allele ambiguity, or compare two variants side by side.</p>
      </div>
      <div class="card">
        <div class="card-icon orange">!</div>
        <h3>Raw Tools Fail Silently</h3>
        <p>plink2 and bcftools will happily score a GRCh37 VCF against GRCh38 coordinates&mdash;no error, clean-looking output, completely wrong results. Both humans and AI agents make these mistakes.</p>
      </div>
    </div>
  </div>
</section>

<!-- ====== HOW IT WORKS ====== -->
<section id="how-it-works">
  <div class="container text-center">
    <div class="section-label">Pipeline</div>
    <h2 class="section-title">How It Works</h2>
    <p class="section-sub center">
      The deterministic pipelines do the heavy lifting. The AI does the reasoning.
    </p>
    <div class="steps">
      <div class="step">
        <div class="step-num">1</div>
        <h4>Upload</h4>
        <p>Register a VCF, gVCF, BAM, or CRAM file from local storage, upload, or URL.</p>
        <div class="step-connector"></div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <h4>Prepare</h4>
        <p>VCF/gVCF files are converted to plink2 binary format. BAM/CRAM skip this step.</p>
        <div class="step-connector"></div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <h4>Pick a Test</h4>
        <p>Choose from 280+ polygenic scores, monogenic panels, pharmacogenomics, ancestry, and more.</p>
        <div class="step-connector"></div>
      </div>
      <div class="step">
        <div class="step-num">4</div>
        <h4>Pipeline Runs</h4>
        <p>The right validated tool runs with build validation, QC gates, and sanity checks.</p>
        <div class="step-connector"></div>
      </div>
      <div class="step">
        <div class="step-num">5</div>
        <h4>AI Reads Results</h4>
        <p>Structured markdown output that Claude can read, explain, and cross-reference.</p>
      </div>
    </div>
  </div>
</section>

<!-- ====== FEATURES ====== -->
<section id="features">
  <div class="container text-center">
    <div class="section-label">Capabilities</div>
    <h2 class="section-title">What It Can Do</h2>
    <p class="section-sub center">
      38 curated tests, 284 polygenic scores, 51 monogenic panels, 19 pharmacogenomic tests, and 16 validation checks.
    </p>
    <div class="grid grid-3">
      <div class="card">
        <div class="card-icon cyan">P</div>
        <h3>Polygenic Risk Scores</h3>
        <p>Score against any PGS Catalog file using plink2 + 1000 Genomes reference panel. Precomputed EUR percentiles with sanity gates: |z|&gt;6 fails, |z|&gt;4 warns, std-collapse detected.</p>
      </div>
      <div class="card">
        <div class="card-icon orange">M</div>
        <h3>Monogenic Screening</h3>
        <p>ClinVar annotation for Pathogenic and Likely_pathogenic variants. ACMG SF v3.3 panels: Cancer predisposition, Cardiovascular, Metabolism. Auto-cached annotated VCFs.</p>
      </div>
      <div class="card">
        <div class="card-icon green">Rx</div>
        <h3>Pharmacogenomics</h3>
        <p>Star-allele calling for CYP2D6 (via Cyrius), CYP2C19, CYP2C9, DPYD, TPMT, NUDT15, SLCO1B1, UGT1A1, and more. Allele verification prevents false positive calls.</p>
      </div>
      <div class="card">
        <div class="card-icon violet">A</div>
        <h3>Ancestry &amp; Haplogroups</h3>
        <p>PCA projection onto 1000G, ADMIXTURE K=5, Y-DNA haplogroup (ISOGG), mtDNA haplogroup (HaploGrep3), Neanderthal %, runs of homozygosity, HLA typing (T1K).</p>
      </div>
      <div class="card">
        <div class="card-icon red">R</div>
        <h3>Repeat Expansions</h3>
        <p>ExpansionHunter v5 calls trinucleotide repeats from BAM/CRAM: FMR1 (Fragile X), HTT (Huntington&rsquo;s), DMPK (Myotonic Dystrophy). Per-allele counts + clinical classification.</p>
      </div>
      <div class="card">
        <div class="card-icon gray">Q</div>
        <h3>Sample QC &amp; Sex Check</h3>
        <p>Ti/Tv ratio, Het/Hom ratio, SNP/indel counts via bcftools stats. Sex verification via Y-chromosome reads, SRY coverage, X:Y ratio, chrX het rate.</p>
      </div>
    </div>
  </div>
</section>

<!-- ====== INTERFACE (Mock Screenshots) ====== -->
<section id="interface">
  <div class="container text-center">
    <div class="section-label">Interface</div>
    <h2 class="section-title">The Dashboard</h2>
    <p class="section-sub center">
      A single-page app for managing files, running tests, and browsing results.
    </p>

    <!-- Mock 1: Test Dashboard -->
    <div class="mock" style="max-width:800px;margin:0 auto 40px">
      <div class="mock-bar">
        <div class="mock-dot r"></div><div class="mock-dot y"></div><div class="mock-dot g"></div>
        <div class="mock-title">Test Dashboard &mdash; 23 &amp; Claude</div>
      </div>
      <div class="mock-body">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
          <div style="font-size:.9rem;color:#f9fafb;font-weight:700">Run Tests</div>
          <div style="padding:4px 12px;border:1px solid #30363d;border-radius:6px;font-size:.72rem;color:#8b949e">sample.g.vcf.gz (GRCh38, Ready)</div>
        </div>
        <div class="mock-grid">
          <div class="mock-card mc-cyan">PGS: Coronary Artery Disease<span>PGS000018 &middot; Khera 2018</span></div>
          <div class="mock-card mc-cyan">PGS: Type 2 Diabetes<span>PGS000014 &middot; Mahajan 2018</span></div>
          <div class="mock-card mc-cyan">PGS: Breast Cancer<span>PGS000004 &middot; Mavaddat 2019</span></div>
          <div class="mock-card mc-orange">Monogenic: BRCA1/BRCA2<span>ClinVar &middot; Cancer Panel</span></div>
          <div class="mock-card mc-orange">Monogenic: Cardiovascular<span>ClinVar &middot; ACMG SF v3.3</span></div>
          <div class="mock-card mc-green">PGx: CYP2D6<span>Cyrius &middot; Star Allele</span></div>
          <div class="mock-card mc-green">PGx: CYP2C19<span>Variant Lookup</span></div>
          <div class="mock-card mc-violet">Ancestry: PCA + ADMIXTURE<span>1000G &middot; 106K sites</span></div>
          <div class="mock-card mc-gray">QC: Sample Statistics<span>bcftools stats</span></div>
        </div>
      </div>
    </div>

    <!-- Mock 2: PGS Result -->
    <div class="grid grid-2" style="max-width:800px;margin:0 auto 40px">
      <div class="mock">
        <div class="mock-bar">
          <div class="mock-dot r"></div><div class="mock-dot y"></div><div class="mock-dot g"></div>
          <div class="mock-title">PGS Result</div>
        </div>
        <div class="mock-body">
          <div style="font-size:.85rem;color:#f9fafb;font-weight:700;margin-bottom:12px">Coronary Artery Disease</div>
          <div class="mock-row"><span class="mock-label">Score</span><span class="mock-val">PGS000018</span></div>
          <div class="mock-row"><span class="mock-label">Raw</span><span class="mock-val">0.00247</span></div>
          <div class="mock-row"><span class="mock-label">Z-score</span><span class="mock-val mid">1.84</span></div>
          <div class="mock-row"><span class="mock-label">Percentile</span><span class="mock-val mid">96.7th (EUR)</span></div>
          <div class="mock-row"><span class="mock-label">Variants</span><span class="mock-val">6,268,514 / 6,630,150</span></div>
          <div class="mock-row"><span class="mock-label">Quality</span><span class="mock-badge badge-green">HIGH</span></div>
        </div>
      </div>

      <!-- Mock 3: Ancestry -->
      <div class="mock">
        <div class="mock-bar">
          <div class="mock-dot r"></div><div class="mock-dot y"></div><div class="mock-dot g"></div>
          <div class="mock-title">Ancestry Composition</div>
        </div>
        <div class="mock-body">
          <div class="anc-bar-wrap">
            <div class="anc-bar-label"><span>Middle Eastern</span><span>63.4%</span></div>
            <div class="anc-bar"><div class="anc-bar-fill" style="width:63.4%;background:#06b6d4"></div></div>
          </div>
          <div class="anc-bar-wrap">
            <div class="anc-bar-label"><span>South European</span><span>31.0%</span></div>
            <div class="anc-bar"><div class="anc-bar-fill" style="width:31%;background:#8b5cf6"></div></div>
          </div>
          <div class="anc-bar-wrap">
            <div class="anc-bar-label"><span>Central/Khoisan African</span><span>5.6%</span></div>
            <div class="anc-bar"><div class="anc-bar-fill" style="width:5.6%;background:#10b981"></div></div>
          </div>
          <div style="margin-top:16px;border-top:1px solid #21262d;padding-top:12px">
            <div class="mock-row"><span class="mock-label">mtDNA</span><span class="mock-val">H1a</span></div>
            <div class="mock-row"><span class="mock-label">Y-DNA</span><span class="mock-val">J-M267</span></div>
            <div class="mock-row"><span class="mock-label">F_ROH</span><span class="mock-val high">0.22% (outbred)</span></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- ====== EXAMPLE OUTPUT ====== -->
<section id="output">
  <div class="container text-center">
    <div class="section-label">AI-Readable</div>
    <h2 class="section-title">What Claude Sees</h2>
    <p class="section-sub center">
      Every test produces structured markdown that Claude reads natively&mdash;no parsing, no guessing.
    </p>
    <div class="example-output" style="text-align:left">
      <div class="eo-h2">## Polygenic Risk Score: Coronary Artery Disease</div>
      <div><span class="eo-key">Score ID:</span> <span class="eo-val">PGS000018</span> (Khera et al. 2018)</div>
      <div><span class="eo-key">Raw score:</span> <span class="eo-val">0.00247</span></div>
      <div><span class="eo-key">Z-score:</span> <span class="eo-val-warn">1.84</span></div>
      <div><span class="eo-key">Percentile:</span> <span class="eo-val-warn">96.7</span> (EUR reference, precomputed)</div>
      <div><span class="eo-key">Variants matched:</span> <span class="eo-val">6,268,514 / 6,630,150</span> (94.5%)</div>
      <div><span class="eo-key">Match quality:</span> <span class="eo-val-good">HIGH</span></div>
      <div class="eo-h3">### Sanity Checks</div>
      <div><span class="eo-key">Build validation:</span> <span class="eo-val-good">PASS</span> (GRCh38 confirmed)</div>
      <div><span class="eo-key">Score SD:</span> <span class="eo-val">0.00134</span> (no collapse)</div>
      <div><span class="eo-key">Z-score range:</span> <span class="eo-val-good">|z| &lt; 4, within normal bounds</span></div>
    </div>
  </div>
</section>

<!-- ====== SPECIFICATIONS ====== -->
<section id="specs">
  <div class="container">
    <div class="text-center">
      <div class="section-label">Specifications</div>
      <h2 class="section-title">Supported Formats &amp; Tools</h2>
      <p class="section-sub center">
        Battle-tested bioinformatics pipelines under the hood.
      </p>
    </div>
    <div class="grid grid-2" style="max-width:900px;margin:0 auto">
      <div>
        <h3 style="font-size:1rem;color:#f9fafb;margin-bottom:16px;font-weight:700">Input Formats</h3>
        <table class="spec-table">
          <thead><tr><th>Format</th><th>Prep Time</th><th>Best For</th></tr></thead>
          <tbody>
            <tr><td><code>.g.vcf.gz</code></td><td>5&ndash;15 min</td><td>Highest accuracy</td></tr>
            <tr><td><code>.vcf.gz</code></td><td>5&ndash;30 sec</td><td>Quick results</td></tr>
            <tr><td><code>.bam</code></td><td>No prep</td><td>On-demand calling</td></tr>
            <tr><td><code>.cram</code></td><td>No prep</td><td>Compressed BAM</td></tr>
          </tbody>
        </table>
      </div>
      <div>
        <h3 style="font-size:1rem;color:#f9fafb;margin-bottom:16px;font-weight:700">Pipeline Tools</h3>
        <table class="spec-table">
          <thead><tr><th>Tool</th><th>Purpose</th></tr></thead>
          <tbody>
            <tr><td><code>plink2</code></td><td>PGS scoring, PCA</td></tr>
            <tr><td><code>bcftools</code></td><td>ClinVar, variant queries</td></tr>
            <tr><td><code>Cyrius</code></td><td>CYP2D6 star alleles</td></tr>
            <tr><td><code>ExpansionHunter</code></td><td>Repeat expansions</td></tr>
            <tr><td><code>HaploGrep3</code></td><td>mtDNA haplogroup</td></tr>
            <tr><td><code>T1K</code></td><td>HLA typing</td></tr>
            <tr><td><code>samtools</code></td><td>BAM/CRAM processing</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<!-- ====== SAFETY ====== -->
<section>
  <div class="container text-center">
    <div class="section-label">Reliability</div>
    <h2 class="section-title">Fail Loudly, Never Silently</h2>
    <p class="section-sub center">
      The pipeline catches the mistakes that both humans and AI agents routinely make.
    </p>
    <div class="grid grid-3">
      <div class="card">
        <div class="card-icon cyan">38</div>
        <h3>Build Validation</h3>
        <p>GRCh37 vs GRCh38 mismatch is caught before scoring begins. Sentinel SNP spot-checks confirm coordinate alignment.</p>
      </div>
      <div class="card">
        <div class="card-icon violet">z</div>
        <h3>Sanity Gates</h3>
        <p>|z|&gt;6 fails the test. |z|&gt;4 triggers a warning. Standard-deviation collapse is detected. Percentiles are capped at [0.5, 99.5].</p>
      </div>
      <div class="card">
        <div class="card-icon green">A/T</div>
        <h3>Allele Verification</h3>
        <p>Position-only hits without matching REF/ALT are reported as locus_mismatch, not false positives. gVCF reference blocks correctly resolve to hom-ref.</p>
      </div>
    </div>
  </div>
</section>

<!-- ====== OPEN SOURCE CTA ====== -->
<section class="cta-section">
  <div class="cta-inner">
    <h2>Free. Open Source.<br>No Strings Attached.</h2>
    <p>Not a company. Not a service. Just science you can run on your own hardware.</p>
    <a href="https://github.com/NimoRotem/23andClaude" target="_blank" rel="noopener" class="btn btn-primary btn-lg">
      <svg class="github-icon" viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
      View on GitHub
    </a>
    <div class="github-stats">
      <div class="github-stat"><span class="num">284</span><span class="lbl">Polygenic Scores</span></div>
      <div class="github-stat"><span class="num">51</span><span class="lbl">Monogenic Panels</span></div>
      <div class="github-stat"><span class="num">19</span><span class="lbl">PGx Tests</span></div>
      <div class="github-stat"><span class="num">7</span><span class="lbl">Pipeline Tools</span></div>
    </div>
  </div>
</section>

<!-- ====== FOOTER ====== -->
<footer>
  <div class="container">
    23 &amp; Claude &mdash; open-source genomics for AI &middot;
    <a href="https://github.com/NimoRotem/23andClaude" target="_blank" rel="noopener">GitHub</a>
  </div>
</footer>

<script>
(function(){
  // If already authenticated, change Sign In to Dashboard
  fetch('/api/auth/me',{credentials:'same-origin'}).then(function(r){
    if(r.ok){
      var btns=document.querySelectorAll('#navSignin,#heroSignin');
      for(var i=0;i<btns.length;i++){
        btns[i].textContent='Go to Dashboard';
        btns[i].href='/app';
      }
    }
  }).catch(function(){});

  // Smooth scroll for anchor links
  document.querySelectorAll('a[href^="#"]').forEach(function(a){
    a.addEventListener('click',function(e){
      var t=document.querySelector(this.getAttribute('href'));
      if(t){e.preventDefault();t.scrollIntoView({behavior:'smooth',block:'start'});}
    });
  });

  // Nav background on scroll
  var nav=document.querySelector('.nav');
  window.addEventListener('scroll',function(){
    nav.style.borderBottomColor=window.scrollY>50?'rgba(255,255,255,0.08)':'rgba(255,255,255,0.06)';
  });
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Patch logic
# ---------------------------------------------------------------------------

def main():
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'{APP}.pre-landing-{ts}'
    shutil.copy2(APP, backup)
    print(f'Backup: {backup}')

    with open(APP) as f:
        src = f.read()

    changes = 0
    original_len = len(src)

    # --- 1. Insert LANDING_PAGE_HTML before _AUTH_PAGE_HTML ---
    marker = '_AUTH_PAGE_HTML = r'
    if marker not in src:
        print(f'FATAL: cannot find marker: {marker}')
        sys.exit(1)
    landing_const = 'LANDING_PAGE_HTML = r"""' + LANDING_HTML + '"""\n\n'
    src = src.replace(marker, landing_const + marker, 1)
    changes += 1
    print(f'[{changes}] Inserted LANDING_PAGE_HTML constant')

    # --- 2. Replace index route: / serves landing, add /app for SPA ---
    old_index = (
        '@app.get("/", response_class=HTMLResponse)\n'
        'async def index():\n'
        '    # The dashboard is a SPA. We always serve the frontend; the JS\n'
        '    # bootstrap calls /api/auth/me and redirects to the login page on\n'
        '    # 401 using its prefix-aware BASE constant. This sidesteps any\n'
        '    # confusion about the nginx /simple/ prefix not being visible to\n'
        '    # FastAPI here.\n'
        '    return HTMLResponse(FRONTEND_HTML)'
    )
    new_index = (
        '@app.get("/", response_class=HTMLResponse)\n'
        'async def landing():\n'
        '    """Public landing page."""\n'
        '    return HTMLResponse(LANDING_PAGE_HTML)\n'
        '\n'
        '\n'
        '@app.get("/app", response_class=HTMLResponse)\n'
        'async def app_dashboard():\n'
        '    """Main SPA dashboard (auth checked client-side)."""\n'
        '    return HTMLResponse(FRONTEND_HTML)'
    )
    if old_index not in src:
        print('FATAL: index route block not found (text mismatch)')
        sys.exit(1)
    src = src.replace(old_index, new_index, 1)
    changes += 1
    print(f'[{changes}] / now serves landing page, /app serves SPA')

    # --- 3. /login → /sign-in + redirect ---
    old_login = (
        '@app.get("/login", response_class=HTMLResponse)\n'
        'async def login_page():\n'
        '    return HTMLResponse(_AUTH_PAGE_HTML.replace("__MODE__", "login"))'
    )
    new_login = (
        '@app.get("/sign-in", response_class=HTMLResponse)\n'
        'async def signin_page():\n'
        '    return HTMLResponse(_AUTH_PAGE_HTML.replace("__MODE__", "login"))\n'
        '\n'
        '\n'
        '@app.get("/login")\n'
        'async def login_redirect():\n'
        '    from starlette.responses import RedirectResponse\n'
        '    return RedirectResponse(url="/sign-in", status_code=301)'
    )
    if old_login not in src:
        print('FATAL: login route not found')
        sys.exit(1)
    src = src.replace(old_login, new_login, 1)
    changes += 1
    print(f'[{changes}] /login → /sign-in + 301 redirect')

    # --- 4. /signup → redirect ---
    old_signup = (
        '@app.get("/signup", response_class=HTMLResponse)\n'
        'async def signup_page():\n'
        '    return HTMLResponse(_AUTH_PAGE_HTML.replace("__MODE__", "signup"))'
    )
    new_signup = (
        '@app.get("/signup")\n'
        'async def signup_redirect():\n'
        '    from starlette.responses import RedirectResponse\n'
        '    return RedirectResponse(url="/sign-in", status_code=301)'
    )
    if old_signup not in src:
        print('FATAL: signup route not found')
        sys.exit(1)
    src = src.replace(old_signup, new_signup, 1)
    changes += 1
    print(f'[{changes}] /signup → 301 redirect to /sign-in')

    # --- 5. JS: /login → /sign-in (all occurrences) ---
    n = src.count("BASE + '/login'")
    src = src.replace("BASE + '/login'", "BASE + '/sign-in'")
    changes += 1
    print(f'[{changes}] Updated {n} JS references: /login → /sign-in')

    # --- 6. JS: login success redirect / → /app ---
    n = src.count("window.location.href = BASE + '/';")
    src = src.replace(
        "window.location.href = BASE + '/';",
        "window.location.href = BASE + '/app';"
    )
    changes += 1
    print(f'[{changes}] Updated {n} login success redirect(s) to /app')

    # --- 7. Hide signup toggle in auth page ---
    old_switch = '<div class="switch" id="switchLink"></div>'
    new_switch = '<div class="switch" id="switchLink" style="display:none"></div>'
    if old_switch in src:
        src = src.replace(old_switch, new_switch, 1)
        changes += 1
        print(f'[{changes}] Hidden signup toggle in auth page')
    else:
        print('WARN: switchLink div not found (may already be hidden)')

    # --- Write ---
    with open(APP, 'w') as f:
        f.write(src)

    delta = len(src) - original_len
    print(f'\nDone! {changes} changes, +{delta} bytes')
    print(f'Now run: sudo supervisorctl restart simple-genomics')


if __name__ == '__main__':
    main()
