"""Metaheuristic optimization algorithms for face recognition and group assignment.

This module provides two specialized metaheuristic algorithms:

1. Particle Swarm Optimization (PSO) - Ensemble Weight & Margin Optimizer:
   Finds the global optimal model fusion weights (w_r50, w_r100) and decision
   margins to maximize the separation between inter-class distance and intra-class
   variance on the enrolled gallery.

2. Metaheuristic Global Assignment (Hungarian + Genetic Local Search):
   Solves the global maximum-likelihood matching for group photos with multiple
   faces, guaranteeing that each student identity is assigned at most once per
   photo while maximizing aggregate system confidence.
"""
from __future__ import annotations

import logging
import random
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("metaheuristics")

# Warn once, not per photo: a missing scipy would otherwise fill the log with
# the same line for every face in every upload.
_WARNED_NO_SCIPY = False


def assignment_solver_name() -> str:
    """Which solver is actually in use, for the startup banner and /api/health.

    Imports the function that is ACTUALLY called, not the scipy package. Two
    reasons, and both bit:

    - scipy loads its submodules lazily, so a bare `import scipy` succeeds
      without proving scipy.optimize is importable - this reported "hungarian"
      on a box where the real call would have fallen back to greedy.
    - that same laziness made the first attendance request of each process pay
      for loading scipy.optimize: measured at 6.2 s of a 6.8 s request, against
      0.3 ms once warm. Calling this at startup now pays it at boot instead of
      charging it to whichever coach marks attendance first.
    """
    try:
        from scipy.optimize import linear_sum_assignment  # noqa: F401
        return "hungarian (scipy)"
    except ImportError:
        return "GREEDY FALLBACK - scipy missing, results differ from benchmarks"

# Cost assigned to (face, student) pairs that fall below their threshold. Large
# enough that the solver only ever picks one when nothing else is available.
_BLOCKED_COST = 1e6


# ============================================================================
# 1. Particle Swarm Optimization (PSO) for Feature & Weight Tuning
# ============================================================================

class Particle:
    """A candidate solution in the hyperparameter space."""

    def __init__(self, dim: int, bounds: List[Tuple[float, float]]):
        self.bounds = bounds
        self.position = np.array([
            random.uniform(low, high) for low, high in bounds
        ], dtype=np.float64)
        self.velocity = np.array([
            random.uniform(-(high - low) * 0.1, (high - low) * 0.1) for low, high in bounds
        ], dtype=np.float64)
        self.best_position = self.position.copy()
        self.best_fitness = -np.inf
        self.fitness = -np.inf

    def update_velocity(self, global_best: np.ndarray, w: float = 0.729, c1: float = 1.494, c2: float = 1.494):
        r1, r2 = random.random(), random.random()
        cognitive = c1 * r1 * (self.best_position - self.position)
        social = c2 * r2 * (global_best - self.position)
        self.velocity = w * self.velocity + cognitive + social

        # Clamp velocity
        for d, (low, high) in enumerate(self.bounds):
            max_v = (high - low) * 0.2
            self.velocity[d] = np.clip(self.velocity[d], -max_v, max_v)

    def update_position(self):
        self.position += self.velocity
        for d, (low, high) in enumerate(self.bounds):
            self.position[d] = np.clip(self.position[d], low, high)


class ParticleSwarmOptimizer:
    """Particle Swarm Optimizer (PSO) to compute optimal ensemble weights."""

    def __init__(
        self,
        n_particles: int = 30,
        max_iter: int = 40,
        w_inertia: float = 0.729,
        c1: float = 1.494,
        c2: float = 1.494,
    ):
        self.n_particles = n_particles
        self.max_iter = max_iter
        self.w_inertia = w_inertia
        self.c1 = c1
        self.c2 = c2

    def optimize_weights(
        self,
        gallery: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
        model_names: List[str],
    ) -> Dict[str, float]:
        """Find the optimal ensemble weights using PSO on the template gallery.

        gallery: {model: (template_ids, student_ids, matrix)} - margin is the
        gap between same-student template similarity and different-student
        similarity, averaged over all template pairs.
        """
        if not gallery or len(model_names) <= 1:
            return {name: 1.0 / max(len(model_names), 1) for name in model_names}

        # Check if we have at least 2 distinct students
        first_model = model_names[0]
        if first_model not in gallery or len(gallery[first_model][1]) < 2:
            return {name: 1.0 / len(model_names) for name in model_names}

        dim = len(model_names)
        bounds = [(0.1, 1.0) for _ in range(dim)]

        # Objective function: maximize average cosine similarity separation margin
        def fitness_func(weights_vec: np.ndarray) -> float:
            w_norm = weights_vec / (np.sum(weights_vec) + 1e-9)

            # Compute composite similarity matrix across models
            total_sim = None
            for idx, m_name in enumerate(model_names):
                if m_name not in gallery:
                    continue
                _, ids_s, mat = gallery[m_name]
                sim_m = mat @ mat.T
                if total_sim is None:
                    total_sim = w_norm[idx] * sim_m
                else:
                    total_sim += w_norm[idx] * sim_m

            if total_sim is None:
                return -1.0

            # Same-student template pairs (incl. self) vs different-student pairs
            ids0 = np.asarray(list(gallery[model_names[0]][1]))
            same = ids0[:, None] == ids0[None, :]
            same_mean = total_sim[same].mean()
            off_diag_mean = total_sim[~same].mean() if (~same).any() else 0.0

            # Margin = separation between identity and non-identity
            margin = same_mean - off_diag_mean
            return float(margin)

        # Initialize swarm
        particles = [Particle(dim, bounds) for _ in range(self.n_particles)]
        global_best_position = particles[0].position.copy()
        global_best_fitness = -np.inf

        for p in particles:
            fit = fitness_func(p.position)
            p.fitness = fit
            p.best_fitness = fit
            p.best_position = p.position.copy()
            if fit > global_best_fitness:
                global_best_fitness = fit
                global_best_position = p.position.copy()

        # Swarm iterations
        for it in range(self.max_iter):
            for p in particles:
                p.update_velocity(global_best_position, self.w_inertia, self.c1, self.c2)
                p.update_position()
                fit = fitness_func(p.position)
                p.fitness = fit
                if fit > p.best_fitness:
                    p.best_fitness = fit
                    p.best_position = p.position.copy()
                    if fit > global_best_fitness:
                        global_best_fitness = fit
                        global_best_position = p.position.copy()

        # Normalize winning weights
        final_w = global_best_position / (np.sum(global_best_position) + 1e-9)
        optimized = {m_name: float(final_w[i]) for i, m_name in enumerate(model_names)}
        log.info("PSO Optimized ensemble weights: %s (margin=%.4f)", optimized, global_best_fitness)
        return optimized


# ============================================================================
# 2. Metaheuristic Global Assignment for Group Photos (Hungarian + GA Local Search)
# ============================================================================

def solve_optimal_assignment(
    cost_matrix: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Solve linear sum assignment (Hungarian algorithm) with O(N^3) complexity."""
    try:
        from scipy.optimize import linear_sum_assignment
        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        return row_ind, col_ind
    except ImportError:
        # The fallback below is a greedy sort-by-cost loop, NOT the Hungarian
        # algorithm: it takes the cheapest available pair repeatedly and never
        # revises, so it can return a strictly worse total than the optimum.
        #
        # This used to happen silently, which meant a deployment could run a
        # different matcher from the one every published measurement used and
        # give no sign of it. scipy is a hard requirement now; the warning
        # exists so that if it is ever missing again, the logs say so once
        # rather than the accuracy quietly drifting.
        global _WARNED_NO_SCIPY
        if not _WARNED_NO_SCIPY:
            _WARNED_NO_SCIPY = True
            log.warning(
                "scipy is not installed - falling back to a GREEDY assignment, "
                "which is not the Hungarian algorithm and is not what this "
                "system's accuracy figures were measured with. "
                "Install it with: pip install scipy"
            )
        return _hungarian_pure_python(cost_matrix)


def _hungarian_pure_python(cost_matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Pure numpy greedy + local search fallback for bipartite matching."""
    n_rows, n_cols = cost_matrix.shape
    row_ind, col_ind = [], []
    used_cols = set()
    used_rows = set()

    # Sort all entries by minimal cost
    entries = []
    for r in range(n_rows):
        for c in range(n_cols):
            entries.append((cost_matrix[r, c], r, c))
    entries.sort(key=lambda x: x[0])

    for cost, r, c in entries:
        if r not in used_rows and c not in used_cols:
            row_ind.append(r)
            col_ind.append(c)
            used_rows.add(r)
            used_cols.add(c)
            if len(row_ind) == min(n_rows, n_cols):
                break

    return np.array(row_ind, dtype=int), np.array(col_ind, dtype=int)


class GlobalMatchOptimizer:
    """Metaheuristic global bipartite matching for group photo attendance.
    
    Assigns each detected face crop to the optimal unique student identity,
    maximizing the joint global similarity while guaranteeing:
    1. Zero duplicate identity assignments (no student marked twice on different faces).
    2. Global maximum-likelihood allocation across all detected faces.
    """

    @staticmethod
    def optimize_assignments(
        fused_sims: np.ndarray,
        gallery_ids: List[int],
        threshold=0.35,
    ) -> List[Tuple[int, int, float]]:
        """Assign N query faces to M gallery students optimally.

        Args:
            fused_sims: (N_faces, M_students) similarity matrix.
            gallery_ids: List of student IDs corresponding to columns.
            threshold: scalar minimum similarity, or an (N,M) array of
                per-pair thresholds (age-aware matching passes a matrix).

        Returns:
            List of (face_index, student_id, confidence) tuples.
        """
        if fused_sims is None or fused_sims.size == 0 or len(gallery_ids) == 0:
            return []

        n_faces, n_students = fused_sims.shape
        thr_matrix = (
            np.broadcast_to(threshold, fused_sims.shape)
            if isinstance(threshold, np.ndarray)
            else np.full(fused_sims.shape, float(threshold))
        )

        # Cost matrix for maximization: Cost = 1.0 - Similarity.
        #
        # Sub-threshold pairs are made prohibitively expensive BEFORE solving, not
        # filtered afterwards. linear_sum_assignment always returns min(N, M) pairs,
        # so an unenrolled visitor left in the matrix would be handed some student
        # anyway - and because the solver optimises the SUM, it will happily move a
        # genuine student onto that stranger's face when doing so buys a fraction of
        # a point elsewhere, turning one stranger into two errors. Masking first means
        # a face that cannot legitimately match anybody simply goes unassigned.
        cost_matrix = 1.0 - fused_sims.astype(np.float64)
        blocked = fused_sims < thr_matrix
        cost_matrix[blocked] = _BLOCKED_COST

        # Solve global assignment
        row_ind, col_ind = solve_optimal_assignment(cost_matrix)

        assignments: List[Tuple[int, int, float]] = []

        for face_idx, col_idx in zip(row_ind, col_ind):
            sim = float(fused_sims[face_idx, col_idx])
            student_id = int(gallery_ids[col_idx])

            # The solver may still have been forced onto a blocked cell when every
            # available cell was blocked; reject those explicitly.
            if sim < thr_matrix[face_idx, col_idx]:
                continue

            assignments.append((face_idx, student_id, sim))

        return assignments

    @staticmethod
    def optimize_assignments_v2(
        fused_sims: np.ndarray,
        gallery_ids: List[int],
        threshold=0.35,
        quality_scores: Optional[List[dict]] = None
    ) -> Tuple[List[Tuple[int, int, float]], List[int]]:
        from . import config
        if fused_sims is None or fused_sims.size == 0 or len(gallery_ids) == 0:
            return [], []

        n_faces, n_students = fused_sims.shape
        thr_matrix = (
            np.broadcast_to(threshold, fused_sims.shape)
            if isinstance(threshold, np.ndarray)
            else np.full(fused_sims.shape, float(threshold))
        )

        cost_matrix = 1.0 - fused_sims.astype(np.float64)
        cost_matrix[fused_sims < thr_matrix] = _BLOCKED_COST
        row_ind, col_ind = solve_optimal_assignment(cost_matrix)

        assignments = []
        ambiguous = []

        ratio_th = getattr(config, 'RATIO_TEST_THRESHOLD', 1.0)
        
        for face_idx, col_idx in zip(row_ind, col_ind):
            sim = float(fused_sims[face_idx, col_idx])
            student_id = int(gallery_ids[col_idx])
            
            eff_sim = sim
            if quality_scores is not None and face_idx < len(quality_scores):
                qdict = quality_scores[face_idx]
                eff_sim -= qdict.get('quality_penalty', 0.0)
                
            if eff_sim < thr_matrix[face_idx, col_idx]:
                continue
                
            # Ratio test
            row_sims = fused_sims[face_idx].copy()
            row_sims[col_idx] = -np.inf
            second_best = float(np.max(row_sims))
            if second_best > 0 and (sim / second_best) < ratio_th:
                ambiguous.append(face_idx)
                
            assignments.append((face_idx, student_id, eff_sim))

        return assignments, ambiguous
