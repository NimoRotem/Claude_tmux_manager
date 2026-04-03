# Cell AI — Quick Wins Action Plan

## What this project is

Cell AI processes microscopy videos of sperm samples through a pipeline: video upload → frame extraction → cell detection → crop generation → AI quality filtering → human labeling → model training → inference. The end goal is a real-time morphology scoring tool that helps embryologists during sperm selection (ICSI). The system is a FastAPI + Celery + PostgreSQL backend with a React frontend, deployed on a single Linux server without Docker or GPU.

**Current state:** 7 videos processed, 8,328 crops generated, only 613 (7.4%) survived QA filtering, and labeling has not started yet. No models have been trained. The team building this from the ground up has a full architectural advisory. This document covers only the changes that can be made to the existing running system for maximum near-term improvement — higher quality crops, less waste in the pipeline, and a smarter labeling process before any rewrite lands.

---

## Change 1 — Replace the contour detector with a trained YOLO model

**Why this is the most important change:** The current detector uses OpenCV contours to find bright blobs in frames. It cannot distinguish sperm from debris, so it generates thousands of false positives that then flow into Gemini for expensive QA filtering. 87% of crops are rejected. This means roughly 12 Gemini API calls are wasted for every 1 useful crop. Fixing detection quality upstream collapses all downstream waste.

**What to do:**

Train a YOLOv8 nano model (`yolov8n`) on the VISEM-Tracking dataset before integrating it. VISEM-Tracking is a public dataset of 20 annotated 30-second videos of wet human semen preparations, with bounding boxes already in YOLO format. License is CC BY 4.0 (free, commercial use permitted). Download from: `https://zenodo.org/record/7293726`

After training on VISEM-Tracking, fine-tune on a small set of manually annotated frames from your own videos (50–100 frames is enough). Your videos are 2592×1944 at 1000fps; VISEM is 640×480 at 50fps. This domain gap is real and fine-tuning on your own frames closes it. Use Label Studio (already in use) to annotate bounding boxes on those frames.

The system already has an `active_detector_id` field in `project_settings` to support swappable detectors. Integrate the trained YOLO model as a new detector option through that mechanism rather than replacing the contour code directly, so you can switch back if needed.

Run inference at `imgsz=1280` rather than 640 when processing your high-resolution frames — sperm heads are 30–60px in a 2592px-wide image and will be missed at lower inference sizes.

Only include class 0 detections (sperm) from YOLO output — skip clusters and pinheads, which VISEM-Tracking also annotates but which produce bad crops for morphology classification.

**Expected direction:** Acceptance rate should improve substantially. Treat any specific number as a hypothesis to measure after integration, not a guarantee.

---

## Change 2 — Fix crop padding to scale with detection size

**Why:** The current fixed 350px padding is applied equally to a 30px head and a 60px head. Sperm tails are 5–10× the length of the head, so crops need to scale with the cell, not be a fixed square. The padding also causes clipping at image edges.

**What to do:**

Replace the fixed `CROP_PADDING` constant in `tasks.py` with a function that computes padding proportional to the detected bounding box size. A multiplier of 10–14× the larger bounding box dimension, with a minimum of 200px, is a reasonable starting point. Center the crop on the detection midpoint and clamp to image boundaries.

At the same time, save a lossless PNG master crop alongside the existing JPEG for each accepted crop. Add a `master_crop_path` column to the `crops` table. This costs almost nothing now and preserves the ability to do segmentation-based analysis later without reprocessing all videos.

---

## Change 3 — Add tracking and deduplication before labeling starts

**Why this must happen before any labeling:** Your videos are extracted at 5fps from high-frame-rate captures. The same physical sperm cell almost certainly appears in multiple consecutive frames. Without deduplication, you could label 20–40 crops of the same cell, inflating your apparent dataset size and creating train/test leakage. This cannot be fixed retroactively — it must be in place before the labeling queue is populated.

**What to do:**

Enable ByteTrack (built into Ultralytics) during video inference by using `model.track()` instead of `model.predict()`, with `persist=True` and `stream=True`. This assigns a `track_id` to each detection across frames of a video.

Add `track_id` and `is_track_representative` boolean columns to the `detections` table.

After processing a video, group detections by `track_id` and select one representative crop per track — the detection with the highest `focus_score`. Mark it `is_track_representative = TRUE`. Only send representative crops to the labeling queue.

Also add `patient_id`, `sample_id`, and `session_id` columns to the `videos` table now, even if they're initially null. When you eventually enforce train/test splits, those splits must be at the patient level, not the video level — two videos from the same patient must stay in the same split. Adding the columns now means you can populate them as videos are uploaded going forward.

---

## Change 4 — Add rotation augmentation and weighted loss to the classifier training code

**Why:** These are one-line changes with outsized effects on a small imbalanced dataset. Sperm appear at arbitrary orientations in video — a classifier trained without rotation augmentation learns orientation-specific features instead of morphology. Class imbalance means rare tail types (broken, double) will be ignored by the model entirely without weighted loss.

**What to do:**

In the training transform pipeline in `tasks.py`, add full 360° random rotation, horizontal flip, and vertical flip to the augmentation stack. Full rotation matters here — unlike natural images, there is no "upright" orientation for sperm.

In the training loss, compute per-class weights as inverse class frequency and pass them to the loss function. This is a single additional argument to `CrossEntropyLoss`.

---

## Change 5 — Use a strong vision API to pre-sort the labeling queue

**Why:** The labeling team is about to work through 613 crops in the order they were accepted. Most are probably easy cases a model handles confidently. Spending human labeling time on easy cases is wasteful — spending it on uncertain or ambiguous cases produces far more training value per hour.

**What to do:**

Before labeling begins, run each accepted crop through a strong vision API using few-shot prompting: provide 6–8 human-validated reference images per category alongside the target crop, and ask the model to predict the morphology class and return a confidence score.

For the strong/expensive model call, use either **Gemini 2.5 Flash** (already integrated via Vertex AI, currently the top-performing cost-efficient vision model on standard benchmarks) or **GPT-5.2** (OpenAI's current flagship as of early 2026, strong on fine-grained visual reasoning). For a cheaper pre-filter if cost is a concern, **Gemini 3 Flash** or **GPT-5 mini** are substantially cheaper options. Avoid the smallest/cheapest models (GPT-5 nano, Flash-Lite) for this task — fine-grained morphology distinction requires meaningful visual reasoning capacity. Before committing, verify current model capability and pricing at the provider's documentation, as this space moves fast.

Store only the confidence score in a new `labeling_priority_score` column on the `crops` table. Re-sort the labeling queue so low-confidence crops appear first. High-confidence crops can be labeled last or processed quickly.

**Hard constraint:** Do not use the API's predicted class as morphology ground truth. API predictions are queue-ordering signals only. All morphology ground truth must come from human annotators. The one partial exception is the binary QA task (usable/not-usable crop) — API labels are acceptable there as weak signal at reduced training weight, but never in the test set.

---

## Change 6 — Fix three security issues

These do not affect model accuracy but must be addressed before any clinical data or external users touch the system.

- **JWT secret:** `main.py` falls back to a hardcoded string if `SECRET_KEY` env var is not set. Remove the fallback entirely — raise an error at startup if the env var is absent.
- **Default admin credentials:** `admin@cellai.local` / `admin123` are seeded on startup unconditionally. Only seed if both `ADMIN_EMAIL` and `ADMIN_PASSWORD` env vars are present and no users exist yet.
- **CORS:** `allow_origins=["*"]` should be replaced with an env-var-configured list of allowed origins.
- **GCP project ID:** `nimo-gpt` is hardcoded in `ai_filter.py`. Move to a `GOOGLE_CLOUD_PROJECT` env var.

---

## Order of operations

Do **Change 3** (tracking + deduplication) before populating the labeling queue.  
Do **Change 1** (YOLO detector) before re-running the pipeline on existing or new videos.  
Do **Changes 4 and 5** before the first training run and before labeling starts respectively.  
**Changes 2 and 6** can happen in parallel with everything else.
