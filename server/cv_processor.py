"""Compute Mouth Aspect Ratio (MAR) from a JPEG frame using MediaPipe Face Mesh.

MAR = vertical_lip_distance / horizontal_lip_distance

Landmark indices used (same as classic Face Mesh numbering):
  13  — upper lip centre
  14  — lower lip centre
  61  — left lip corner
  291 — right lip corner

Normalising by mouth width (rather than interocular distance) means the metric
works even when only the mouth region is visible to the camera.  Both distances
scale proportionally with camera distance, so MAR is scale-invariant.

Returns None when no face is detected so callers can write an empty CSV cell
rather than a spurious 0.0 that would corrupt amplitude statistics.

MediaPipe ≥ 0.10 removed the legacy `mp.solutions` API.  This module uses the
Tasks API (FaceLandmarker) with a bundled .task model file instead.  The model
file path is resolved relative to this file so the server can be started from
any working directory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import cv2
import mediapipe as mp
import numpy as np

# Path to the bundled model — lives at the repo root alongside this file.
_MODEL_PATH = Path(__file__).parent.parent / "face_landmarker.task"

_VisionRunningMode   = mp.tasks.vision.RunningMode
_FaceLandmarker      = mp.tasks.vision.FaceLandmarker
_FaceLandmarkerOpts  = mp.tasks.vision.FaceLandmarkerOptions
_BaseOptions         = mp.tasks.BaseOptions

# IMAGE mode processes one frame at a time (no async callback needed).
# Confidence thresholds lowered to 0.3: the Pi camera at 320x240 / JPEG 50 produces
# compressed frames where the default 0.5 threshold rejects valid detections.
_options = _FaceLandmarkerOpts(
    base_options=_BaseOptions(model_asset_path=str(_MODEL_PATH)),
    running_mode=_VisionRunningMode.IMAGE,
    num_faces=1,
    min_face_detection_confidence=0.3,
    min_face_presence_confidence=0.3,
    min_tracking_confidence=0.3,
)

_face_landmarker = _FaceLandmarker.create_from_options(_options)

# Landmark indices (MediaPipe Face Mesh 478-point topology)
_UPPER_LIP   = 13
_LOWER_LIP   = 14
_LEFT_CORNER  = 61
_RIGHT_CORNER = 291


def compute_nhm(jpeg_bytes: bytes) -> Optional[tuple[float, float]]:
    """Return (vertical, horizontal) mouth opening relative to frame size, or None if no face.

    Both components use MediaPipe's already-normalised landmark coordinates (range [0, 1]):
      vertical   = |y_upper_lip - y_lower_lip|   (fraction of frame height)
      horizontal = |x_left_corner - x_right_corner| (fraction of frame width)
    """
    img = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None

    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=img_rgb)

    result = _face_landmarker.detect(mp_image)
    if not result.face_landmarks:
        return None

    lm = result.face_landmarks[0]
    vertical   = abs(lm[_UPPER_LIP].y  - lm[_LOWER_LIP].y)
    horizontal = abs(lm[_LEFT_CORNER].x - lm[_RIGHT_CORNER].x)
    if horizontal < 1e-6:
        return None
    return (float(vertical), float(horizontal))
