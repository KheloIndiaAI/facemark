import cv2, numpy as np
from backend import database, config
from backend.detector import get_detector
from backend.recognizer import get_recognizer, pool_to_students
from backend.metaheuristics import GlobalMatchOptimizer

det = get_detector()
rec = get_recognizer()
gallery, quality_dict = database.load_gallery_with_quality()
weights = {m.name: m.weight for m in rec.models}
all_ids = sorted({int(sid) for name in gallery for sid in gallery[name][1]})

def run_test(img_path, label):
    img = cv2.imread(img_path)
    faces = det.detect(img, 'fused')
    queries = rec.embed_faces(img, faces)
    
    raw_fused = np.zeros((len(faces), len(all_ids)), dtype=np.float64)
    wsum = np.zeros(len(all_ids), dtype=np.float64)
    for m in rec.models:
        name = m.name
        if name not in queries or name not in gallery: continue
        wi = m.weight
        _, tmpl_student_ids, mat = gallery[name]
        sims = queries[name] @ mat.T
        pooled, covered = pool_to_students(sims, tmpl_student_ids, all_ids)
        raw_fused[:, covered] += wi * pooled[:, covered]
        wsum[covered] += wi
    valid = wsum > 0
    raw_fused[:, valid] /= wsum[valid]
    
    # Adaptive per-face threshold matrix
    # Standard threshold = 0.45.
    # If face is high quality (w >= 35px) and ratio <= 0.60, relaxed threshold = 0.36
    thr_matrix = np.full(raw_fused.shape, 0.45, dtype=np.float64)
    for i, f in enumerate(faces):
        sorted_sims = np.sort(raw_fused[i])[::-1]
        best_sim = sorted_sims[0]
        sec_sim = sorted_sims[1] if len(sorted_sims) > 1 else 0.0
        ratio = sec_sim / max(best_sim, 1e-6)
        
        # If face is sufficiently large and has a very strong margin over 2nd candidate:
        if f.width >= 35.0 and ratio <= 0.65:
            thr_matrix[i, :] = 0.36
        elif f.width < 30.0:
            # Small crowd faces require stricter threshold
            thr_matrix[i, :] = 0.48

    config.RATIO_TEST = True
    config.RATIO_TEST_THRESHOLD = 0.85
    matches = GlobalMatchOptimizer.optimize_assignments(raw_fused, gallery_ids=all_ids, threshold=thr_matrix)
    match_dict = {f_idx: (sid, sim) for f_idx, sid, sim in matches}
    
    print("\n" + "="*60)
    print(f"{label}: {len(faces)} faces | {len(matches)} recognized | {len(faces)-len(matches)} unknown")
    print("="*60)
    for i, f in enumerate(faces):
        if i in match_dict:
            sid, sim = match_dict[i]
            st = database.get_student(sid)
            sname = st['name'] if st else str(sid)
            print(f"  Face {i+1:2d} (w={f.width:4.1f}px): MATCH -> {sname:<15} (sim={sim:.4f})")
        else:
            sorted_sims = np.sort(raw_fused[i])[::-1]
            best_sim = sorted_sims[0]
            sec_sim = sorted_sims[1] if len(sorted_sims) > 1 else 0.0
            ratio = sec_sim / max(best_sim, 1e-6)
            best_sid = all_ids[np.argmax(raw_fused[i])]
            st = database.get_student(best_sid)
            sname = st['name'] if st else str(best_sid)
            print(f"  Face {i+1:2d} (w={f.width:4.1f}px): UNKNOWN (best={sname} {best_sim:.3f}, ratio={ratio:.2f})")

run_test('data/uploads/group_20260820_160116_156.jpg', 'UNKNOWN PHOTO (Delhi Mallakhamb Association)')
run_test('data/uploads/group_20260820_132732_813.jpg', 'KNOWN CLASS PHOTO (13 Registered Students)')
