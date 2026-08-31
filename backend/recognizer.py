"""Face recognition with OpenCV SFace.

LICENSING - why this is SFace and not ArcFace.
The application is deployed as a network service, so every model must permit
commercial use. The previous ensemble could not:

    glintr100, w600k_r50   InsightFace - "non-commercial research purposes only"
    adaface_ir101          weights trained on WebFace4M, academic research terms

SFace is Apache-2.0, published in the OpenCV Zoo and loaded through OpenCV's own
DNN module, so there is no obligation beyond attribution and no extra runtime
dependency.

WHAT THE SWAP COSTS, measured on this project's data rather than assumed:

    ground-truth photo, frame 1      13/13   (previous stack: 13/13)
    ground-truth photo, frame 2      13/13   (previous stack: 13/13)
    weightlifting centre             6/22    (previous stack: 6/22)
    strangers control                0       (previous stack: 0)
    time per group photo             0.5 s   (previous stack: 13 s)
    model weights                    37 MB   (previous stack: 1,022 MB)

Identical accuracy on every test, 25x faster, 27x smaller.

OPERATING POINT. SFace's published threshold of 0.363 is tuned for 1:1
verification. Attendance is open-set - most faces in a group photo belong to
nobody enrolled - and at 0.363 six strangers were accepted. Measured here, the
clean point is 0.55, which rejects every stranger while still identifying
everyone genuinely present. A threshold sweep that stopped at 0.50 would have
concluded the model was unusable; see scripts/permissive_bench.py.

EMBEDDINGS are 128-dimensional and L2-normalised, so cosine similarity is a dot
product. This is NOT interchangeable with the previous 512-dimensional ArcFace
vectors - a database enrolled under the old stack must be re-enrolled.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import config

log = logging.getLogger("recognizer")

EMBED_DIM = 128


def estimate_landmarks_for_crop(shape) -> np.ndarray:
    from .detector import estimate_landmarks
    h, w = shape[:2]
    return estimate_landmarks((0, 0, w, h))


class SFaceRecognizer:
    """One SFace network, with alignment handled by OpenCV.

    SFace ships its own aligner: `alignCrop` warps using the five landmarks the
    detector produced, into exactly the geometry the network was trained on.
    Using it rather than a hand-rolled warp removes a whole class of alignment
    bugs - the previous pipeline's similarity transform had to be kept in sync
    with the model's expectations by hand.
    """

    def __init__(self, model_path):
        self.path = str(model_path)
        self._lock = threading.Lock()
        self._net = cv2.FaceRecognizerSF.create(self.path, "")
        self.name = model_path.name
        self.weight = 1.0

    def _align(self, img_bgr: np.ndarray, face) -> np.ndarray:
        raw = getattr(face, "raw", None)
        if raw is not None and len(raw) >= 15:
            return self._net.alignCrop(img_bgr, raw.reshape(1, -1))
        # A face reconstructed without detection metadata (an assigned crop, a
        # restored image): rebuild the row SFace expects from box + landmarks.
        lm = face.landmarks
        if lm is None:
            lm = estimate_landmarks_for_crop(img_bgr.shape)
        x1, y1, x2, y2 = face.box
        row = np.zeros((1, 15), dtype=np.float32)
        row[0, 0:4] = [x1, y1, x2 - x1, y2 - y1]
        row[0, 4:14] = np.asarray(lm, dtype=np.float32).reshape(-1)
        row[0, 14] = 1.0
        return self._net.alignCrop(img_bgr, row)

    def embed_faces(self, img_bgr: np.ndarray, faces) -> np.ndarray:
        """-> (N, 128) L2-normalised embeddings."""
        if not faces:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        out = []
        with self._lock:
            for f in faces:
                try:
                    aligned = self._align(img_bgr, f)
                    v = self._net.feature(aligned).flatten().astype(np.float32)
                except cv2.error as e:      # unusable crop; keep the row aligned
                    log.warning("SFace embedding failed for one face: %s", e)
                    v = np.zeros(EMBED_DIM, dtype=np.float32)
                out.append(v / (np.linalg.norm(v) + 1e-10))
        return np.stack(out)

    def embed_aligned(self, crops: List[np.ndarray]) -> np.ndarray:
        """Embed images that are already face crops, with no detection behind them."""
        if not crops:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        from .detector import Face
        out = []
        with self._lock:
            for c in crops:
                h, w = c.shape[:2]
                f = Face(box=(0, 0, w, h), conf=1.0,
                         landmarks=estimate_landmarks_for_crop(c.shape), source="crop")
                try:
                    v = self._net.feature(self._align(c, f)).flatten().astype(np.float32)
                except cv2.error:
                    v = np.zeros(EMBED_DIM, dtype=np.float32)
                out.append(v / (np.linalg.norm(v) + 1e-10))
        return np.stack(out)


class EnsembleRecognizer:
    """Kept as a single-member ensemble so callers are unchanged by the swap.

    The previous implementation fused three networks. One Apache-2.0 model
    matches that accuracy on every measured case, so the fusion machinery is
    gone; the shape of the interface is not, because main.py, the evaluation
    harness and the enrolment paths all speak it.
    """

    def __init__(self):
        path = config.MODELS_DIR / config.SFACE_MODEL
        if not path.exists():
            raise FileNotFoundError(
                f"SFace model missing: {path}. Run: python -m scripts.download_models"
            )
        self.models = [SFaceRecognizer(path)]
        log.info("Recognizer: %s (Apache-2.0, %d-d)", config.SFACE_MODEL, EMBED_DIM)

    @property
    def label(self) -> str:
        return " + ".join(m.name for m in self.models)

    def embed_faces(self, img_bgr: np.ndarray, faces) -> Dict[str, np.ndarray]:
        return {m.name: m.embed_faces(img_bgr, faces) for m in self.models}

    def embed_aligned(self, crops: List[np.ndarray]) -> Dict[str, np.ndarray]:
        return {m.name: m.embed_aligned(crops) for m in self.models}

    def embed_single(self, img_bgr: np.ndarray, face) -> Dict[str, np.ndarray]:
        return self.embed_faces(img_bgr, [face])


def pool_to_students(
    sims_qt: np.ndarray, template_student_ids: np.ndarray, all_ids: List[int]
) -> Tuple[np.ndarray, np.ndarray]:
    """Max-pool (Q,T) template similarities into (Q,N) student columns.

    A student owns several templates - registration photo, and one per view from
    guided capture - and their score is the best over all of them, so an old
    reference and a current one are both represented.
    """
    n_faces, n_students = sims_qt.shape[0], len(all_ids)
    col_of = {int(sid): j for j, sid in enumerate(all_ids)}
    cols = np.array([col_of[int(sid)] for sid in template_student_ids], dtype=int)
    pooled = np.full((n_faces, n_students), -1.0, dtype=np.float64)
    for q in range(n_faces):
        np.maximum.at(pooled[q], cols, sims_qt[q])
    covered = np.zeros(n_students, dtype=bool)
    covered[cols] = True
    return pooled, covered


def fuse_scores(
    queries: Dict[str, np.ndarray],
    gallery: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
    weights: Dict[str, float],
    feature_queries: Optional[np.ndarray] = None,
    feature_gallery: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None,
) -> Tuple[Optional[np.ndarray], List[int]]:
    """Similarity of each query face against the multi-template gallery.

    gallery: {model_name: (template_ids (T,), student_ids (T,), matrix (T,D))}.
    Returns (sims (Q,N) or None, sorted student ids). Students with no usable
    template score -1.
    """
    usable = [name for name in queries if name in gallery and len(queries[name])]
    if not usable:
        return None, []
    all_ids = sorted({int(sid) for name in usable for sid in gallery[name][1]})
    n_faces = len(next(iter(queries.values())))

    fused = np.zeros((n_faces, len(all_ids)), dtype=np.float64)
    wsum = np.zeros(len(all_ids), dtype=np.float64)
    for name in usable:
        wi = weights.get(name, 1.0)
        _, tmpl_student_ids, mat = gallery[name]
        if mat.shape[1] != queries[name].shape[1]:
            # A gallery built under the previous 512-d stack cannot be compared
            # with 128-d SFace vectors. Say so rather than raising deep inside
            # a matrix multiply.
            log.error(
                "Template dimension mismatch for %s: gallery %d-d, queries %d-d. "
                "The gallery predates the SFace migration - re-enrol with "
                "scripts/reenroll_sface.py.",
                name, mat.shape[1], queries[name].shape[1],
            )
            continue
        sims = queries[name] @ mat.T
        pooled, covered = pool_to_students(sims, tmpl_student_ids, all_ids)
        fused[:, covered] += wi * pooled[:, covered]
        wsum[covered] += wi
    valid = wsum > 0
    if not valid.any():
        return None, []
    fused[:, valid] /= wsum[valid]
    fused[:, ~valid] = -1.0
    return fused, all_ids


def fused_similarity_to_student(
    queries: Dict[str, np.ndarray],
    gallery: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
    weights: Dict[str, float],
    student_id: int,
) -> float:
    """Similarity of a single query embedding against one student."""
    sims, ws = [], []
    for name, vec in queries.items():
        if name not in gallery or not len(vec):
            continue
        _, tmpl_student_ids, mat = gallery[name]
        if mat.shape[1] != vec.shape[1]:
            continue
        mask = tmpl_student_ids == student_id
        if not mask.any():
            continue
        sims.append(float((vec[0] @ mat[mask].T).max()))
        ws.append(weights.get(name, 1.0))
    if not sims or sum(ws) <= 0:
        return -1.0
    return float(np.average(sims, weights=ws))


_recognizer: Optional[EnsembleRecognizer] = None


def get_recognizer() -> EnsembleRecognizer:
    global _recognizer
    if _recognizer is None:
        _recognizer = EnsembleRecognizer()
    return _recognizer
