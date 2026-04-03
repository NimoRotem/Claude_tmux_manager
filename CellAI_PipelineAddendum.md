# Cell AI — Pipeline Addendum: Video Ingestion, Frame Sampling, Gemini Frame Tagging, and Gated Progression

## What this document is

This is a direct addendum to `CellAI_PipelineRedesign.md`. Read that document first. The pipeline described there (frame-level annotation UI, contour-based box proposals, crops exported from confirmed detections) is still the target. This document adds four things that were not covered:

1. Video normalization on upload — handle any input from any microscope
2. Smart frame sampling — replace the arbitrary 20–40 frame limit with a content-aware approach
3. Gemini frame-level pre-tagging — use the API on full frames with numbered boxes, not on crops
4. Gated progression — enforce a sensible order so the system doesn't try to train a model before there's enough validated data to do it meaningfully

---

## Part 1 — Video Normalization on Upload

### The problem

Users will upload videos from different microscopes, cameras, and recording software. These will vary in resolution (anywhere from 640×480 to 4K+), frame rate (25fps to 1000fps+), codec (H.264, H.265, AVI, MOV, raw), duration (a few seconds to many minutes), and file size (tens of MB to several GB). The rest of the pipeline assumes consistent input. Without normalization, every downstream step has to handle edge cases.

### What to do

Add a normalization Celery task that runs immediately after a video is uploaded and before any other processing. This task uses ffmpeg (already installed) to produce a canonical normalized copy of the video that the rest of the pipeline works from. The original file is kept unchanged for reference.

**Normalization steps, in order:**

**Step 1 — Probe the input.** Use `ffprobe` (already used in the codebase) to extract: codec, resolution, frame rate, duration, file size, audio streams, rotation metadata (some phone/microscope recordings embed rotation flags).

**Step 2 — Decide target frame rate.** Microscopy videos recorded at 1000fps contain enormous redundancy — cells barely move between frames at that rate. For frame sampling purposes, a working frame rate of 10–25fps is sufficient. The normalization step does not re-encode at this rate; it stores the original fps in the database and uses it to compute sampling intervals later. However, if the input video is above 500fps, record a flag `high_frame_rate = TRUE` in the `videos` table so the sampling logic knows to apply wider intervals.

**Step 3 — Handle very large files by splitting.** If the video exceeds a configurable size threshold (default: 500MB or 10 minutes, whichever comes first), split it into segments using ffmpeg's segment muxer. Each segment becomes its own row in the `videos` table with a `parent_video_id` foreign key pointing to the original. All downstream processing (frame extraction, detection, annotation) operates on segments, not the original. The UI should surface segments grouped under their parent video.

```sql
ALTER TABLE videos ADD COLUMN parent_video_id INTEGER REFERENCES videos(id);
ALTER TABLE videos ADD COLUMN segment_index INTEGER;
-- null for original uploads; 0,1,2,... for auto-split segments
ALTER TABLE videos ADD COLUMN original_fps FLOAT;
ALTER TABLE videos ADD COLUMN high_frame_rate BOOLEAN DEFAULT FALSE;
ALTER TABLE videos ADD COLUMN normalized_path VARCHAR(500);
-- path to the normalized copy used for processing
ALTER TABLE videos ADD COLUMN original_path VARCHAR(500);
-- path to the raw uploaded file, kept for reference
ALTER TABLE videos ADD COLUMN normalization_status VARCHAR(20) DEFAULT 'pending';
-- values: pending, running, complete, failed
```

**Step 4 — Normalize resolution.** If the input resolution is larger than 2592×1944 (your current native resolution), downscale to that. If it is smaller, keep it as-is (do not upscale — upscaling adds no information). Store the working resolution in `videos.width` and `videos.height` (already exists).

**Step 5 — Normalize codec.** Re-encode to H.264 in an MP4 container if the input is not already H.264/MP4. This ensures ffmpeg can seek reliably to arbitrary timestamps, which the frame sampling step depends on. Use CRF 18 (visually lossless) for the re-encode.

**Step 6 — Fix rotation.** If `ffprobe` reports a rotation flag in metadata (common with mobile recordings), apply the rotation physically during the re-encode step so the normalized file has no rotation metadata and all frames are correctly oriented.

**Step 7 — Emit a normalization summary.** Store a JSON summary in `videos.normalization_summary`: original codec, original fps, original resolution, original duration, original file size, what actions were taken (split, re-encoded, rotated, downscaled), and the resulting segment count. Surface this in the UI when a user clicks on a video, so they can see what happened to their upload.

The normalization task should complete before the video appears as "ready to process" in the UI. Show a progress indicator during normalization. If normalization fails, surface the error clearly with the raw ffprobe output so it is debuggable.

---

## Part 2 — Smart Frame Sampling

### The problem

The previous redesign document suggested 20–40 frames per video as an annotation target, which is too few for a 5-minute video. But sampling every extracted frame is also wrong — consecutive frames from a 1000fps video are nearly identical. The goal is to sample frames that are meaningfully different from each other, spaced far enough apart in time that cells have moved, while preferring sharp over blurry frames.

### What to do

Replace the fixed frame count target with a content-aware sampling strategy. This runs as a Celery task after frame extraction and produces the set of frames marked `selected_for_annotation = TRUE`.

**The algorithm:**

Do not extract all frames first and then select from them. Extract frames on demand at candidate timestamps and evaluate each one before deciding whether to include it.

```
1. Start at t = 0
2. Extract the frame at timestamp t
3. Compute sharpness (Laplacian variance — already implemented)
4. If sharpness < BLUR_THRESHOLD: skip this frame, advance t by a small step (0.1s) and retry
5. If sharpness >= BLUR_THRESHOLD:
   a. Compare this frame to the last accepted frame using a perceptual similarity score
   b. If similarity is too high (frames look nearly identical): skip, advance t by MIN_INTERVAL and retry
   c. If similarity is below threshold (frames are meaningfully different): ACCEPT this frame
      - Store it in the frames table
      - Set selected_for_annotation = TRUE
      - Advance t by MIN_INTERVAL before looking at the next candidate
6. Repeat until end of video
```

**Similarity comparison.** Use a fast perceptual hash (pHash or dHash, available in the `imagehash` Python library) rather than pixel-level comparison. A Hamming distance below a threshold (e.g., < 8 out of 64 bits) means the frames are too similar to annotate separately. This is cheap to compute — no GPU needed, runs in milliseconds per frame.

**Configurable parameters, stored in `project_settings`:**

```python
BLUR_THRESHOLD = 100          # Laplacian variance below this = too blurry, skip
MIN_INTERVAL = 0.5            # seconds — minimum gap between accepted frames
SIMILARITY_THRESHOLD = 8      # pHash Hamming distance below this = too similar, skip
MAX_FRAMES_PER_VIDEO = 500    # hard cap per video segment — safety valve
```

The `MIN_INTERVAL` of 0.5 seconds is the key parameter. At 0.5s gaps, a 5-minute video yields at most 600 candidate windows, from which the blur and similarity filters will further reduce the accepted set. The actual count depends on content — a static sample with little cell movement will yield fewer accepted frames than a dynamic one.

**Why 0.5 seconds specifically.** At typical sperm motility speeds under a microscope, cells move a meaningful fraction of their body length in 0.3–0.5 seconds. Frames 0.5s apart will show noticeably different cell positions and configurations. Frames 0.1s apart are often nearly identical, especially for immotile or slow cells. 0.5s is adjustable; expose it in `project_settings` so the user can tune it.

**Implementation note.** Do not use `ffmpeg` to extract every frame and then filter. Use `ffmpeg` with a specific `-ss` seek timestamp for each candidate frame. This avoids writing thousands of frames to disk only to discard them. Seek, decode one frame, evaluate, decide, move on.

**Install dependency:**
```
pip install imagehash --break-system-packages
```

---

## Part 3 — Gemini Frame-Level Pre-Tagging

### The role of Gemini in the new pipeline

Gemini is no longer used to QA individual crops. It is now used to pre-tag full frames — showing the model the entire microscopy image with numbered bounding boxes overlaid, and asking it to return a tag for each box. The human annotator then sees a frame that is already partially tagged and only needs to correct errors, rather than starting from scratch.

This is a meaningful improvement in annotator efficiency for the same reason that spell-check is faster than writing with no assistance — correcting a suggestion is faster than producing one from scratch, as long as the suggestions are good enough to be worth reading.

### When to run Gemini tagging, and on how many frames

**Do not run Gemini on all frames immediately.** In the early stages, Gemini has no project-specific examples to learn from. Its tags will be based on generic knowledge of sperm morphology and will have meaningful error rates. Running it on hundreds of frames immediately would either waste API budget or — worse — give annotators so many wrong suggestions that they start ignoring them entirely.

Implement a gating rule in `project_settings`:

```python
GEMINI_GATE_MIN_HUMAN_FRAMES = 10
# Do not run Gemini on any frame until at least this many frames
# have been fully annotated by a human

GEMINI_BATCH_SIZE_INITIAL = 20
# Once the gate is passed, run Gemini on this many frames first
# and wait for human review before expanding

GEMINI_BATCH_SIZE_EXPANDED = 200
# After the initial batch is reviewed and Gemini accuracy is
# confirmed acceptable (human agreement rate > 70%), expand to this batch size
```

The gate check is a simple query before enqueueing the Gemini task:

```python
human_annotated_count = db.query(Frame).filter(
    Frame.annotation_status == 'complete',
    Frame.video.has(project_id=project_id)
).count()

if human_annotated_count < settings.GEMINI_GATE_MIN_HUMAN_FRAMES:
    # Don't run Gemini yet — queue frames for direct human annotation
    return
```

### How to construct the Gemini prompt

The prompt must include the full frame image with numbered boxes drawn on it, plus the text instruction. Do not send crops. Do not send one image per cell.

**Prepare the annotated frame image server-side (in the Celery task):**

Use Pillow to draw numbered bounding boxes on a copy of the frame image. Each box gets a small label in the corner with its detection ID number. Save this annotated version as a temporary JPEG. Send it to Gemini. Discard the temporary file after the API call returns.

Do not modify or save over the original frame image.

**Prompt structure:**

The prompt has two parts: a system-level instruction defining the task and the tags, and then the image followed by a structured output request.

The tag taxonomy to use (adjust as the domain knowledge develops):

- `normal_sperm` — single sperm cell, full tail visible, appears morphologically normal
- `abnormal_tail` — single sperm cell, tail is clearly abnormal (bent, coiled, short, broken, double)
- `tail_missing` — single sperm cell, head visible but tail not visible in this crop (trigger the box-expansion retry — see below)
- `blurred` — the cell in this box is too blurry to assess
- `not_sperm` — the detected box contains debris, a bubble, or something that is not a sperm cell
- `multiple_cells` — the bounding box contains more than one cell (box should be split)
- `swollen_head` — sperm with an abnormally large or irregular head
- `pinhead` — abnormally small head

Ask Gemini to return structured JSON: an array where each element contains the box number and the tag. Example expected output:
```json
[
  {"box": 1, "tag": "normal_sperm", "confidence": "high"},
  {"box": 2, "tag": "blurred", "confidence": "high"},
  {"box": 3, "tag": "tail_missing", "confidence": "medium"},
  {"box": 4, "tag": "not_sperm", "confidence": "high"}
]
```

**When Gemini has human-validated examples available** (after the gate is passed and the initial batch is reviewed), include 2–4 validated frame examples in the prompt as few-shot references. Show frames where the human's final tags are known, alongside the numbered annotated image. This is the same few-shot pattern that improves performance on any structured classification task. Retrieve the examples from the database: select frames where `annotation_status = 'complete'` and `gemini_tagging_status = 'complete'`, ordered by the number of human corrections made to Gemini's suggestions (prefer frames where Gemini was mostly right — high agreement = better example).

### The `tail_missing` retry

When Gemini (or later, the human annotator) tags a box as `tail_missing`, trigger a box-expansion step before re-sending to Gemini:

- Expand the bounding box by 40–60% in each direction (clamped to frame boundaries)
- Re-send just that box region to Gemini as a follow-up prompt asking specifically whether the tail is now visible
- If Gemini confirms the tail is now visible, update the detection's bounding box coordinates to the expanded version and re-tag it
- If the tail is still not visible after expansion, leave it tagged as `tail_missing` for the human to decide

This retry is cheap (one small image, one API call) and handles the common case where the contour detector found the head but the bounding box was too tight to include the tail.

### Schema additions for Gemini tagging state

```sql
ALTER TABLE frames ADD COLUMN gemini_tagging_status VARCHAR(20) DEFAULT 'pending';
-- values: pending, skipped (gate not passed), running, complete, failed

ALTER TABLE frames ADD COLUMN gemini_tagged_at TIMESTAMP;
ALTER TABLE frames ADD COLUMN gemini_model_version VARCHAR(50);
-- record which Gemini model was used, for reproducibility

ALTER TABLE detections ADD COLUMN gemini_tag VARCHAR(50);
ALTER TABLE detections ADD COLUMN gemini_confidence VARCHAR(10);
-- values: high, medium, low

ALTER TABLE detections ADD COLUMN gemini_tag_accepted BOOLEAN;
-- set when human annotator confirms or rejects Gemini's tag
-- null = not yet reviewed by human
-- true = human accepted Gemini's tag as correct
-- false = human changed the tag

ALTER TABLE detections ADD COLUMN gemini_tag_raw JSONB;
-- full Gemini response for this detection, for auditability
```

### What the annotator sees

When a frame has been Gemini-tagged, the annotation UI should show each bounding box pre-colored by Gemini's tag (e.g., green for `normal_sperm`, orange for `abnormal_tail`, red for `not_sperm`, grey for `blurred`). The human can then:

- Accept a box's tag with one keystroke (sets `gemini_tag_accepted = TRUE`, copies `gemini_tag` to `morphology_label`)
- Change a tag by clicking the box and selecting from the taxonomy dropdown
- Delete a box (Gemini false positive)
- Add a new box (Gemini false negative)

Track the agreement rate per frame (percentage of boxes where `gemini_tag_accepted = TRUE`) and surface this in the admin dashboard. When the agreement rate across the initial 20-frame batch is above 70%, automatically expand Gemini processing to the full video.

---

## Part 4 — Gated Progression: Don't Let the System Run Ahead of Its Data

### The principle

Each stage of the pipeline should only activate when the previous stage has produced enough validated output to make it useful. Training a model on 30 crops is a waste of compute and produces a meaningless model. Running Gemini on 500 frames before any human has validated a single one wastes API budget and produces suggestions nobody trusts.

Implement this as explicit gates checked before each Celery task is enqueued. Gates are checked in `tasks.py` before enqueueing downstream work, not as separate scheduler jobs.

### The gate table

Define the gates clearly so they are visible and adjustable:

```python
# In project_settings or a constants file:

PIPELINE_GATES = {

    # Gate 1: Don't start Gemini tagging until humans have annotated at least N frames
    "gemini_initial_batch": {
        "requires": "human_annotated_frames >= 10",
        "batch_size": 20,
        "description": "Minimum human frames before running Gemini on any frame"
    },

    # Gate 2: Don't expand Gemini to full video until initial batch agreement is acceptable
    "gemini_full_expansion": {
        "requires": "gemini_agreement_rate >= 0.70 on initial 20 frames",
        "batch_size": 200,
        "description": "Gemini agreement rate threshold before expanding to full video"
    },

    # Gate 3: Don't export training crops until enough confirmed detections exist
    "training_crop_export": {
        "requires": "confirmed_detections_with_labels >= 100",
        "description": "Minimum labeled detections before generating training crop export"
    },

    # Gate 4: Don't start model training until the crop export is large enough
    "model_training": {
        "requires": "exported_crops >= 200 AND unique_labels >= 2",
        "description": "Minimum crops and class diversity before training"
    },
}
```

These are guidelines for implementation, not exact syntax. The point is that these thresholds are centralized, visible, and adjustable — not scattered as magic numbers across `tasks.py`.

### How gates appear in the UI

The project dashboard should show a simple pipeline status indicator:

```
✅ Video normalized
✅ Frames extracted (847 frames sampled from 5 videos)
✅ Detections proposed (12,400 boxes across 847 frames)
⏳ Human annotation in progress (34 / 847 frames complete)
🔒 Gemini tagging — waiting for 10 human-annotated frames (currently: 34 ✅ — UNLOCKED)
🔒 Training crop export — waiting for 100 labeled detections (currently: 512 ✅ — UNLOCKED)
🔒 Model training — waiting for 200 exported crops (currently: 0)
```

Each locked stage shows exactly what it is waiting for and the current count. This prevents "why isn't the model training" confusion and makes the data requirements legible to non-developers.

### What triggers gate checks

- After a human marks a frame as `annotation_status = 'complete'`: check Gemini initial gate and training crop export gate
- After Gemini tags an initial batch of frames and those are reviewed: check Gemini full expansion gate
- After training crop export completes: check model training gate
- Never run these gates on a scheduler — run them as a consequence of human actions completing, so the system advances exactly when data justifies it

---

## Summary of new components

| Component | What it does | When it runs |
|-----------|-------------|--------------|
| Video normalization task | Probe, split, re-encode, fix rotation | Immediately on upload, before anything else |
| Smart frame sampler | pHash similarity + blur filter + MIN_INTERVAL spacing | After normalization, before detection |
| Gemini frame tagger | Send full annotated frames, get box-level tags back | After gate: 10 human frames complete |
| `tail_missing` box expander | Expand tight boxes, retry Gemini for tail visibility | Triggered per-detection when tag = tail_missing |
| Gate checker | Validate pipeline stage preconditions | After each human annotation session completes |
| Pipeline status dashboard | Show current counts vs. gate thresholds | Always visible in project view |

## Dependencies to add

```bash
pip install imagehash --break-system-packages
# imagehash: perceptual hashing for frame similarity comparison
# All other dependencies (ffmpeg, Pillow, Gemini API) already present
```
