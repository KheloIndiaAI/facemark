import sqlite3, cv2, numpy as np
from backend import database, config
from backend.detector import get_detector
from backend.recognizer import get_recognizer, pool_to_students

conn = database.connect()
conn.execute("DELETE FROM templates WHERE source = 'adapted'")
conn.commit()
print('Cleared adapted templates from DB.')

det = get_detector()
rec = get_recognizer()
gallery, _ = database.load_gallery_with_quality()
weights = {m.name: m.weight for m in rec.models}
all_ids = sorted({int(sid) for name in gallery for sid in gallery[name][1]})

def eval_image(path, label):
    img = cv2.imread(path)
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
    
    print(f"\n=== {label} ({len(faces)} faces detected) ===")
    for i, f in enumerate(faces):
        sorted_indices = np.argsort(raw_fused[i])[::-1]
        best_sid = all_ids[sorted_indices[0]]
        best_sim = raw_fused[i, sorted_indices[0]]
        sec_sim = raw_fused[i, sorted_indices[1]] if len(sorted_indices) > 1 else 0.0
        ratio = sec_sim / max(best_sim, 1e-6)
        st = database.get_student(best_sid)
        name = st['name'] if st else str(best_sid)
        print(f"Face {i+1:2d} (w={f.width:4.1f}px): Best={name:<15} | Cosine={best_sim:.4f} | 2nd={sec_sim:.4f} | Ratio={ratio:.2f}")

eval_image('data/uploads/group_20260820_132732_813.jpg', 'KNOWN CLASS PHOTO (13 students registered)')
eval_image('data/uploads/group_20260820_160116_156.jpg', 'UNKNOWN GROUP PHOTO (0 students registered)')
