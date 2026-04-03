# Rotem.cc V2 — Comprehensive Genomics Analysis Wishlist

> **Purpose**: A detailed, organized catalog of everything interesting we can analyze from whole-genome sequencing data for the V2 rebuild of rotem.cc. Covers what V1 already does, what's missing, and creative new analyses to add.
>
> **Created**: 2026-03-29

---

## Table of Contents

1. [V1 Current Coverage Summary](#1-v1-current-coverage-summary)
2. [Polygenic Scores (PGS) — Health Conditions](#2-polygenic-scores--health-conditions)
3. [Polygenic Scores (PGS) — Traits & Behavioral](#3-polygenic-scores--traits--behavioral)
4. [Monogenic Disease Screening](#4-monogenic-disease-screening)
5. [Carrier Status (Recessive Diseases)](#5-carrier-status-recessive-diseases)
6. [Pharmacogenomics (PGx)](#6-pharmacogenomics-pgx)
7. [Single-Variant Health Risk Markers](#7-single-variant-health-risk-markers)
8. [Fun & Interesting Trait Variants](#8-fun--interesting-trait-variants)
9. [Ancestry & Population Genetics](#9-ancestry--population-genetics)
10. [Advanced / Creative Analyses](#10-advanced--creative-analyses)
11. [Databases & Tools Reference](#11-databases--tools-reference)

---

## 1. V1 Current Coverage Summary

V1 (rotem.cc) runs **Pipeline E+ (Direct BAM Genotyping)** across 6 family members (3 EUR, 1 EAS, 2 MIXED). Current coverage:

| Category | V1 Status |
|----------|-----------|
| **PGS Scores** | 704 total across 122 traits (435 via Pipeline E+) |
| **Monogenic Screening** | 378 ClinVar genes, 6 pathogenic variants found |
| **Pharmacogenomics** | CYP2D6 (with CNV), CYP2C19, RYR1, CYP2B6, OPRM1, COMT, HTR2A |
| **APOE Typing** | Full epsilon2/3/4 genotyping |
| **Cancer PGS** | Breast (133 PGS), Prostate (93), Colorectal (1), Melanoma (5), Basal Cell (15) |
| **Cardiovascular PGS** | CAD (7), AF (61), Hypertension (21), Heart Failure (15), VTE (2), AAA (1), BP (13) |
| **Metabolic PGS** | T2D (3), BMI (5), HDL (17), LDL (3), Triglycerides (4), HbA1c (8), Glucose (5) |
| **Neuro/Psych PGS** | Alzheimer's (52), Schizophrenia (1), Depression (2), ADHD (2), Bipolar (1), Parkinson's (2), Anxiety (1), Intelligence (6) |
| **Behavioral PGS** | Education (11), Household Income (2) |
| **Other** | Epilepsy (2), Glaucoma (1), Insomnia (2), Autism (1), Baldness (1) |

### What V1 is Missing (V2 Opportunities)

- No carrier status panel (CF, sickle cell, Tay-Sachs, etc.)
- Limited pharmacogenomics (missing CYP2C9, VKORC1, DPYD, TPMT, SLCO1B1, HLA, G6PD, NAT2)
- No ancestry/admixture analysis
- No haplogroup determination
- No Neanderthal/Denisovan ancestry
- No fun trait variants (taste, earwax, eye color, etc.)
- No nutrigenomics
- No sleep/circadian genetics
- No sports/fitness genetics
- No blood type prediction
- No HLA typing
- Missing many PGS trait categories (see below)

---

## 2. Polygenic Scores — Health Conditions

### 2A. Cancer

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Breast Cancer** | PGS000004 (PRS313) | 313 | Mavaddat 2018, AJHG | EUR (158K) | AUROC 0.63 | Yes (133 PGS) | The clinical gold standard; validated across 59 cohorts |
| **Breast Cancer** | PGS000001 (PRS77) | 77 | Mavaddat 2015, JNCI | EUR (22.6K) | OR 1.55 | Yes | First PGS Catalog entry ever — historically significant |
| **Prostate Cancer** | PGS000067 (PHS) | 54 | Seibert 2018, BMJ | EUR/multi | HR 2.9 (top 2%) | Yes (93 PGS) | Best for screening decisions; cross-ancestry validated |
| **Prostate Cancer** | PGS000044 | 66 | Pashayan 2015, BJC | EUR | OR 1.56 | Yes | Compact clinical score |
| **Colorectal Cancer** | PGS004580 | 1,099,906 | Youssef 2024, Lab Invest | EUR (342K Finnish) | OR 1.50/SD | Partial (1 PGS) | **V2: Add genome-wide CRC scores** |
| **Lung Cancer** | PGS000070 | 19 | Dai 2019, Lancet Respir | EUR+EAS (54K) | AUROC 0.73 | No | **V2: NEW** — compact score, balanced ancestry |
| **Melanoma** | existing V1 scores | 5 PGS | — | — | — | Yes (5 PGS) | Adequate |
| **Basal Cell Carcinoma** | existing V1 scores | 15 PGS | — | — | — | Yes (15 PGS) | Adequate |
| **Ovarian Cancer** | PGS000292+ | Various | Multiple | EUR | Various | No | **V2: NEW** — important alongside BRCA status |
| **Pancreatic Cancer** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — rising incidence, poor prognosis |
| **Bladder Cancer** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Kidney Cancer** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Thyroid Cancer** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Testicular Cancer** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — highly heritable cancer |

### 2B. Cardiovascular

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Coronary Artery Disease** | PGS000018 (metaGRS) | 1,745,179 | Inouye 2018, JACC | Multi (382K) | AUROC 0.79 | Yes (7 PGS) | Best-performing CAD score |
| **CAD** | PGS000013 (GPS) | 6,630,150 | Khera 2018, Nat Genet | EUR (120K UKB) | AUROC 0.81 | Yes | Landmark Khera study |
| **CAD Multi-ancestry** | PGS003725 | 1,296,172 | Patel 2023, Nat Med | Multi | HR 1.75 (EUR) | No | **V2: ADD** — best multi-ancestry CAD score |
| **Atrial Fibrillation** | PGS000016 + others | 6.7M+ | Khera 2018, Nat Genet | Multi | AUROC 0.78 | Yes (61 PGS) | Excellent coverage |
| **Heart Failure** | existing V1 scores | 15 PGS | — | — | — | Yes | Adequate |
| **Hypertension** | existing V1 scores | 21 PGS | — | — | — | Yes | Adequate |
| **Stroke (Ischemic)** | PGS000665 | 32 | Marston 2020, Circulation | Multi (524K) | C-index 0.65 | No | **V2: NEW** — compact, high-impact |
| **Venous Thromboembolism** | existing V1 scores | 2 PGS | — | — | — | Yes | Could expand |
| **Aortic Aneurysm** | existing V1 scores | 1 PGS | — | — | — | Yes | Adequate |
| **Peripheral Artery Disease** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

### 2C. Metabolic & Endocrine

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Type 2 Diabetes** | PGS000014 (GPS) | 6,917,436 | Khera 2018, Nat Genet | EUR | AUROC 0.73 | Yes (3 PGS) | Could expand with more scores |
| **BMI / Obesity** | PGS000027 (GPS) | 2,100,302 | Khera 2019, Cell | EUR (120K UKB) | R²=0.085 | Yes (5 PGS) | Top decile = 13kg heavier average |
| **Lipids (HDL/LDL/TG)** | existing V1 scores | 24 PGS | — | — | — | Yes | Good coverage |
| **HbA1c / Glucose** | existing V1 scores | 13 PGS | — | — | — | Yes | Good coverage |
| **Type 1 Diabetes** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — strong HLA component |
| **Gout** | PGS Catalog entries | Various | Multiple | Multi | Various | No | **V2: NEW** — urate transporter genetics |
| **NAFLD/NASH** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — PNPLA3 already in V1 monogenic |
| **Thyroid Disease** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — hypothyroidism very common |
| **Celiac Disease** | PGS000040 | 228 | Abraham 2014, PLoS Genet | EUR (6.7K) | **AUROC 0.90** | No | **V2: NEW** — one of the best-performing PGS in existence |
| **Osteoporosis / BMD** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — bone mineral density scores |

### 2D. Autoimmune & Inflammatory

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Inflammatory Bowel Disease** | PGS004081 | 1,073,268 | Monti 2024, AJHG | Multi | AUROC 0.68 | No | **V2: NEW** — good cross-ancestry portability |
| **Crohn's Disease** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — subset of IBD |
| **Ulcerative Colitis** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — subset of IBD |
| **Rheumatoid Arthritis** | PGS002745 | 2,575 | Ishigaki 2022, Nat Genet | Multi (276K) | AUROC 0.66 | No | **V2: NEW** — consistent across EUR/EAS |
| **Multiple Sclerosis** | PGS002038 | 129,077 | Prive 2022, AJHG | EUR (391K UKB) | r=0.045 | No | **V2: NEW** — strong HLA component |
| **Lupus (SLE)** | PGS Catalog entries | Various | Multiple | Multi | Various | No | **V2: NEW** |
| **Psoriasis** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Asthma** | PGS002311 | 1,109,311 | Weissbrod 2022, Nat Genet | EUR (337K UKB) | R²=0.024 | No | **V2: NEW** |
| **Allergic Rhinitis** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Eczema / Atopic Dermatitis** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

### 2E. Neurological & Psychiatric

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Alzheimer's Disease** | PGS000812 + 51 others | 57-1.1M | Multiple | EUR | HR 1.15+ | Yes (52 PGS) | Excellent — most comprehensive coverage |
| **Parkinson's Disease** | PGS000123 + PGS Mega | 16-1.7M | Ibanez 2017+ | EUR | Significant | Yes (2 PGS) | Could expand |
| **Schizophrenia** | PGS000135 | 972,439 | Zheutlin 2019, AJP | EUR/EAS (82K) | AUROC 0.74 | Yes (1 PGS) | Best psychiatric PGS result |
| **Major Depression** | PGS000193 | 1,138 | Coleman 2020, Mol Psych | EUR (431K) | OR 1.18 | Yes (2 PGS) | Modest but significant |
| **Bipolar Disorder** | existing V1 score | 1 PGS | — | — | — | Yes | Could expand |
| **ADHD** | PGS002746+ | Various | PGC | EUR | R²~0.05 | Yes (2 PGS) | Heritable (h²=0.74) |
| **Autism Spectrum** | PGS000327 | 35,087 | Grove 2019, Nat Genet | EUR (46K) | OR 1.33 | Yes (1 PGS) | R²=0.025 |
| **Anxiety Disorders** | existing V1 score | 1 PGS | — | — | — | Yes | Could expand |
| **OCD** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **PTSD** | PGS Catalog entries | Various | Multiple | Multi | Various | No | **V2: NEW** |
| **Eating Disorders (Anorexia)** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — metabolic-psychiatric cross-trait |
| **Migraine** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — very common, heritable |
| **Epilepsy** | PGS002760 | 835,530 | — | — | — | Yes (2 PGS) | Adequate |
| **ALS** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

### 2F. Other Medical

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Chronic Kidney Disease** | PGS Catalog entries | Various | Multiple | Multi | Various | No | **V2: NEW** |
| **Age-related Macular Degeneration** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** — strong genetic component |
| **Glaucoma** | PGS002761 | 1,082,518 | — | — | — | Yes (1 PGS) | Could expand (4 available) |
| **Endometriosis** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Uterine Fibroids** | PGS Catalog entries | Various | Multiple | Multi | Various | No | **V2: NEW** |
| **Polycystic Ovary Syndrome** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Kidney Stones** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Gallstones** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

---

## 3. Polygenic Scores — Traits & Behavioral

### 3A. Physical Traits

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Height** | PGS000297 | 3,290 | Xie 2020, Circ GMP | EUR (693K) | R²=0.138 | Yes (2 PGS) | Could expand |
| **Hair Color** | PGS002598 | 8,312 | Weissbrod 2022, Nat Genet | EUR (332K UKB) | R²=0.182 | No | **V2: NEW** — strong prediction in EUR |
| **Skin Pigmentation** | PGS001897 | 15,817 | Prive 2022, AJHG | EUR (391K UKB) | r=0.387 | No | **V2: NEW** — remarkable EUR prediction |
| **Eye Color** | IrisPlex (6 SNPs) | 6 | Walsh 2011, FSI:Genetics | Multi | >90% accuracy (blue/brown) | No | **V2: NEW** — highly accurate prediction |
| **Baldness / Hair Loss** | PGS001784 + PGS002314 | 911K-1.1M | Multiple | EUR | AUC ~0.75 | Yes (1 PGS) | Could expand |
| **Body Fat %** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Waist-Hip Ratio** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

### 3B. Behavioral & Cognitive

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Educational Attainment** | PGS002012+ | 50K-1.1M | Lee 2018/Okbay 2022 | EUR (>1M) | R²~0.13 | Yes (11 PGS) | Top behavioral PGS |
| **Intelligence / Cognition** | PGS003724 | 6,683,248 | Hatoum 2022, Biol Psych | EUR (216K UKB) | R²=0.121 | Yes (6 PGS) | Good coverage |
| **Household Income** | existing V1 scores | 2 PGS | — | — | — | Yes | Adequate |
| **Risk Tolerance** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Neuroticism** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Subjective Well-being** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Loneliness** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |

### 3C. Lifestyle & Consumption

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Coffee Consumption** | PGS001123 | 48 | Tanigawa 2022, PLoS Genet | EUR (116K UKB) | AUROC 0.60 | No | **V2: NEW** — CYP1A2/AHR region |
| **Alcohol Consumption** | PGS002752 | 1,089,551 | Mars 2022, AJHG | EUR (941K) | OR 1.19/SD | No | **V2: NEW** — ADH/ALDH clusters |
| **Smoking Initiation** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Chronotype** | PGS002586 | 255 | Weissbrod 2022, Nat Genet | EUR (301K) | R²=0.004 | No | **V2: NEW** — morning vs. evening person |
| **Sleep Duration** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Insomnia** | PGS000908 + PGS003322 | 2.7M-8.6M | Multiple | EUR | Various | Yes (2 PGS) | Adequate |

### 3D. Longevity & Aging

| Trait | Recommended PGS | Variants | Study | Population | Performance | V1? | Notes |
|-------|-----------------|----------|-------|------------|-------------|-----|-------|
| **Longevity** | PGS000906 | 330 | Tesi 2021, J Gerontol | EUR (500K) | HR 0.89/SD | No | **V2: NEW** — 11% lower mortality per SD |
| **Healthspan** | PGS Catalog entries | Various | Multiple | EUR | Various | No | **V2: NEW** |
| **Telomere Length (genetic)** | PGS Catalog entries | Various | 303 SNPs from 20 GWAS | EUR | Various | No | **V2: NEW** — genetic predisposition to longer/shorter telomeres |

---

## 4. Monogenic Disease Screening

V1 screens 378 ClinVar genes. V2 should expand and add structured reporting for additional categories.

### Currently Detected in V1 (Family)

| Gene | Variant | Condition | Carriers | Severity |
|------|---------|-----------|----------|----------|
| **RYR1** | rs200563280 (p.Arg2241Ter) | Malignant Hyperthermia | Nimo, Efi, B2, B3 | **CRITICAL** — anesthesia alert |
| **BRCA2** | chr13:32,380,036 C>A | Breast/Ovarian/Prostate Cancer | B2 | **HIGH** — expert-panel pathogenic |
| **FBN1** | chr15:48,492,539 A>G | Marfan Syndrome / Aortic Disease | Mina, B3 | **HIGH** — cardiac monitoring needed |
| **F11** | variant detected | Factor XI Deficiency (Bleeding) | Nimo, Mina | Moderate |
| **PRSS1** | variant detected | Hereditary Pancreatitis | Efi, B3 | Moderate |

### V2 Should Add Structured Screening For

| Category | Genes | What It Catches |
|----------|-------|-----------------|
| **Cardiomyopathy panel** | MYH7, MYBPC3, TNNT2, TNNI3, LMNA, TTN, DSP, PKP2 | Hypertrophic & dilated cardiomyopathy, arrhythmogenic RV cardiomyopathy |
| **Arrhythmia panel** | SCN5A, KCNQ1, KCNH2, KCNJ2, RYR2 | Long QT syndrome, Brugada syndrome, CPVT |
| **Hereditary cancer panel** | BRCA1, BRCA2, TP53, PALB2, CHEK2, ATM, RAD51C/D, CDH1, STK11, PTEN | Breast, ovarian, pancreatic, gastric, colorectal cancer syndromes |
| **Lynch syndrome** | MLH1, MSH2, MSH6, PMS2, EPCAM | Hereditary nonpolyposis colorectal cancer + endometrial |
| **Connective tissue** | FBN1, FBN2, TGFBR1, TGFBR2, COL3A1, SMAD3 | Marfan, Loeys-Dietz, vascular Ehlers-Danlos |
| **Neurodegenerative** | SNCA, LRRK2, GBA, PARK7, PINK1, PRKN, VPS35, C9orf72, SOD1, FUS | Parkinson's, ALS, frontotemporal dementia |
| **Aortopathy** | ACTA2, MYH11, SMAD3, TGFBR1/2, FBN1, LOX, PRKG1 | Familial thoracic aortic aneurysm/dissection |

---

## 5. Carrier Status (Recessive Diseases)

**Entirely new for V2.** These are heterozygous carrier screens — the individual is unaffected but could pass a disease allele to offspring.

### High-Priority Carrier Panel

| Disease | Gene | Key Variant(s) | rs Number | Carrier Freq | Population | Notes |
|---------|------|----------------|-----------|--------------|------------|-------|
| **Cystic Fibrosis** | CFTR | F508del (most common, ~70% of alleles) | rs113993960 | 1 in 25 | Northern EUR | Most common lethal AR disease in Europeans |
| **Sickle Cell Disease** | HBB | Glu6Val (HbS) | rs334 | 1 in 13 | African Americans | Carriers have partial malaria resistance |
| **Tay-Sachs Disease** | HEXA | 1278insTATC, IVS12+1G>C, G269S | Various | 1 in 30 | Ashkenazi, Cajun, French Canadian | Progressive neurodegeneration |
| **Gaucher Disease** | GBA | N370S (Type 1), L444P (severe) | rs76763715, rs421016 | 1 in 15 | Ashkenazi | Also a Parkinson's risk factor in carriers |
| **Phenylketonuria (PKU)** | PAH | R408W (most common in Slavic) | rs5030858 | 1 in 50 | European | Treatable with diet if caught at birth |
| **Beta-Thalassemia** | HBB | Codon 39 C>T, IVS-I-110, IVS-I-5 | rs33930165, rs33950507, rs11549407 | 5-30% | Mediterranean, ME, S/SE Asian | Severe anemia, transfusion-dependent |
| **Familial Mediterranean Fever** | MEFV | M694V, M680I, V726A | rs61752717, rs28940579, rs28940580 | 1 in 3-5 | Turkish, Armenian, Arab, Sephardic | Recurrent fever + serositis; amyloidosis risk |
| **Hereditary Hemochromatosis** | HFE | C282Y, H63D | rs1800562, rs1799945 | C282Y: 1 in 9 | Northern EUR | Iron overload; 30-50% penetrance for C282Y/C282Y |
| **Bloom Syndrome** | BLM | blmAsh (2281del6ins7) | — | 1 in 100 | Ashkenazi | Growth retardation, cancer predisposition |
| **Canavan Disease** | ASPA | E285A, Y231X | rs28940279 | 1 in 40 | Ashkenazi | Leukodystrophy, fatal in childhood |
| **Niemann-Pick Type A** | SMPD1 | L302P, fsP330 | rs120074118 | 1 in 90 | Ashkenazi | Severe neurodegeneration |
| **Fanconi Anemia (Type C)** | FANCC | IVS4+4A>T | — | 1 in 89 | Ashkenazi | Bone marrow failure, cancer predisposition |
| **Wilson Disease** | ATP7B | H1069Q (most common in EUR) | rs76151636 | 1 in 90 | European | Copper overload — liver + brain damage |
| **Maple Syrup Urine Disease** | BCKDHA/B/DBT | Various | Various | Rare (1 in 100 Mennonite) | Mennonite, general | Metabolic crisis from branched-chain amino acids |
| **Spinal Muscular Atrophy** | SMN1 | Exon 7 deletion (most common) | — | 1 in 40-50 | Pan-ethnic | Leading genetic cause of infant death; treatable with gene therapy |
| **Fragile X Syndrome** | FMR1 | CGG repeat expansion | — | 1 in 250 females | Pan-ethnic | Leading inherited cause of intellectual disability |
| **Congenital Adrenal Hyperplasia** | CYP21A2 | Various | Various | 1 in 60 | Pan-ethnic | Cortisol deficiency, virilization |

---

## 6. Pharmacogenomics (PGx)

### V1 Current PGx Coverage

| Gene | V1 Status | Key Findings |
|------|-----------|--------------|
| CYP2D6 (with CNV) | **Done** | B2XH ultrarapid, B3XH poor metabolizer, others intermediate |
| CYP2C19 | **Done** | All *1/*1 (normal) |
| RYR1 | **Done** | 4 carriers of pathogenic variant — anesthesia alert |
| CYP2B6 | **Done** | Chi *9/*9 (poor) — tramadol seizure risk |
| OPRM1 | **Done** | Chi GG (reduced opioid receptor binding) |
| COMT | **Done** | Nimo Val/Met, others Val/Val |
| HTR2A | **Done** | Efi/Chi/B3 TT (altered SSRI response) |

### V2 Must-Add PGx Genes

| Gene | Variants/Alleles | Key Drugs | Clinical Impact | CPIC Level | Testable from WGS? |
|------|-----------------|-----------|-----------------|------------|---------------------|
| **CYP2C9** | *2 (rs1799853), *3 (rs1057910), *5, *6, *8, *11 | Warfarin, NSAIDs (celecoxib, ibuprofen), phenytoin, losartan, siponimod | Warfarin dose (with VKORC1); NSAID GI bleeding risk; phenytoin toxicity | Level A | Yes — all SNPs callable |
| **VKORC1** | rs9923231 (-1639G>A) | Warfarin | A/A = ~50% lower dose needed; accounts for ~25% of warfarin dose variability | Level A | Yes |
| **CYP4F2** | *3 (rs2108622) | Warfarin (minor modifier) | Increases warfarin requirement slightly | Level A | Yes |
| **DPYD** | *2A (rs3918290), *13 (rs55886062), c.2846A>T (rs67376798), HapB3 (rs75017182) | 5-FU, capecitabine, tegafur (fluoropyrimidine chemo) | DPD deficiency in 3-7% of EUR; complete deficiency = **fatal** with 5-FU. EU mandates pre-treatment testing | Level A | Yes — critical for cancer patients |
| **TPMT** | *2 (rs1800462), *3A (rs1800460+rs1142345), *3C (rs1142345) | Azathioprine, 6-MP, thioguanine (IBD, leukemia, autoimmune) | PM = severe myelosuppression (pancytopenia); reduce dose 90% or avoid | Level A | Yes |
| **NUDT15** | *3 (rs116855232, p.R139C) | Same thiopurines as TPMT | Especially important in East Asians (~10% carrier freq); complements TPMT | Level A | Yes |
| **UGT1A1** | *28 (rs8175347, TA repeat), *6 (rs4148323) | Irinotecan (cancer), atazanavir (HIV) | *28/*28 = 70% reduced glucuronidation → severe neutropenia with irinotecan | Level A | Yes (TA repeat from WGS reads) |
| **HLA-B*57:01** | Presence/absence | Abacavir (HIV) | 50% risk of fatal hypersensitivity in carriers. Testing is **global standard of care**. NPV ~100% | Level A | Yes — HLA typing from WGS |
| **HLA-B*15:02** | Presence/absence | Carbamazepine, oxcarbazepine (antiepileptics) | Stevens-Johnson syndrome / TEN (life-threatening). FDA mandates testing in SE Asian ancestry | Level A | Yes |
| **HLA-A*31:01** | Presence/absence | Carbamazepine | Broader hypersensitivity (MPE, DRESS, SJS/TEN) across all ethnicities including EUR and Japanese | Level A | Yes |
| **SLCO1B1** | *5 (rs4149056), *14 (rs2306283) | All statins (esp. simvastatin) | *5/*5 = 17x myopathy risk with simvastatin. Avoid simvastatin in homozygotes | Level A | Yes |
| **ABCG2** | rs2231142 (421C>A) | Rosuvastatin | Reduced hepatic uptake → increased statin exposure and myopathy risk | Level A | Yes |
| **CYP3A5** | *3 (rs776746) | Tacrolimus (transplant immunosuppression) | Expressers (*1 carriers) need higher doses (0.3 vs 0.15 mg/kg/day) to prevent rejection | Level A | Yes |
| **G6PD** | A- (rs1050828+rs1050829), Mediterranean (rs5030868), Canton, Mahidol | Rasburicase (CONTRAINDICATED), primaquine, tafenoquine, dapsone, methylene blue, nitrofurantoin + 42 more drugs | Deficiency affects ~400M worldwide. Rasburicase = fatal hemolytic anemia | Level A | Yes — common variants; rare ones need careful calling |
| **IFNL3 (IL28B)** | rs12979860 (C/T), rs8099917 | PEG-IFN + ribavirin (Hep C) | CC = 2x SVR rate. Less relevant with DAAs but still useful | Level A | Yes |
| **NAT2** | *5 (rs1801280), *6 (rs1799930), *7 (rs1799931), multiple sub-alleles | Hydralazine (2025 CPIC guideline), isoniazid (TB), sulfasalazine, procainamide | Slow acetylators (50% of population): better hydralazine efficacy but DISLE risk; isoniazid hepatotoxicity | Level A | Yes |
| **CYP2B6** | (Already in V1 but expand) *4, *6 (rs3745274), *9, *18 | Efavirenz (HIV), methadone, bupropion, cyclophosphamide | PM (*6/*6) = 3-4x efavirenz levels → CNS toxicity. For methadone: primary metabolic enzyme | Level A | Yes |

### PGx Summary Statistics

- CPIC guidelines as of 2025: **34 genes, 164 drugs**, 28 active guidelines
- PREPARE trial (Lancet 2023): 12-gene panel across 7 EU countries → **30% reduction in adverse drug reactions**
- V1 covers ~4 of 17 key pharmacogenes. **V2 should cover all 17+**

---

## 7. Single-Variant Health Risk Markers

These are individual SNPs or small variant sets with large, clinically significant effects.

### Already in V1

| Marker | Gene | rs Number | V1 Status | Family Results |
|--------|------|-----------|-----------|----------------|
| APOE genotype | APOE | rs429358 + rs7412 | Done | Nimo: E3/E3, Mina: E3/E4, Efi: E2/E3, Chi: E3/E4, B2: E2/E4, B3: E2/E3 |
| COMT Val/Met | COMT | rs4680 | Done | Nimo Val/Met, others Val/Val |
| BDNF Val66Met | BDNF | rs6265 | Done | All Val/Val |
| PNPLA3 (NAFLD) | PNPLA3 | rs738409 | Done | Nimo C/G (moderate NAFLD risk) |
| Factor V Leiden | F5 | rs6025 | Checked (WT) | All wild-type |
| HFE C282Y/H63D | HFE | rs1800562 / rs1799945 | Checked (WT) | All wild-type |

### V2 Should Add

| Marker | Gene | rs Number | What It Means | Population Frequency |
|--------|------|-----------|---------------|----------------------|
| **Prothrombin G20210A** | F2 | rs1799963 | 2.8x VTE risk (heterozygous); compounds with Factor V Leiden and OCP use | 2-3% EUR |
| **MTHFR C677T** | MTHFR | rs1801133 | TT = 30% enzyme activity; elevated homocysteine; neural tube defect risk with low folate | 10-15% EUR (TT), 25% Hispanic |
| **MTHFR A1298C** | MTHFR | rs1801131 | Milder reduction; compound heterozygosity (677CT + 1298AC) also significant | 7-12% EUR (CC) |
| **Alpha-1 Antitrypsin Z** | SERPINA1 | rs28929474 | ZZ = 15% normal AAT → emphysema (esp. smokers) + liver disease | Z allele: 2-3% N.EUR |
| **Alpha-1 Antitrypsin S** | SERPINA1 | rs17580 | Moderate reduction; SZ compound = intermediate risk | S allele: 5-10% S.EUR |
| **PCSK9 R46L** | PCSK9 | rs11591147 | **Protective** loss-of-function: ~50% lower LDL, ~90% lower CHD risk | 2-3% EUR |
| **APOB R3527Q** | APOB | rs5742904 | Familial hypercholesterolemia — severely elevated LDL from birth | Rare (~1 in 1000) |
| **FTO obesity risk** | FTO | rs9939609 | AA = 1.67x obesity risk, ~3kg heavier; affects satiety signaling | A allele: 42% EUR |
| **TCF7L2 diabetes** | TCF7L2 | rs7903146 | Strongest common T2D variant: TT = 1.8x risk. Affects insulin secretion | T allele: 30% EUR |
| **9p21 CAD locus** | CDKN2A/B region | rs10757278 | GG = 1.6x MI risk. The first replicated CAD GWAS hit | G allele: 49% EUR |
| **LRRK2 G2019S** | LRRK2 | rs34637584 | Most common genetic cause of Parkinson's. 28-74% lifetime PD risk (dominant) | 1-2% Ashkenazi; 30-40% of North African Arab PD |
| **GBA N370S (Parkinson's)** | GBA | rs76763715 | Heterozygous carriers: 5-10x Parkinson's risk (even without Gaucher disease) | 1 in 15 Ashkenazi |
| **PALB2** | PALB2 | Various | Breast cancer risk gene — 2-4x risk. On ACMG actionable gene list | ~1 in 1000 |
| **CHEK2 1100delC** | CHEK2 | rs555607708 | Moderate breast cancer risk (~2x). Very common in Northern Europeans | 0.5-1.5% N.EUR |

---

## 8. Fun & Interesting Trait Variants

**Entirely new for V2.** These make the report engaging and personally relatable.

### Taste & Smell

| Trait | Gene | rs Number(s) | Genotype → Phenotype | Inheritance | Freq |
|-------|------|-------------|----------------------|-------------|------|
| **Bitter taste (PTC/PROP)** | TAS2R38 | rs713598, rs1726866, rs10246939 | PAV/PAV = supertaster (Brussels sprouts taste terrible); AVI/AVI = non-taster | Co-dominant | 25% non-tasters (EUR) |
| **Cilantro soapy taste** | OR6A2 | rs72921001 | A allele = more likely to perceive cilantro as soapy; OR6A2 binds the aldehydes in cilantro | Additive | 4-14% EUR dislike cilantro; 21% EAS |
| **Asparagus urine smell** | OR2M7 | rs4481887 | A allele = can detect the sulfurous metabolites in urine after eating asparagus | Complex | 40-60% can smell it |

### Body & Appearance

| Trait | Gene | rs Number(s) | Genotype → Phenotype | Inheritance | Freq |
|-------|------|-------------|----------------------|-------------|------|
| **Earwax type & body odor** | ABCC11 | rs17822931 | AA = dry/flaky earwax + reduced body odor (no deodorant needed); GG/GA = wet/sticky earwax | Recessive (dry) | AA: 80-95% EAS, 1-3% EUR |
| **Red hair** | MC1R | rs1805007 (R151C), rs1805008 (R160W) | Two MC1R variants = full red hair; one = reddish tints + increased sun/pain sensitivity | Recessive for red hair | 6-10% Northern EUR |
| **Eye color** | HERC2/OCA2 | rs12913832 | AA = blue/grey eyes; GG = brown; AG = green/hazel. Controls OCA2 enhancer | Largely recessive for blue | AA: 70-80% Scandinavian, <5% EAS |
| **Cleft chin** | multiple loci | rs11684042, rs7552331 | Minor alleles associated with chin cleft formation | Complex | Variable |
| **Freckling** | MC1R + IRF4 | rs1805007 + rs12203592 | MC1R variants + IRF4 T allele = heavy freckling and sun sensitivity | Additive | Variable |

### Metabolism & Diet

| Trait | Gene | rs Number(s) | Genotype → Phenotype | Inheritance | Freq |
|-------|------|-------------|----------------------|-------------|------|
| **Lactose tolerance** | MCM6/LCT | rs4988235 | TT/TC = lactase persistent (can drink milk); CC = lactose intolerant (lose lactase after weaning) | Dominant (T) | T: 90-95% N.EUR, 5-20% EAS |
| **Alcohol flush reaction** | ALDH2 | rs671 | AA = near-zero ALDH2 activity, cannot drink; GA = flushing + nausea; also esophageal cancer risk if drinking | Co-dominant | 30-40% EAS; absent in EUR/AFR |
| **Caffeine metabolism** | CYP1A2 | rs762551 | AA = fast metabolizer (coffee is protective); CC/AC = slow (hypertension risk with heavy coffee) | Recessive (fast) | ~45-50% AA |
| **Norovirus resistance** | FUT2 | rs601338 | AA = non-secretor = strong norovirus resistance + reduced rotavirus/H. pylori. Trade-off: slightly higher Crohn's risk | Recessive | 20% EUR, 5% EAS, 25-35% AFR |

### Athletic & Physical

| Trait | Gene | rs Number(s) | Genotype → Phenotype | Inheritance | Freq |
|-------|------|-------------|----------------------|-------------|------|
| **Sprint vs. endurance** | ACTN3 | rs1815739 (R577X) | CC (RR) = fast-twitch muscle, sprint/power; TT (XX) = no alpha-actinin-3, endurance-adapted. Elite sprinters almost never XX | Recessive (XX) | XX: 18% EUR, 25% EAS, 1-3% AFR |
| **Warrior vs. worrier** | COMT | rs4680 (Val158Met) | GG = warrior (fast dopamine breakdown, stress-resilient); AA = worrier (higher prefrontal dopamine, better focus but stress-vulnerable) | Co-dominant | ~25% GG, ~50% AG, ~25% AA (EUR) |
| **ACE endurance** | ACE | I/D polymorphism | II = endurance athlete enriched; DD = power/strength enriched | Additive | Variable |

### Curiosities

| Trait | Gene | rs Number(s) | Genotype → Phenotype | Inheritance | Freq |
|-------|------|-------------|----------------------|-------------|------|
| **Photic sneeze reflex** | ZEB2/NR2F2 | rs10427255, rs11856995 | Sneezing when exposed to bright light (ACHOO syndrome). Cross-wiring of optic + trigeminal nerves | Autosomal dominant | 18-35% of people |
| **Short sleep gene** | DEC2/BHLHE41 | rs121912617 (P385R) | 4-6.5 hrs sleep without impairment. High pain threshold, high drive | Autosomal dominant | Extremely rare (<1% of short sleepers) |
| **Morning/evening person** | CLOCK | rs1801260 | CC = night owl tendency, delayed sleep; TT = morning person | Additive | C allele: 25-30% EUR |
| **Blood type (ABO)** | ABO | rs7853989, rs8176722, rs8176746 | Predicts A/B/AB/O blood type from DNA | Co-dominant | Universal |
| **Rh factor** | RHD | rs590787 + related | Predicts Rh+ vs Rh- | Recessive (Rh-) | Rh-: 15% EUR, <1% EAS |

---

## 9. Ancestry & Population Genetics

**Entirely new for V2.**

### 9A. Admixture / Global Ancestry

| Analysis | Method | Tools | Feasibility | Notes |
|----------|--------|-------|-------------|-------|
| **Continental admixture** | Maximum likelihood decomposition into K ancestral components | ADMIXTURE software, DNAGENICS Global K23 | Excellent from WGS | Decompose genome into EUR/AFR/EAS/SAS/AMR/OCE proportions |
| **Sub-continental ancestry** | Regional reference panels | G25 coordinates (DNAGENICS), 100+ calculators | Excellent | e.g. distinguish Northern vs Southern European, Levantine vs Arabian Peninsula |
| **PCA visualization** | Principal Component Analysis | PLINK, smartpca, G25 Studio | Excellent | Plot samples against 1000 Genomes or HGDP reference panels |
| **Jewish ancestry breakdown** | Specialized reference panels | DNAGENICS Jewish calculators, JewishGen DNA | Good for Ashkenazi | Distinguish Ashkenazi, Sephardi, Mizrahi, Yemenite components |

### 9B. Haplogroups

| Analysis | What It Traces | Method | Tools | Notes |
|----------|---------------|--------|-------|-------|
| **Y-DNA haplogroup** | Strict paternal lineage (father's father's father...) | Y-chromosome SNP calling | yhaplo (23andMe open-source), YSEQ Clade Finder | WGS has excellent Y coverage; can resolve to deep sub-clades |
| **mtDNA haplogroup** | Strict maternal lineage (mother's mother's mother...) | Mitochondrial variant calling | HaploGrep, mtDNA-Server | WGS captures full mitochondrial genome; complete resolution |

### 9C. Archaic Ancestry

| Analysis | What It Shows | Method | Tools | Notes |
|----------|--------------|--------|-------|-------|
| **Neanderthal %** | Percentage of genome from Neanderthal introgression | Comparison to Altai Neanderthal reference | admixfrog, custom pipeline | Non-African humans carry 1-2%; can identify specific introgressed segments |
| **Denisovan %** | Percentage from Denisovan introgression | Comparison to Altai Denisovan reference | admixfrog | Melanesians carry 4-6%; others trace amounts |
| **Archaic gene segments** | Which specific genes came from archaic humans | Segment detection + gene overlap | Custom pipeline | Interesting examples: EPAS1 (Tibetan altitude adaptation from Denisovans), immune genes (HLA alleles from Neanderthals) |

### 9D. Population Structure

| Analysis | What It Shows | Tools | Notes |
|----------|--------------|-------|-------|
| **Runs of homozygosity (ROH)** | Parental relatedness / population endogamy | PLINK, BCFtools/RoH | Long ROH (>5Mb) = recent common ancestor; many medium ROH = population bottleneck (Ashkenazi, Finnish, etc.) |
| **Inbreeding coefficient (F_ROH)** | Proportion of genome that is autozygous | PLINK | Direct measure from genomic data |
| **IBD segment detection** | Shared ancestry between family members | GERMLINE, PLINK, Beagle | Can map which genomic regions are shared between family members |
| **Ancestry-informative markers** | Quick ancestry classification | Custom SNP panels | ~100-200 AIMs can accurately classify continental ancestry |

---

## 10. Advanced / Creative Analyses

### 10A. Nutrigenomics Panel

Personalized nutrition guidance based on genetics.

| Nutrient/Pathway | Gene | rs Number | What to Report | Evidence Level |
|-----------------|------|-----------|----------------|---------------|
| **Folate metabolism** | MTHFR | rs1801133 (C677T) | TT genotype needs more dietary folate or methylfolate supplementation | Strong |
| **Lactose digestion** | MCM6 | rs4988235 | CC = avoid dairy or use lactase supplements | Strong |
| **Caffeine metabolism** | CYP1A2 | rs762551 | Slow metabolizers should limit coffee; fast metabolizers get CV benefit | Strong |
| **Omega-3 metabolism** | FADS1/FADS2 | rs174546, rs174547 | Affects conversion of plant omega-3 (ALA) to EPA/DHA; some genotypes need direct fish oil | Moderate |
| **Vitamin D metabolism** | GC (VDBP) | rs2282679, rs7041 | Affects vitamin D binding protein levels and bioavailability | Moderate |
| **Vitamin B12** | FUT2, TCN2 | rs601338, rs1801198 | Non-secretors may have lower B12; TCN2 variants affect transport | Moderate |
| **Salt sensitivity** | AGT, ACE, ADD1 | rs699, ACE I/D, rs4961 | Some genotypes have stronger BP response to salt intake | Moderate |
| **Saturated fat response** | APOE, FADS | rs429358, rs174546 | APOE-e4 carriers have greater LDL response to saturated fat | Moderate |
| **Alcohol metabolism** | ADH1B, ALDH2 | rs1229984, rs671 | Fast ADH1B + slow ALDH2 = worst hangover; ALDH2 deficiency = cancer risk from drinking | Strong |
| **Celiac risk** | HLA-DQ2/DQ8 | HLA typing | ~95% of celiacs carry HLA-DQ2.5; without it, celiac is virtually excluded | Strong |
| **Iron absorption** | HFE, TMPRSS6 | rs1800562, rs855791 | HFE carriers may need to limit iron; TMPRSS6 variants affect hepcidin | Strong |

### 10B. Dermatogenomics

| Trait | Gene | rs Number | What to Report |
|-------|------|-----------|----------------|
| **UV sensitivity / burn risk** | MC1R | rs1805007, rs1805008 | Red hair variants = dramatically increased burn risk; need SPF 50+ |
| **Skin pigmentation** | SLC24A5, SLC45A2, TYR | rs1426654, rs16891982, rs1042602 | Baseline skin melanin prediction |
| **Freckling tendency** | IRF4 | rs12203592 | T allele = sun-induced freckling |
| **Photoaging** | MMP1 | rs1799750 | Affects collagen degradation rate |
| **Psoriasis susceptibility** | HLA-C*06:02, IL12B | HLA typing, rs3212227 | Strong genetic component |
| **Vitiligo risk** | NLRP1, TYR | rs2670660, rs1042602 | Autoimmune skin depigmentation |

### 10C. Sleep & Circadian Genetics

| Trait | Gene | rs Number | What to Report |
|-------|------|-----------|----------------|
| **Chronotype** | PER2, CLOCK, CRY1 | rs2304672, rs1801260, various | Morning vs. evening genetic tendency |
| **Sleep duration** | PAX8, VRK2 | rs1823125, rs17190618 | Genetic influence on natural sleep need |
| **Insomnia risk** | PGS000908 | 2,746,982 variants (PGS) | Already in V1 — report as part of sleep panel |
| **Deep sleep efficiency** | ADA | rs73598374 | Affects adenosine metabolism; G/A carriers may get deeper slow-wave sleep |
| **Caffeine sleep disruption** | ADORA2A | rs5751876 | T/T genotype = more sensitive to caffeine-induced sleep disruption |
| **Short sleep ability** | DEC2/BHLHE41 | rs121912617 | Extremely rare — true short sleepers (functional on 4-6 hrs) |

### 10D. Sports & Fitness Genetics

| Trait | Gene | rs Number | What to Report |
|-------|------|-----------|----------------|
| **Muscle fiber composition** | ACTN3 | rs1815739 | RR = sprint/power advantage; XX = endurance advantage |
| **Endurance capacity** | ACE | I/D polymorphism | II = endurance; DD = power/strength |
| **VO2max trainability** | PPARGC1A | rs8192678 | Affects mitochondrial biogenesis response to training |
| **Tendon injury risk** | COL5A1 | rs12722 | T allele = increased Achilles tendinopathy risk |
| **Recovery & inflammation** | IL6 | rs1800795 | G allele = higher IL-6 response to exercise; may need longer recovery |
| **Angiogenesis** | VEGFA | rs2010963 | Affects blood vessel formation in response to training |
| **Pain sensitivity** | SCN9A | rs6746030 | A allele = increased pain sensitivity (relevant for training tolerance) |
| **Muscle damage susceptibility** | CKM | rs8111989 | Affects creatine kinase response to eccentric exercise |

### 10E. Blood Type from DNA

| Blood System | Gene | Variants | Method |
|-------------|------|----------|--------|
| **ABO** | ABO | rs7853989, rs8176722, rs8176746 | Haplotype determination predicts A/B/AB/O |
| **Rh (D)** | RHD | rs590787 + structural variants | Presence/absence of RHD predicts Rh+/Rh- |
| **Kell** | KEL | rs8176058 | Predicts K/k antigen status |
| **Duffy** | ACKR1/DARC | rs2814778 | CC = Duffy-null (malaria resistance in Africans) |
| **MNS** | GYPA/GYPB | Various | Minor blood group antigens |

### 10F. HLA Typing

Full HLA typing from WGS enables:
- **Transplant compatibility** estimation (HLA-A, -B, -C, -DR, -DQ, -DP)
- **Disease susceptibility**: Ankylosing spondylitis (HLA-B27), celiac (HLA-DQ2.5/DQ8), T1D (HLA-DR3/DR4), narcolepsy (HLA-DQB1*06:02), psoriasis (HLA-C*06:02)
- **Drug reaction prediction**: Abacavir (HLA-B*57:01), carbamazepine (HLA-B*15:02, HLA-A*31:01)
- **Methods**: HLA-LA, OptiType, HISAT-genotype for WGS-based HLA calling

### 10G. Facial Feature & Appearance Prediction

| Feature | Method | Accuracy | Notes |
|---------|--------|----------|-------|
| **Eye color** | IrisPlex (6 SNPs) | >90% (blue vs brown) | Very reliable |
| **Hair color** | HIrisPlex (22 SNPs) | ~85% | Good for major categories |
| **Skin color** | HIrisPlex-S (41 SNPs) | ~85% | Good for broad prediction |
| **Face morphology** | 253 SNPs across 188 loci | <8% variance explained | Still research-grade; fun but not highly predictive |
| **Composite phenotype** | Parabon Snapshot-style | Moderate | Combines all above into a "genetic mugshot" |

### 10H. Immune System Profiling

| Analysis | What It Shows | Notes |
|----------|--------------|-------|
| **HLA diversity** | Heterozygosity at HLA loci | Higher HLA diversity = broader immune response to pathogens |
| **KIR gene content** | Natural killer cell receptor repertoire | Interacts with HLA for innate immune function |
| **Complement system** | C4A/C4B copy number | Low C4 = lupus risk; high C4A = schizophrenia risk (MHC locus) |
| **Cytokine profiles** | IL-6, TNF-alpha, IL-10 variants | Genetic predisposition to inflammatory vs. anti-inflammatory responses |
| **COVID-19 susceptibility** | HLA types, IFNAR2, TYK2, OAS1, ABO | Blood type O slightly protective; specific HLA alleles affect severity |

---

## 11. Databases & Tools Reference

### Key Databases

| Database | URL | Purpose |
|----------|-----|---------|
| **PGS Catalog** | pgscatalog.org | Polygenic score coefficients, variants, effect sizes. 4,000+ scores |
| **ClinVar** | ncbi.nlm.nih.gov/clinvar/ | Variant-disease relationships. Gold standard for pathogenicity |
| **OMIM** | omim.org | Gene-disease catalog with clinical synopses |
| **PharmGKB** | pharmgkb.org | Pharmacogenomics knowledge base |
| **CPIC** | cpicpgx.org | Clinical pharmacogenomics guidelines (34 genes, 164 drugs) |
| **gnomAD** | gnomad.broadinstitute.org | Population allele frequencies (76K+ genomes) |
| **SNPedia** | snpedia.com | Wiki of SNP associations (used by Promethease) |
| **GWAS Catalog** | ebi.ac.uk/gwas/ | All published GWAS associations |
| **UniProt** | uniprot.org | Protein function and variant annotations |
| **HGMD** | hgmd.cf.ac.uk | Human Gene Mutation Database (requires license) |
| **ACMG SF v3.2** | clinicalgenome.org | 81 genes recommended for secondary findings reporting |

### Analysis Tools for V2 Pipeline

| Tool | Purpose | Input |
|------|---------|-------|
| **plink2** | PGS calculation, IBD, ROH, QC | VCF/PGEN |
| **bcftools** | Variant calling, filtering, stats | BAM/VCF |
| **HLA-LA** | HLA typing from WGS | BAM |
| **CYP2D6 Caller (Stargazer/Cyrius)** | CYP2D6 star allele + CNV calling | BAM/VCF |
| **PharmCAT** | Pharmacogenomics clinical annotation | VCF |
| **ADMIXTURE** | Ancestry admixture analysis | PLINK bed |
| **admixfrog** | Archaic (Neanderthal/Denisovan) ancestry | BAM |
| **HaploGrep** | mtDNA haplogroup | FASTA/VCF |
| **yhaplo** | Y-DNA haplogroup | VCF |
| **TelSeq** | Telomere length estimation | BAM |
| **HIrisPlex-S** | Eye/hair/skin color prediction | 41 SNPs |
| **PRSice-2 / LDpred2** | PGS calculation methods | GWAS + genotypes |
| **InterVar / ACMG classifier** | Variant pathogenicity classification | VCF |

### Open-Source Analysis Platforms

| Platform | What It Does | URL |
|----------|-------------|-----|
| **Impute.me** | PRS from consumer data (code open-sourced) | github.com/lassefolkersen/impute-me |
| **PRScalc** | Privacy-preserving client-side PRS calculation | Academic paper: Oxford 2023 |
| **Genetic Genie** | Free methylation/detox profiles | geneticgenie.org |
| **Open Humans** | Citizen science data sharing hub | openhumans.org |
| **DNAGENICS** | 70+ free admixture calculators, G25 | dnagenics.com |

---

## Priority Matrix for V2 Implementation

### Tier 1 — Must Have (High clinical value, well-validated)

- [ ] Expanded pharmacogenomics (all 17 CPIC Level A genes)
- [ ] HLA typing (drug reactions + disease associations)
- [ ] Carrier status panel (top 20 recessive diseases)
- [ ] Missing high-value PGS: Celiac (AUROC 0.90!), IBD, Stroke, Lung Cancer
- [ ] Additional single-variant health markers (MTHFR, SERPINA1, PCSK9, Prothrombin)
- [ ] Expanded monogenic cancer panel (Lynch, PALB2, CHEK2)

### Tier 2 — Should Have (High interest, good science)

- [ ] Ancestry admixture analysis
- [ ] Y-DNA and mtDNA haplogroups
- [ ] Neanderthal/Denisovan ancestry
- [ ] Blood type prediction from DNA
- [ ] Fun trait variants (taste, earwax, eye color, etc.)
- [ ] Autoimmune PGS panel (RA, MS, Lupus, Psoriasis, Asthma)
- [ ] Additional cancer PGS (ovarian, pancreatic, thyroid, bladder, kidney, testicular)
- [ ] Longevity PGS

### Tier 3 — Nice to Have (Creative, engaging, newer science)

- [ ] Nutrigenomics panel
- [ ] Sleep & circadian genetics
- [ ] Sports & fitness genetics
- [ ] Dermatogenomics
- [ ] Facial feature prediction (HIrisPlex-S)
- [ ] Immune system profiling
- [ ] Telomere length genetic predisposition
- [ ] ROH / inbreeding coefficient
- [ ] Advanced PGS: risk tolerance, neuroticism, well-being

### Tier 4 — Experimental / Future

- [ ] COVID-19 genetic susceptibility
- [ ] Epigenetic age estimation (needs methylation data — could partner with TruDiagnostic)
- [ ] Microbiome-genetics interactions
- [ ] Facial morphology prediction (low accuracy currently)
- [ ] Multi-ancestry PGS recalibration across all traits

---

## Appendix: Key Publications

| Study | Citation | Significance |
|-------|----------|-------------|
| Khera et al. (2018) | *Nat Genet* | Landmark 5-disease genome-wide PGS (CAD, T2D, AF, IBD, breast cancer) |
| Khera et al. (2019) | *Cell* | BMI PGS equivalent to monogenic obesity mutations |
| Mavaddat et al. (2018) | *AJHG* | PRS313 for breast cancer — clinical standard |
| Seibert et al. (2018) | *BMJ* | Prostate cancer polygenic hazard score |
| Abraham et al. (2014) | *PLoS Genet* | Celiac PGS with AUROC 0.90 |
| Inouye et al. (2018) | *JACC* | metaGRS for CAD — AUROC 0.79 |
| PREPARE Trial (2023) | *Lancet* | 12-gene PGx panel → 30% fewer adverse drug reactions |
| Patel et al. (2023) | *Nat Med* | Multi-ancestry CAD PGS |
| Grove et al. (2019) | *Nat Genet* | Largest autism GWAS |
| Ishigaki et al. (2022) | *Nat Genet* | Multi-ancestry rheumatoid arthritis PGS |
