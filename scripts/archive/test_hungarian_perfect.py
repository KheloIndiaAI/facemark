import cv2, numpy as np
from backend import database, config
from backend.detector import get_detector
from backend.recognizer import get_recognizer, pool_to_students
from backend.metaheuristics import solve_optimal_assignment

det = get_detector()
rec = get_recognizer()
gallery, _ = database.load_gallery_with_quality()
weights = {m.name: m.weight for m in rec.models}
all_ids = sorted({int(sid) for name in gallery for sid in gallery[name][1]})

def eval_photo(img_path, label):
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
    
    # Resolution scaling on distant microscopic crowd faces (< 38px)
    for i, f in enumerate(faces):
        dim = max(f.width, f.height)
        if dim < 38.0:
            raw_fused[i] *= min(1.0, (dim / 38.0) ** 0.4)
            
    # Hungarian Global Assignment
    cost_matrix = 1.0 - raw_fused.copy()
    row_ind, col_ind = solve_optimal_assignment(cost_matrix)
    
    # Dynamic threshold: base threshold = 0.38 for clear classroom faces (>= 35px), 0.45 for small crowd faces (< 35px)
    matches = []
    for r, c in zip(row_ind, col_ind):
        sim = float(raw_fused[r, c])
        sid = all_ids[c]
        f = faces[r]
        thr = 0.38 if f.width >= 35.0 else 0.46
        if sim >= thr:
            matches.append((r, sid, sim))
            
    match_dict = {r: (sid, sim) for r, sid, sim in matches}
    
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
            best_sid = all_ids[np.argmax(raw_fused[i])]
            st = database.get_student(best_sid)
            sname = st['name'] if st else str(best_sid)
            print(f"  Face {i+1:2d} (w={f.width:4.1f}px): UNKNOWN (best={sname} {best_sim:.3f})")

eval_photo('data/uploads/group_20260820_160116_156.jpg', 'UNKNOWN PHOTO (Delhi Mallakhamb Association)')
eval_photo('data/uploads/group_20260820_132732_813.jpg', 'KNOWN CLASS PHOTO (13 Registered Students)')
