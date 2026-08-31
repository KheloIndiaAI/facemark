import sys, cv2, numpy as np, glob
from pathlib import Path
ROOT = Path(r'c:\Users\Jatin Singh\Downloads\Attendance face recog')
sys.path.insert(0, str(ROOT))
from backend import database, config, utils
from backend.detector import get_detector
from backend.recognizer import get_recognizer, fuse_scores
from backend.metaheuristics import GlobalMatchOptimizer

det = get_detector()
rec = get_recognizer()
gallery = database.load_gallery()

files = glob.glob(r'C:/Users/Jatin Singh/.gemini/antigravity/brain/56727575-3043-4f08-bbd7-3d8c54b56f79/.user_uploaded/*.png')
for f in files:
    img = cv2.imread(f)
    if img is not None and img.shape[1] > 800:
        detected = det.detect(img, mode='fused')
        if len(detected) >= 10:
            print(f'Processing group photo: {Path(f).name} with {len(detected)} detected faces')
            queries = rec.embed_faces(img, detected)
            fused, gallery_ids = fuse_scores(queries, gallery, {m.name: m.weight for m in rec.models})
            
            matches = GlobalMatchOptimizer.optimize_assignments(fused, gallery_ids, threshold=0.45)
            match_dict = {f_idx: (sid, sim) for f_idx, sid, sim in matches}
            
            print(f'Optimizer found {len(matches)} matches out of {len(detected)} faces:')
            for i, face in enumerate(detected):
                if i in match_dict:
                    sid, sim = match_dict[i]
                    s = database.get_student(sid)
                    name = s['name'] if s else f'ID {sid}'
                    print(f'  Face #{i} -> {name} (sim={sim:.4f})')
                else:
                    best_col = np.argmax(fused[i])
                    best_sid = gallery_ids[best_col]
                    best_sim = fused[i, best_col]
                    s = database.get_student(best_sid)
                    name = s['name'] if s else f'ID {best_sid}'
                    print(f'  Face #{i} -> UNKNOWN (closest was {name} with sim={best_sim:.4f})')
