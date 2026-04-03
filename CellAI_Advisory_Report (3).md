# Cell AI — Technical Advisory Report
**Sperm Morphology Analysis Platform | March 2026**

---

## Context for the Agent

This document contains actionable technical recommendations for the Cell AI project at `rotem.cc/sperm` (repo: `NimoRotem/cell-ai`). The project is a FastAPI + React + Celery + PostgreSQL system that processes microscopy videos of sperm samples, detects individual cells, crops them, runs AI quality filtering (Gemini Vision), and provides a UI for manual labeling. The goal is to train a tail-morphology classifier and eventually use it as a real-time ICSI assist tool.

**Current state:** 7 videos processed, 8,328 crops generated, 613 accepted (7.4%), 0 labeled, 0 models trained.

**Recent addition:** The system now tracks whether a crop's QA status was set by a human reviewer vs. the AI filter. This creates a correction signal: cases where the human disagreed with the AI are high-value training data. See Priority 7 for how to exploit this fully.

**API resources available:** Gemini API (currently in use), OpenAI API (GPT-4o vision). See Priority 8 for pseudo-annotation strategy. Read the constraints on morphology pseudo-labeling carefully — it is useful for QA triage and queue prioritization, but should not be treated as a major source of trusted morphology ground truth.

**Product framing constraint — applies to all development decisions:** The correct near-term framing is **morphology scoring and ranking assistance**, not autonomous or quasi-autonomous sperm selection. This is not only a future regulatory concern. It affects what metrics to optimize for, what data to collect, what the UI shows, and what claims are made to embryologists. If developers frame the goal internally as "tell the embryologist which sperm to pick," they will optimize for the wrong thing: high per-crop classification confidence rather than calibrated ranking quality with appropriate uncertainty. Design data schema, model outputs, UI, and evaluation metrics around supporting a human decision, not replacing it.

**On numerical estimates in this document:** Figures like acceptance rates after YOLO, training times, and accuracy expectations are planning hypotheses, not reliable predictions. Measure against your own data before trusting any of them.

**Stack:** Python 3.10, FastAPI, Celery + Redis, PostgreSQL, PyTorch (CPU only), OpenCV, Gemini 2.5 Flash Vision, React 18 + Vite.

---

## Priority 0 — Security Fixes (Do Before Anything Else)

These are critical and take ~30 minutes total.

### 0.1 JWT Secret
**Problem:** `main.py` falls back to a hardcoded secret `"cell-ai-secret-change-in-production-2024"` if the env var is not set.  
**Fix:** Remove the fallback. Raise an error if `SECRET_KEY` env var is not present at startup.

```python
# In main.py or config.py
import os
SECRET_KEY = os.environ["SECRET_KEY"]  # Will raise KeyError if not set — intentional
```

### 0.2 Default Admin Credentials
**Problem:** `admin@cellai.local` / `admin123` are seeded on startup.  
**Fix:** Seed the admin only if no users exist AND credentials are provided via env vars. If env vars are absent, do not seed.

```python
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if ADMIN_EMAIL and ADMIN_PASSWORD and db.query(User).count() == 0:
    # create admin user
```

### 0.3 CORS
**Problem:** `allow_origins=["*"]`  
**Fix:** Read allowed origins from an env var.

```python
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, ...)
```

---

## Priority 1 — Replace the Contour Detector with YOLOv8

### Why
The current OpenCV contour detector generates 8,328 crops from 7 videos, of which only 613 (7.4%) survive QA. This means ~12 Gemini API calls are spent per usable crop. The heuristic detector cannot distinguish sperm from debris; it just finds blobs.

### What to Do

**Step 1: Download VISEM-Tracking dataset**
- URL: https://zenodo.org/record/7293726
- License: CC BY 4.0 (free, commercial use allowed)
- Contents: 20 videos × 30s at 50fps = 29,196 frames, bounding boxes already in YOLO format
- Classes: `sperm`, `sperm_cluster`, `small_or_pinhead`
- This is the standard benchmark dataset used in published sperm detection papers (YOLOv5, YOLOv8 baselines exist)

**Step 2: Train YOLOv8n on VISEM-Tracking**
```bash
pip install ultralytics
# Create visem.yaml pointing to the downloaded dataset
yolo detect train \
  data=visem.yaml \
  model=yolov8n.pt \
  epochs=50 \
  imgsz=640 \
  batch=16 \
  lr0=0.01
# CPU training estimated time: 3-8 hours (one-time cost)
```

**Step 3: Fine-tune on your own frames**
- Extract 50-100 frames from your existing videos using ffmpeg
- Annotate bounding boxes using Label Studio (already in use) or CVAT (free, similar)
- Fine-tune for 20-30 epochs at a lower learning rate:
```bash
yolo detect train \
  data=your_data.yaml \
  model=runs/detect/train/weights/best.pt \
  epochs=25 \
  imgsz=640 \
  lr0=0.001
```

**Step 4: Integrate into tasks.py**

The system already has a `active_detector_id` field in `project_settings`. Replace the contour detection function with YOLO inference:

```python
from ultralytics import YOLO

model = YOLO("path/to/best.pt")

def detect_cells_yolo(frame_path, conf_threshold=0.4):
    results = model(frame_path, conf=conf_threshold, imgsz=640)
    detections = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        confidence = float(box.conf[0])
        class_id = int(box.cls[0])
        if class_id == 0:  # class 0 = 'sperm' (not cluster or pinhead)
            detections.append({
                "x1": int(x1), "y1": int(y1),
                "x2": int(x2), "y2": int(y2),
                "confidence": confidence
            })
    return detections
```

**Note on resolution mismatch:** Your videos are 2592×1944 at 1000fps; VISEM is 640×480 at 50fps. When running YOLO on your full-res frames, use `imgsz=1280` or tile the image into overlapping 640×640 patches before inference, then merge detections with NMS. Sperm heads are 30-60px in your 2592px-wide frames — at 640px inference size they become ~7-15px and may be missed.

**Expected outcome:** Acceptance rate should rise from ~7% to 30-60%+, collapsing Gemini API usage proportionally.

---

## Priority 1b — Add Tracking and Per-Track Deduplication

**This must be done before labeling starts.** It is a gap in the original recommendations and a critical one.

### The Problem

Your videos are extracted at 5fps from 1000fps captures. The same physical sperm cell appears in multiple consecutive frames. Without deduplication, you could have 20-40 crops of the same individual cell in your dataset. This causes three problems:

1. **Inflated dataset size** — 613 accepted crops may represent far fewer than 613 unique cells
2. **Train/test leakage** — the same cell appearing in both splits makes accuracy metrics meaningless
3. **Biased statistics** — a single motile sperm that happens to stay in frame skews class distributions

### Fix: Add a Tracker After YOLO Detection

Ultralytics YOLO has ByteTrack built in. Enable it during inference with a single flag:

```python
from ultralytics import YOLO

model = YOLO("path/to/best.pt")

def detect_and_track_frame(frame_path, conf_threshold=0.4):
    # track=True enables ByteTrack across frames in a video
    results = model.track(frame_path, conf=conf_threshold, persist=True, tracker="bytetrack.yaml")
    detections = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        confidence = float(box.conf[0])
        track_id = int(box.id[0]) if box.id is not None else None  # None if tracker lost it
        detections.append({
            "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
            "confidence": confidence,
            "track_id": track_id,
        })
    return detections
```

For processing a full video (preferred over frame-by-frame for tracking continuity):

```python
def detect_and_track_video(video_path, conf_threshold=0.4):
    results = model.track(
        source=video_path,
        conf=conf_threshold,
        persist=True,
        tracker="bytetrack.yaml",
        stream=True,   # memory-efficient for long videos
        imgsz=1280,
    )
    all_detections = []
    for frame_idx, result in enumerate(results):
        for box in result.boxes:
            if box.id is None:
                continue
            all_detections.append({
                "frame_index": frame_idx,
                "track_id": int(box.id[0]),
                "x1": int(box.xyxy[0][0]), "y1": int(box.xyxy[0][1]),
                "x2": int(box.xyxy[0][2]), "y2": int(box.xyxy[0][3]),
                "confidence": float(box.conf[0]),
            })
    return all_detections
```

### Schema Changes Required

Add `track_id` to the `detections` table, and add provenance columns that should have been there from the start:

```sql
-- detections table
ALTER TABLE detections ADD COLUMN IF NOT EXISTS track_id INTEGER;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS is_track_representative BOOLEAN DEFAULT FALSE;

-- videos table — add specimen provenance
ALTER TABLE videos ADD COLUMN IF NOT EXISTS sample_id VARCHAR(100);    -- semen sample identifier
ALTER TABLE videos ADD COLUMN IF NOT EXISTS patient_id VARCHAR(100);   -- anonymized patient ID
ALTER TABLE videos ADD COLUMN IF NOT EXISTS session_id VARCHAR(100);   -- collection session

-- labels table — add annotation provenance
ALTER TABLE labels ADD COLUMN IF NOT EXISTS label_version INTEGER DEFAULT 1;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS consensus_status VARCHAR(20);
-- consensus_status values: 'single', 'agreement', 'disagreement', 'adjudicated'
ALTER TABLE labels ADD COLUMN IF NOT EXISTS annotator_confidence VARCHAR(10);
-- values: 'high', 'medium', 'low' — separate from model confidence
```

### Per-Track Representative Crop Selection

After tracking, select one crop per track for labeling. Don't label all crops from the same track — that's redundant. Select the crop with the best focus score:

```python
def select_representative_crops(detections_by_track: dict[int, list]) -> list[int]:
    """
    For each track, select the detection with the highest focus_score as the
    representative crop to send to the labeling queue.
    Returns list of detection IDs.
    """
    representatives = []
    for track_id, detections in detections_by_track.items():
        best = max(detections, key=lambda d: d["focus_score"])
        representatives.append(best["id"])
    return representatives
```

Mark the selected crop: `UPDATE detections SET is_track_representative = TRUE WHERE id = ?`

### Dataset Split Must Be at Patient Level, Not Video Level

The current 70/15/15 split is by video, which is better than by crop but still wrong if two videos share a patient. With `patient_id` added to `videos`, enforce patient-disjoint splits:

```python
def split_dataset_by_patient(db, project_id, train=0.7, val=0.15, test=0.15):
    """
    Group videos by patient_id. Split patient groups, not individual videos.
    If patient_id is null, fall back to video-level split and log a warning.
    """
    from sklearn.model_selection import GroupShuffleSplit
    
    videos = db.query(Video).filter(Video.project_id == project_id).all()
    video_ids = [v.id for v in videos]
    patient_ids = [v.patient_id or f"unknown_{v.id}" for v in videos]
    
    gss = GroupShuffleSplit(n_splits=1, test_size=test + val, random_state=42)
    train_idx, temp_idx = next(gss.split(video_ids, groups=patient_ids))
    
    # Further split temp into val and test
    gss2 = GroupShuffleSplit(n_splits=1, test_size=test / (test + val), random_state=42)
    temp_videos = [video_ids[i] for i in temp_idx]
    temp_patients = [patient_ids[i] for i in temp_idx]
    val_idx, test_idx = next(gss2.split(temp_videos, groups=temp_patients))
    
    return {
        "train": [video_ids[i] for i in train_idx],
        "val": [temp_videos[i] for i in val_idx],
        "test": [temp_videos[i] for i in test_idx],
    }
```

Note: with only 7 videos from (presumably) 7 patients, the val and test sets will each be 1 video. Document this prominently in `training_runs.config_json` as a known limitation. Accuracy numbers from a 1-video test set are indicative only.

---

## Priority 2 — Fix Crop Padding

### Problem
Fixed 350px padding around 30-60px detections is arbitrary. Two issues:
1. A 35px head and a 60px head get identical framing despite different sizes
2. At image edges, fixed padding causes clipping

### Fix

Replace fixed padding with proportional padding in `tasks.py`:

```python
# Current (bad):
CROP_PADDING = 350  # fixed pixels

# Replace with:
def compute_padding(bbox_w, bbox_h, multiplier=4.0, min_pad=200):
    return max(int(multiplier * max(bbox_w, bbox_h)), min_pad)

# When cropping:
bbox_w = x2 - x1
bbox_h = y2 - y1
pad = compute_padding(bbox_w, bbox_h)
cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
crop_x1 = max(0, cx - pad)
crop_y1 = max(0, cy - pad)
crop_x2 = min(frame_w, cx + pad)
crop_y2 = min(frame_h, cy + pad)
```

Rationale: Sperm tails are 5-10× the length of the head, so generous context is correct. The multiplier of 4.0 gives a 50px head a ~400px crop, which is appropriate. Adjust based on visual review of the 613 existing accepted crops.

---

## Priority 3 — Celery Task Queue Separation

### Problem
A single Celery queue means a long training job blocks video processing tasks.

### Fix

Define two queues in `tasks.py` and the worker startup:

```python
# In tasks.py
@celery_app.task(queue="processing")
def extract_frames(video_id): ...

@celery_app.task(queue="processing")  
def run_detection(video_id): ...

@celery_app.task(queue="processing")
def run_ai_filter(video_id): ...

@celery_app.task(queue="training")
def build_dataset(project_id, version): ...

@celery_app.task(queue="training")
def train_model(training_run_id): ...
```

Start two separate worker processes (add to systemd):
```bash
# Worker 1: video processing (higher concurrency)
celery -A app.tasks worker -Q processing --concurrency=4 --loglevel=info

# Worker 2: ML training (single concurrency, can be long-running)
celery -A app.tasks worker -Q training --concurrency=1 --loglevel=info
```

---

## Priority 4 — Classification Model

### Architecture Recommendation

For the current scale (613 crops, 8 classes), train on CPU with pretrained ImageNet weights. Do not train from scratch.

**Recommended order to try:**
1. **ResNet18** (primary baseline) — well-understood, fast, strong transfer learning, avoids overfitting on small medical datasets
2. **ConvNeXt-Tiny** — consistently outperforms ResNet and EfficientNet on small domain-specific datasets in recent benchmarks
3. **EfficientNet-B0** — parameter-efficient but some medical imaging research shows it underperforms older architectures on domain transfer

**Do not use:** Vision Transformers (ViT, Swin) — require large datasets, consistently underperform CNNs in low-data fine-tuning.

### Training Code Pattern

```python
import torchvision.models as models
import torch.nn as nn

def build_model(num_classes, arch="resnet18"):
    if arch == "resnet18":
        model = models.resnet18(weights="IMAGENET1K_V1")
        # Phase 1: freeze backbone, train only classifier head
        for param in model.parameters():
            param.requires_grad = False
        model.fc = nn.Linear(model.fc.in_features, num_classes)
    elif arch == "convnext_tiny":
        model = models.convnext_tiny(weights="IMAGENET1K_V1")
        for param in model.parameters():
            param.requires_grad = False
        model.classifier[2] = nn.Linear(model.classifier[2].in_features, num_classes)
    return model

# Phase 1: train head only (5-10 epochs, lr=1e-3)
# Phase 2: unfreeze all, train end-to-end (20-30 epochs, lr=1e-4)
```

### Class Imbalance — Required Fix

With 613 crops and 8 classes, distribution is certainly uneven. Add class weighting:

```python
from collections import Counter
import torch

label_counts = Counter(all_labels)
total = sum(label_counts.values())
weights = torch.tensor([total / label_counts[i] for i in range(num_classes)], dtype=torch.float)
criterion = nn.CrossEntropyLoss(weight=weights)
```

### Data Augmentation — Required

Sperm orientation in video is arbitrary (can appear at any angle). Apply aggressive augmentation:

```python
from torchvision import transforms

train_transform = transforms.Compose([
    transforms.RandomRotation(degrees=360),       # Full rotation — critical
    transforms.RandomHorizontalFlip(),
    transforms.RandomVerticalFlip(),
    transforms.ColorJitter(brightness=0.3, contrast=0.3),
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])
```

---

## Priority 5 — Dataset Strategy

### On the Morphology Label Schema — Use Multilabel, Not Mutually Exclusive Classes

**This is a correction from the original recommendation**, which suggested collapsing to 4-5 mutually exclusive classes. That is the wrong data model for sperm morphology.

Most abnormal sperm have multiple simultaneous defects. A sperm can be both `coiled` and `short`. Forcing a single label loses information and creates annotator confusion when a cell genuinely fits two categories. The literature and WHO guidelines describe defects as co-occurring attributes, not exclusive categories.

**Use a two-level schema instead:**

**Level 1 — Binary QA (keep separate from morphology):**
These are QA states, not morphology labels. They live in the `crops` table, not `labels`:
- `usable` / `not_usable`
- Rejection reasons as flags: `multi_cell`, `blurred`, `tail_not_visible`, `cut_off`, `not_sperm`, `artifact`

**Level 2 — Morphology (multilabel on usable crops only):**

```python
# In label_classes table or as a fixed schema in code:
MORPHOLOGY_SCHEMA = {
    # Top-level binary — always label this
    "tail_normal": bool,        # True = morphologically normal tail

    # Attribute flags — label all that apply (only relevant if tail_normal=False)
    "attr_bent": bool,          # sharp angle or kink >45°
    "attr_coiled": bool,        # tail loops back on itself
    "attr_short": bool,         # significantly shorter than normal
    "attr_broken": bool,        # fragmented or interrupted
    "attr_double": bool,        # two or more tails

    # Annotator metadata — always capture
    "annotator_confidence": str,  # "high" / "medium" / "low"
    "uncertain": bool,            # flag without guessing — goes to consensus review
}
```

**Database: store as columns, not as a single class string:**

```sql
-- Replace or supplement the single label_class column in labels table
ALTER TABLE labels ADD COLUMN IF NOT EXISTS tail_normal BOOLEAN;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS attr_bent BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS attr_coiled BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS attr_short BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS attr_broken BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS attr_double BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS uncertain BOOLEAN DEFAULT FALSE;
ALTER TABLE labels ADD COLUMN IF NOT EXISTS annotator_confidence VARCHAR(10);
```

**Training: use BCEWithLogitsLoss for multilabel:**

```python
import torch
import torch.nn as nn

# For multilabel output: one sigmoid per attribute, not softmax
def build_multilabel_head(backbone, num_attributes=6):
    # num_attributes: [tail_normal, bent, coiled, short, broken, double]
    in_features = backbone.fc.in_features  # ResNet18
    backbone.fc = nn.Linear(in_features, num_attributes)
    return backbone

# Loss: BCEWithLogitsLoss (sigmoid + binary cross entropy per attribute)
criterion = nn.BCEWithLogitsLoss(
    pos_weight=torch.tensor([1.0, 3.0, 3.0, 3.0, 5.0, 5.0])  # upweight rare attributes
)

# Inference: threshold each attribute independently
def predict(model, image, threshold=0.5):
    logits = model(image)
    probs = torch.sigmoid(logits)
    return {
        "tail_normal": probs[0].item(),
        "attr_bent": probs[1].item() > threshold,
        "attr_coiled": probs[2].item() > threshold,
        "attr_short": probs[3].item() > threshold,
        "attr_broken": probs[4].item() > threshold,
        "attr_double": probs[5].item() > threshold,
    }
```

**For the pseudo-annotation prompts (Priority 8)**, update the morphology prompt to ask the model to return a multilabel JSON object rather than a single class string. The `MORPHOLOGY_SCHEMA` dict above maps directly to the JSON structure to request.

**Practical note on starting small:** On the first labeling pass with 613 crops, you can label just `tail_normal` (binary) and `annotator_confidence`. Add attribute flags in pass 2 once the binary classifier is working. This gets you a useful model faster without overwhelming annotators with a complex schema from day one.

### On Consensus Labeling

With a single annotator, label disagreements are invisible. The other advisor is right that this matters. Minimum viable consensus process:

- Label every crop with a primary annotator
- Route all `uncertain=True` crops and all crops where `annotator_confidence = "low"` to a second annotator
- Adjudicate disagreements (any case where annotators differ) with the embryologist
- Store `consensus_status` per label: `single`, `agreement`, `disagreement`, `adjudicated`
- Only use `agreement` and `adjudicated` labels in the test set

CVAT supports consensus replica jobs natively. Label Studio supports multiple annotators per task with agreement scoring.

### Public Datasets — Updated Recommendations

| Dataset | License | Use | Caution |
|---------|---------|-----|---------|
| VISEM-Tracking | CC BY 4.0 | **Primary**: train YOLO detector | None — best starting point |
| Monash live unstained | Check paper | **Best domain match**: live brightfield, includes uncertainty labels | Confirm license before use |
| HuSHeM | Free (Mendeley) | Supplement normal/abnormal examples | Stained slides — domain gap |
| Hi-LabSpermMorpho | Check paper | Taxonomy exploration, weak morphology pretraining | Stained domain |
| MHSMA | CC BY-NC-SA 4.0 | Has tail labels | NC = no commercial use; tail not fully visible in all images |
| SCIAN | Free | Head classification only | Stained; head-only, not tail |
| VISEM original (not Tracking) | CC BY-NC 4.0 | Avoid on a commercial path | NC license |
| AndroGen (synthetic) | AGPL-3.0 | Synthetic augmentation | AGPL is viral — secure commercial rights before use |

**Key addition from second advisor:** Prioritize the **Monash clinically labelled live, unstained dataset** — it is the closest public data to your actual domain (live, unstained, brightfield microscopy) and explicitly separates full-agreement, partial-agreement, and disagreement annotation cases, which is directly useful for your consensus labeling design.

### Minimum Viable Dataset for Deployment

Target: **200+ labeled crops per retained class** before treating the model as clinically useful. This is achievable by:
1. Labeling the existing 613 accepted crops
2. Uploading 10-15 additional videos and processing with the new YOLO detector (higher acceptance rate means more usable crops per video)

---

## Priority 6 — Gemini Vision QA: Phase-Out Plan

### Current state
Gemini 2.5 Flash processes crops in batches of 8. At current scale (~1,041 API calls per run), cost is low. With YOLO detection reducing total crops by 60-80%, this becomes even cheaper.

### When to replace it

After you have 500+ human-reviewed crops (accepted + rejected, see Priority 7), train a **local binary classifier** on those labels. This is much simpler than the morphology classifier, and the human correction signal makes it better than training on AI-assigned labels alone:

```python
# Binary classifier: is this a usable crop? (0=reject, 1=accept)
# Architecture: MobileNetV3-Small (tiny, fast, CPU-friendly)
model = models.mobilenet_v3_small(weights="IMAGENET1K_V1")
model.classifier[3] = nn.Linear(model.classifier[3].in_features, 2)
# Training time on 500 crops, CPU: ~5 minutes
# Expected accuracy: 90%+
```

Replace Gemini with this classifier as the primary filter. Optionally keep Gemini for a second-pass on borderline cases (confidence between 0.4-0.6).

**Critically:** Weight human-corrected examples 2-3× higher than AI-confirmed examples during training. Cases where the human and AI agreed are lower-signal; cases where they disagreed are where the model needs to learn the most.

---

## Priority 7 — Human-in-the-Loop Feedback System

This is the core mechanism for making the pipeline self-improving over time. It applies to two distinct signals: **crop quality corrections** (QA stage) and **morphology label corrections** (classification stage). Both need to be captured, stored, and fed back into retraining systematically.

### 7.1 — Database: Track the Source of Every Decision

The `crops` table needs to distinguish between AI-assigned and human-assigned QA status. Add a `reviewed_by_human` boolean (apparently already added based on the feature description), but also capture the full correction signal:

```sql
-- Add to crops table if not already present:
ALTER TABLE crops ADD COLUMN IF NOT EXISTS reviewed_by_human BOOLEAN DEFAULT FALSE;
ALTER TABLE crops ADD COLUMN IF NOT EXISTS ai_qa_status VARCHAR(20);      -- what the AI decided
ALTER TABLE crops ADD COLUMN IF NOT EXISTS human_qa_status VARCHAR(20);   -- what the human decided (null if not reviewed)
ALTER TABLE crops ADD COLUMN IF NOT EXISTS reviewer_user_id INTEGER REFERENCES users(id);
ALTER TABLE crops ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;

-- The qa_status column remains the authoritative status (human overrides AI)
-- ai_qa_status captures the original AI decision before any human override
```

Similarly for morphology labels — if a labeler corrects a model's predicted label, that correction is more valuable than a label on an unpredicted crop:

```sql
ALTER TABLE labels ADD COLUMN IF NOT EXISTS model_predicted_class VARCHAR(50);  -- what the model predicted (null if no model ran yet)
ALTER TABLE labels ADD COLUMN IF NOT EXISTS is_correction BOOLEAN DEFAULT FALSE; -- true if human label differs from model prediction
```

### 7.2 — The Four Correction Cases and Their Value

| Case | AI Decision | Human Decision | Signal Type | Training Value |
|------|------------|----------------|-------------|----------------|
| Agreement — accept | accepted | accepted (or not reviewed) | Positive confirmation | Standard |
| Agreement — reject | rejected | rejected (or not reviewed) | Negative confirmation | Standard |
| **False positive** | accepted | human marks rejected | AI accepted a bad crop | **High — hard negative** |
| **False negative** | rejected | human rescues to accepted | AI rejected a good crop | **High — hard positive** |

The correction cases (false positives and false negatives) are the most important training examples. The AI was wrong about these; they are the images that sit near the model's decision boundary and have the most gradient signal for retraining.

### 7.3 — API Endpoints: Capture the Correction Explicitly

When a reviewer changes a crop's status from what the AI assigned, the API should record it, not just overwrite:

```python
# In main.py — crop QA update endpoint
@app.patch("/api/crops/{crop_id}/qa")
async def update_crop_qa(crop_id: int, payload: CropQAUpdate, current_user=Depends(get_current_user), db=Depends(get_db)):
    crop = db.query(Crop).filter(Crop.id == crop_id).first()
    
    # Preserve the original AI decision if this is the first human review
    if not crop.reviewed_by_human and crop.ai_qa_status is None:
        crop.ai_qa_status = crop.qa_status  # snapshot AI's decision
    
    # Record the correction
    is_correction = (crop.ai_qa_status is not None and crop.ai_qa_status != payload.status)
    
    crop.qa_status = payload.status
    crop.human_qa_status = payload.status
    crop.reviewed_by_human = True
    crop.reviewer_user_id = current_user.id
    crop.reviewed_at = datetime.utcnow()
    
    db.commit()
    
    # Log to audit_log with correction flag for easy querying later
    log_audit(db, current_user.id, "crop_qa_update", "crop", crop_id, {
        "ai_decision": crop.ai_qa_status,
        "human_decision": payload.status,
        "is_correction": is_correction
    })
    
    return crop
```

### 7.4 — Retraining Trigger: When and What

Define a retraining policy for each model in the pipeline. Implement as a Celery task on the `training` queue:

```python
@celery_app.task(queue="training")
def maybe_retrain_qa_classifier(project_id: int):
    """
    Check if enough new human feedback has accumulated to warrant retraining
    the local QA binary classifier. Called automatically after each reviewer session.
    """
    db = get_db_session()
    
    # Count human-reviewed crops since last QA model training
    last_trained = get_last_qa_model_trained_at(db, project_id)
    new_reviews = db.query(Crop).filter(
        Crop.reviewed_by_human == True,
        Crop.reviewed_at > last_trained
    ).count()
    
    new_corrections = db.query(Crop).filter(
        Crop.reviewed_by_human == True,
        Crop.reviewed_at > last_trained,
        Crop.ai_qa_status != Crop.human_qa_status  # disagreements only
    ).count()
    
    # Retrain if: 50+ new reviews OR 10+ new corrections (corrections are higher signal)
    if new_reviews >= 50 or new_corrections >= 10:
        retrain_qa_classifier.delay(project_id)

@celery_app.task(queue="training")
def retrain_qa_classifier(project_id: int):
    """
    Build training set from all human-reviewed crops, weight corrections higher,
    train binary classifier, save as new model version.
    """
    db = get_db_session()
    
    # Get all human-reviewed crops
    reviewed = db.query(Crop).filter(Crop.reviewed_by_human == True).all()
    
    images, labels, weights = [], [], []
    for crop in reviewed:
        img = load_and_preprocess(crop.crop_path)
        label = 1 if crop.human_qa_status == "accepted" else 0
        # Corrections (human disagreed with AI) get higher weight
        is_correction = (crop.ai_qa_status != crop.human_qa_status)
        weight = 3.0 if is_correction else 1.0
        
        images.append(img)
        labels.append(label)
        weights.append(weight)
    
    train_binary_classifier(images, labels, weights, project_id)
```

### 7.5 — Retraining the YOLO Detector from Human QA

Human QA decisions are also supervision signal for the YOLO detector, not just the QA classifier. If a human marks a crop as `rejected` with reason `not_sperm`, the original bounding box was a false positive detection. If a human rescues a crop that was filtered out by the OpenCV pre-filter (before YOLO), that's a false negative.

Implement a periodic task that exports human-reviewed crops back into YOLO training format:

```python
@celery_app.task(queue="training")
def export_human_reviewed_as_yolo_annotations(project_id: int):
    """
    Export human-confirmed detections as YOLO bounding box annotations.
    These supplement the VISEM-Tracking training data with domain-specific examples.
    """
    db = get_db_session()
    
    confirmed_detections = db.query(Crop, Detection, Frame).join(...).filter(
        Crop.reviewed_by_human == True,
        Crop.human_qa_status == "accepted"  # human confirmed this is a real, good sperm
    ).all()
    
    rejected_as_not_sperm = db.query(Crop, Detection, Frame).join(...).filter(
        Crop.reviewed_by_human == True,
        Crop.human_qa_status.in_(["rejected", "not_sperm"])
    ).all()
    
    # Write YOLO format label files
    # confirmed -> positive examples (class 0 = sperm)
    # rejected_as_not_sperm -> negative examples (empty label file = background)
    write_yolo_dataset(confirmed_detections, rejected_as_not_sperm, output_dir="yolo_human_data/")
    
    # Combine with VISEM-Tracking for the next fine-tuning run
    # Run: yolo detect train data=combined.yaml model=current_best.pt epochs=20 lr0=0.001
```

### 7.6 — Morphology Label Corrections (Classification Model)

Once the classifier is running and making predictions, the labeling UI should show the model's prediction alongside the labeler's interface. When the labeler assigns a different class than the model predicted, flag it:

```python
# When saving a label, check against model prediction
@app.post("/api/crops/{crop_id}/label")
async def create_label(crop_id: int, payload: LabelCreate, ...):
    
    # Get the latest model prediction for this crop, if any
    latest_prediction = get_latest_prediction_for_crop(db, crop_id)
    is_correction = (
        latest_prediction is not None and 
        latest_prediction.predicted_class != payload.label_class
    )
    
    label = Label(
        crop_id=crop_id,
        label_class=payload.label_class,
        labeler_user_id=current_user.id,
        model_predicted_class=latest_prediction.predicted_class if latest_prediction else None,
        is_correction=is_correction
    )
    db.add(label)
    db.commit()
```

When retraining the morphology classifier, prioritize corrected labels:

```python
def build_training_dataset(db, project_id):
    labels = db.query(Label).join(Crop).filter(Crop.qa_status == "accepted").all()
    
    dataset = []
    for label in labels:
        weight = 3.0 if label.is_correction else 1.0  # corrections weighted higher
        dataset.append((label.crop.crop_path, label.label_class, weight))
    
    return dataset
```

### 7.7 — Dashboard: Make the Feedback Loop Visible

Add a simple stats endpoint (and surface it in the UI) so the team can see the feedback loop working:

```python
@app.get("/api/projects/{project_id}/feedback-stats")
async def get_feedback_stats(project_id: int, db=Depends(get_db)):
    return {
        "qa_reviews_total": db.query(Crop).filter(Crop.reviewed_by_human == True).count(),
        "qa_ai_human_agreement_rate": compute_agreement_rate(db, project_id),
        "qa_corrections": {
            "false_positives": count_corrections(db, project_id, "accepted", "rejected"),
            "false_negatives": count_corrections(db, project_id, "rejected", "accepted"),
        },
        "morphology_corrections": db.query(Label).filter(Label.is_correction == True).count(),
        "last_qa_model_retrained": get_last_qa_model_trained_at(db, project_id),
        "last_classifier_retrained": get_last_classifier_trained_at(db, project_id),
        "corrections_since_last_retrain": count_corrections_since(db, project_id),
    }
```

This gives the team visibility into model drift and signals when retraining is due.

---

## Priority 8 — Strong Vision API as Pseudo-Annotator (Dataset Amplification)

### The Core Idea

You have ~90 human-labeled crops (example number; replace with actual count). You need 500-2,000 to train a reliable classifier. Instead of waiting for humans to label everything manually, use a strong vision API (GPT-4o, Gemini 1.5 Pro) with those 90 human examples embedded directly in the prompt as few-shot references. The model then labels the remaining unlabeled crops at high quality. This is sometimes called **LLM-as-labeler** or **few-shot prompted annotation**.

This is meaningfully stronger than your current Gemini QA filter, which uses generic reference images and a generic prompt. The difference is:
- **Current QA filter:** static reference images, binary accept/reject, general prompt
- **Pseudo-annotator:** your own human-validated examples, structured output with confidence, per-class morphology labels, disagreement flagging

The resulting labels are not as trustworthy as direct human labels, but they are far better than nothing and can be used to bootstrap training immediately. Human-labeled data stays the ground truth; pseudo-annotated data is treated as a lower-trust tier.

### 8.1 — Label Tiers: How to Track Data Provenance

Introduce a `label_source` field to distinguish how each label was created. This is critical — you must never mix tiers silently:

```sql
-- Add to labels table
ALTER TABLE labels ADD COLUMN label_source VARCHAR(20) NOT NULL DEFAULT 'human';
-- Values: 'human', 'pseudo_gpt4o', 'pseudo_gemini', 'pseudo_ensemble', 'model_prediction'

ALTER TABLE labels ADD COLUMN label_confidence FLOAT;       -- model's self-reported confidence (0.0-1.0)
ALTER TABLE labels ADD COLUMN label_raw_response JSONB;     -- full API response for auditability
```

In `training_runs`, track which tiers were included:

```sql
-- In training_runs.config_json, always record:
{
  "label_sources_included": ["human", "pseudo_gpt4o"],
  "human_label_count": 90,
  "pseudo_label_count": 410,
  "pseudo_confidence_threshold": 0.85
}
```

### 8.2 — Prompt Engineering for QA Pseudo-Annotation

For the binary QA task (is this crop usable for labeling?), the few-shot prompt structure:

```python
import base64
from pathlib import Path

def encode_image(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")

def build_qa_prompt(human_accepted_paths: list[str], human_rejected_paths: list[str], target_path: str) -> list[dict]:
    """
    Build a few-shot prompt for binary QA classification.
    Use 6-10 human-validated examples per class (accepted/rejected).
    More examples = better calibration, but higher token cost.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert image quality assessor for sperm morphology microscopy. "
                "You will be shown reference images that a human expert has already classified as ACCEPTED "
                "(suitable for morphology analysis) or REJECTED (not suitable). "
                "Then you will assess a new image and classify it using the same criteria. "
                "An ACCEPTED image must have: exactly one sperm cell, the full tail visible and not cut off, "
                "the head in focus, no overlapping cells. "
                "Respond ONLY with valid JSON. No other text."
            )
        },
        {
            "role": "user",
            "content": (
                # Inject accepted examples
                [{"type": "text", "text": f"REFERENCE — ACCEPTED example {i+1}:"}] +
                [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_image(p)}", "detail": "low"}}
                 for i, p in enumerate(human_accepted_paths[:6])] +
                # Inject rejected examples
                [{"type": "text", "text": f"REFERENCE — REJECTED example {i+1}:"}] +
                [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_image(p)}", "detail": "low"}}
                 for i, p in enumerate(human_rejected_paths[:6])] +
                # Target image
                [
                    {"type": "text", "text": "Now classify this new image using the same criteria as above:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_image(target_path)}", "detail": "high"}},
                    {"type": "text", "text": (
                        'Respond with JSON only: {"verdict": "accepted" or "rejected", '
                        '"confidence": 0.0-1.0, '
                        '"reason": "one sentence", '
                        '"flags": ["multi_cell"|"no_tail"|"blurred"|"cut_off"|"not_sperm"|"none"]}'
                    )}
                ]
            )
        }
    ]
    return messages
```

**Model choice for QA pseudo-annotation:**
- `gpt-4o` — best accuracy, ~$0.003/image at `detail: low` for references + `detail: high` for target. Use for the initial batch.
- `gemini-1.5-pro` — comparable accuracy, slightly cheaper. Already integrated.
- `gpt-4o-mini` — 10× cheaper, meaningfully weaker on subtle cases. Use only for high-confidence easy cases as a pre-filter.

### 8.3 — Prompt Engineering for Morphology Pseudo-Annotation

For tail morphology classification, the few-shot prompt is more demanding because the classes are more subtle. Use more examples and include explicit visual descriptions alongside each reference image:

```python
MORPHOLOGY_CLASSES = {
    "normal_tail": "straight or gently curved, full length visible, smooth",
    "coiled_tail": "tail loops or spirals back on itself",
    "bent_tail": "sharp angle or kink in the tail, >45 degrees",
    "short_tail": "tail is significantly shorter than a normal tail",
    "other_abnormal": "any other tail defect not covered above",
}

def build_morphology_prompt(human_examples_by_class: dict[str, list[str]], target_path: str) -> list[dict]:
    """
    human_examples_by_class: {"normal_tail": [path1, path2, ...], "coiled_tail": [...], ...}
    Use 4-6 examples per class minimum. 8-10 per class for best results.
    """
    content = []
    
    # Class definitions
    content.append({"type": "text", "text": (
        "You are classifying sperm tail morphology. The classes and their visual descriptions are:\n" +
        "\n".join(f"- {cls}: {desc}" for cls, desc in MORPHOLOGY_CLASSES.items())
    )})
    
    # Few-shot examples per class
    for cls, paths in human_examples_by_class.items():
        content.append({"type": "text", "text": f"\nHuman-verified examples of '{cls}':"})
        for path in paths[:6]:
            content.append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{encode_image(path)}", "detail": "low"
            }})
    
    # Target
    content.extend([
        {"type": "text", "text": "\nNow classify this new image:"},
        {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{encode_image(target_path)}", "detail": "high"
        }},
        {"type": "text", "text": (
            'Respond with JSON only: {'
            '"class": one of the class names above, '
            '"confidence": 0.0-1.0, '
            '"second_choice": class name or null, '
            '"reason": "one sentence describing what you see in the tail"'
            '}'
        )}
    ])
    
    return [{"role": "user", "content": content}]
```

### 8.4 — Ensemble Agreement as a Confidence Gate

The key to making pseudo-labels trustworthy is requiring **agreement between two independent models** before accepting a label. Single-model labels at high stated confidence are still unreliable for subtle morphology; two models that independently agree are much more likely to be correct.

```python
import asyncio
import openai
import google.generativeai as genai

async def pseudo_annotate_crop(crop_path: str, examples: dict, task: str) -> dict:
    """
    Run two models independently, accept label only if they agree above threshold.
    task: "qa" or "morphology"
    """
    # Run both models concurrently
    gpt4o_result, gemini_result = await asyncio.gather(
        call_gpt4o(crop_path, examples, task),
        call_gemini_pro(crop_path, examples, task),
    )
    
    gpt4o_label = gpt4o_result["verdict" if task == "qa" else "class"]
    gemini_label = gemini_result["verdict" if task == "qa" else "class"]
    gpt4o_conf = gpt4o_result["confidence"]
    gemini_conf = gemini_result["confidence"]
    
    models_agree = (gpt4o_label == gemini_label)
    avg_confidence = (gpt4o_conf + gemini_conf) / 2
    
    return {
        "label": gpt4o_label if models_agree else None,
        "source": "pseudo_ensemble" if models_agree else None,
        "confidence": avg_confidence,
        "models_agree": models_agree,
        "gpt4o": gpt4o_result,
        "gemini": gemini_result,
        # Disagreements go to human review queue — do not auto-label
        "needs_human_review": not models_agree or avg_confidence < 0.80,
    }
```

**Routing logic:**

| Condition | Action |
|-----------|--------|
| Both models agree AND avg confidence ≥ 0.85 | Accept as `pseudo_ensemble` label |
| Both models agree AND confidence 0.70–0.85 | Accept as `pseudo_ensemble` but weight 0.5× in training |
| Models disagree OR confidence < 0.70 | Add to human review queue, do not auto-label |
| One model errors / times out | Fall back to single model with lower confidence threshold (0.90+) |

### 8.5 — Celery Task: Batch Pseudo-Annotation

```python
@celery_app.task(queue="processing", rate_limit="50/m")  # stay within API rate limits
def pseudo_annotate_batch(crop_ids: list[int], task: str, project_id: int):
    """
    task: "qa" or "morphology"
    Fetches human examples from DB, runs ensemble annotation, stores results.
    """
    db = get_db_session()
    
    # Fetch the human-validated reference examples
    if task == "qa":
        accepted_examples = get_human_accepted_crop_paths(db, project_id, limit=8)
        rejected_examples = get_human_rejected_crop_paths(db, project_id, limit=8)
        examples = {"accepted": accepted_examples, "rejected": rejected_examples}
    else:
        examples = get_human_examples_by_class(db, project_id, per_class=6)
    
    results = {"auto_labeled": 0, "queued_for_human": 0, "errors": 0}
    
    for crop_id in crop_ids:
        crop = db.query(Crop).filter(Crop.id == crop_id).first()
        try:
            result = asyncio.run(pseudo_annotate_crop(crop.crop_path, examples, task))
            
            if not result["needs_human_review"]:
                # Store pseudo-label
                if task == "qa":
                    crop.qa_status = result["label"]
                    crop.reviewed_by_human = False
                else:
                    label = Label(
                        crop_id=crop_id,
                        label_class=result["label"],
                        label_source="pseudo_ensemble",
                        label_confidence=result["confidence"],
                        label_raw_response=result,
                    )
                    db.add(label)
                results["auto_labeled"] += 1
            else:
                # Add to human review queue (flag in DB or a separate queue table)
                flag_for_human_review(db, crop_id, reason="pseudo_annotation_disagreement")
                results["queued_for_human"] += 1
                
        except Exception as e:
            log_error(e, crop_id)
            results["errors"] += 1
    
    db.commit()
    return results
```

### 8.6 — When to Run and What to Expect

**Trigger pseudo-annotation runs when:**
- A new batch of human labels is complete (e.g., human finishes labeling 20 crops → run pseudo-annotation on 200 more)
- After each human QA review session (e.g., human reviews 30 crops → pseudo-annotate 300 queued crops)
- Never run pseudo-annotation on crops where a human label already exists — human always wins

**Expected output ratios based on this approach:**
- QA pseudo-annotation: expect ~70-80% of crops to auto-label (agreement + confidence), ~20-30% to queue for human
- Morphology pseudo-annotation: harder task, expect ~50-65% auto-label, ~35-50% queued for human
- Disagreements queued for human are the *right* crops to prioritize — they are genuinely ambiguous

**Cost estimate (QA task, GPT-4o + Gemini Pro ensemble):**
- 6 reference images at `detail:low` ≈ 6 × 85 tokens each ≈ 510 tokens input per call
- 1 target image at `detail:high` ≈ 1,105 tokens
- ~1,700 tokens total per crop × 2 models ≈ 3,400 tokens
- At GPT-4o pricing (~$2.50/1M input tokens): **~$0.009 per crop**
- 500 crops ≈ **$4.50 total** — trivially cheap

Morphology task costs ~2× more due to more reference images but still under $20 for 500 crops.

### 8.7 — How Pseudo-Labels Flow Into Training — With Important Constraints

**The QA task and morphology task have fundamentally different risk profiles for pseudo-labeling. Treat them separately.**

**QA pseudo-labels (accept/reject): relatively safe to use in training.**
The task is binary and the errors are recoverable — a wrong QA label means a crop is misclassified as usable or unusable, which is caught when a human eventually reviews or when the morphology model's output looks wrong. Use QA pseudo-labels in training with reduced weight.

**Morphology pseudo-labels: use only for queue prioritization and weak signal, not as ground truth.**
A wrong morphology pseudo-label trains the classifier toward incorrect biology. The downstream harm is invisible — the model quietly learns the wrong thing, accuracy on the human-labeled test set drops, and diagnosing why is hard. The correct uses for morphology pseudo-labels are:

- **Queue prioritization:** use model/API confidence scores to decide which crops to send to human annotators first (low confidence = higher priority)
- **Weak regularization:** include with very low weight (0.1-0.2×) after you already have a working human-label baseline, as a regularizer only
- **Never:** as a substitute for human morphology labels, never in the test set, never as the majority of training data for morphology

```python
def build_training_dataset(db, project_id, task="morphology"):
    
    if task == "qa":
        # QA: pseudo-labels acceptable with reduced weight
        query = build_qa_query(db, project_id)
        dataset = []
        for label in query.all():
            if label.label_source == "human":
                weight = 3.0 if label.is_correction else 1.0
            elif label.label_source == "pseudo_ensemble" and label.label_confidence >= 0.85:
                weight = 0.5  # half-weight, not full
            else:
                weight = 0.0  # exclude low-confidence pseudo
            if weight > 0:
                dataset.append((label.crop.crop_path, label.qa_status, weight))
    
    elif task == "morphology":
        # Morphology: human labels only for real training
        # Pseudo-labels only for optional weak regularization after baseline is established
        human_labels = db.query(Label).filter(
            Label.label_source == "human",
            Label.consensus_status.in_(["agreement", "adjudicated", "single"])
        ).all()
        
        dataset = [(l.crop.crop_path, l.tail_normal, l.attributes, 
                    3.0 if l.is_correction else 1.0) 
                   for l in human_labels]
        
        # Optional: add pseudo-labels as weak regularization only AFTER baseline works
        # pseudo_labels = get_high_confidence_pseudo(db, project_id, min_conf=0.92)
        # for l in pseudo_labels:
        #     dataset.append((l.crop.crop_path, l.tail_normal, l.attributes, 0.1))
    
    return dataset
```

**Validation discipline — non-negotiable:** Evaluate on a **human-labels-only held-out test set** always, regardless of what was used in training. Never include pseudo-labels in the test or validation set. If your test set contains pseudo-labels you are measuring how well the model agrees with the API, not how well it performs on biology.

### 8.8 — Revised Rollout Sequence

The original sequence was too optimistic about morphology pseudo-annotation. Revised:

```
Phase 0 (now):       Human labels 30-50 QA examples (accepted + rejected)
Phase 1:             Run QA pseudo-annotation on queued crops for TRIAGE ONLY
                     → use to prioritize which crops humans should review next
                     → do not yet use as training labels
Phase 2:             Humans label 200+ morphology examples (human-only ground truth)
Phase 3:             Train first classifier on human labels only
                     → establish a baseline accuracy number on human test set
Phase 4:             Add QA pseudo-labels to QA classifier training at 0.5× weight
                     → measure if validation accuracy improves or degrades
Phase 5:             Only if Phase 4 improves things: cautiously add morphology
                     pseudo-labels at 0.1× weight as weak regularization
                     → measure on human test set; discard if no improvement
Phase 6:             Local model handles easy cases; API ensemble for uncertain cases only
```

The key difference: pseudo-labels for morphology earn their way in by demonstrating measurable improvement on the human test set, starting from a very low weight. They do not get added by default.

---

## Known Issues Not Addressed Here (For Later)

- `main.py` is 1,486 lines with all routes — consider splitting into APIRouter modules when the file becomes hard to navigate; not urgent
- No Docker — add when you need to replicate the environment on multiple machines
- No tests — add at minimum one integration test per Celery task before any clinical deployment
- GCP project ID `nimo-gpt` hardcoded in `ai_filter.py` — move to env var: `GOOGLE_CLOUD_PROJECT`

**Segmentation — keep explicitly on the near roadmap (v2), not a vague "someday":**

Crop-level CNN classification of tail morphology will hit a ceiling because the tail is a thin, elongated structure and crop-level models lose geometric detail. Segmentation/skeletonization is the right long-term tool. This does not block v1. But the *data decisions you make now* either preserve or permanently close off segmentation later. Specifically:

- **Save lossless master crops** — store the full-resolution padded crop as a separate file alongside the JPEG used for labeling. JPEG compression destroys fine tail edge detail that segmentation needs. One additional field in `crops`: `master_crop_path VARCHAR` pointing to a lossless PNG.
- **Preserve representative track crops** — the per-track deduplication from Priority 1b naturally gives you the best-focus image of each unique cell. This is exactly the subset worth mask-annotating later.
- **Flag a small subset for future mask annotation** — add a boolean `crops.flagged_for_segmentation`. When a human reviewer sees a crop with an unusually clear, fully visible tail, they can flag it. Accumulate 200-300 such crops passively while doing normal QA review. When v2 begins, you have a pre-selected segmentation annotation queue rather than having to re-review everything.

The marginal cost of doing this now: one extra file write per accepted crop, one extra column, one extra checkbox in the reviewer UI. The cost of not doing it: re-process all videos and re-annotate when v2 arrives.

---

## Summary Table

| Item | Action | Effort | Impact |
|------|--------|--------|--------|
| Security: JWT/admin/CORS | Fix env vars | 30 min | Critical |
| Add provenance columns (patient_id, sample_id, session_id, track_id) | Schema migration | 1 hour | Critical — do before any labeling |
| YOLO detector | Train on VISEM-Tracking, integrate | 1-2 days | Very High |
| Add ByteTrack tracking to detection pipeline | Enable track=True in YOLO, store track_id | 3 hours | Very High — prevents duplicate labels |
| Per-track representative crop selection | Select best focus_score crop per track | 2 hours | High |
| Patient-level dataset split | GroupShuffleSplit by patient_id | 2 hours | High — honest accuracy metrics |
| Proportional padding | 3-line formula change | 30 min | Medium |
| Celery queue split | Add queue= params + 2nd worker | 1 hour | Medium |
| Multilabel schema (tail_normal + attribute flags) | Schema migration + BCEWithLogitsLoss in trainer | 4 hours | High — correct data model for morphology |
| Separate QA flags from morphology labels in UI | UI + API change | 1 day | High — cleaner labeling |
| Consensus labeling workflow | 2nd annotator on uncertain crops, adjudication | Process, not code | High — test set integrity |
| Class-weighted / attribute-weighted loss | pos_weight in BCEWithLogitsLoss | 15 min | High |
| Full-rotation augmentation | Add to transforms | 15 min | High |
| Taxonomy review with embryologist | Call | 1 hour | High |
| Download VISEM-Tracking | wget from Zenodo | 10 min | High (enables YOLO) |
| Local binary QA classifier | After 500 human-reviewed crops | 1 day | Medium (cost savings) |
| Track ai_qa_status + human_qa_status in DB | Schema migration + API update | 2 hours | High (enables all of Priority 7) |
| Weight corrections in QA classifier training | 1 line per sample in training loop | 30 min | High |
| Auto-retrain trigger (Celery task) | New task: check correction count, fire retrain | 3 hours | High |
| Export human QA as YOLO annotations | New Celery task + yolo fine-tune script | 4 hours | High (improves detector over time) |
| Show model prediction in labeling UI | Fetch prediction before rendering label UI | 2 hours | High (captures morphology corrections) |
| Feedback stats endpoint + UI widget | New API endpoint + small React component | 3 hours | Medium (visibility) |
| label_source + label_confidence columns | Schema migration | 30 min | Critical (enables all of Priority 8) |
| QA pseudo-annotation (ensemble, triage only) | Prompt builder + async API calls + Celery task | 1 day | High (queue prioritization, not ground truth) |
| Morphology pseudo-annotation | Use only for queue ordering; earned into training at 0.1× only after baseline | — | Low until baseline established |
| Confidence-gated routing (agree/disagree) | Logic layer in pseudo_annotate_crop() | 2 hours | High (keeps pseudo-labels honest) |
| Human-only test set enforcement | Filter in dataset builder + enforce in code | 1 hour | Critical (honest accuracy measurement) |
| Phase 6 local+API hybrid inference | Route low-confidence crops to API ensemble | 1 day | High (long-term cost reduction) |
| Save lossless master crop (PNG) alongside JPEG | One extra file write + master_crop_path column | 2 hours | High — preserves v2 segmentation option |
| Flag crops for future segmentation | Boolean column + checkbox in reviewer UI | 1 hour | Low cost now, high value at v2 |
