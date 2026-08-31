import sys, os
from pathlib import Path
import cv2
import numpy as np
import random

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import database

def create_synthetic_group_photo(students, filename):
    # Determine canvas size based on number of students
    num_students = len(students)
    W = max(1200, (num_students + 1) * 200)
    H = 720
    
    bg = np.zeros((H, W, 3), dtype=np.uint8)
    
    # Create a simple classroom-like gradient background
    for y in range(H):
        alpha = y / H
        bg[y, :] = (
            int(220 * (1 - alpha) + 180 * alpha),
            int(230 * (1 - alpha) + 190 * alpha),
            int(240 * (1 - alpha) + 200 * alpha)
        )
        
    # Draw a whiteboard
    cv2.rectangle(bg, (50, 40), (W - 50, 200), (160, 170, 180), -1)
    cv2.rectangle(bg, (50, 40), (W - 50, 200), (120, 130, 140), 2)
    cv2.putText(bg, "Computer Science 2024", (70, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (100, 100, 100), 2)
    
    spacing = W // (num_students + 1)
    
    for idx, student in enumerate(students):
        photo_path = student['photo_path']
        if not os.path.exists(photo_path):
            continue
            
        img = cv2.imread(photo_path)
        if img is None:
            continue
            
        # Resize to standard face portrait size
        FACE_SIZE = 220
        img = cv2.resize(img, (FACE_SIZE, FACE_SIZE))
        
        fh, fw = img.shape[:2]
        cx = spacing * (idx + 1)
        # Alternate height slightly for realism
        cy = 430 + (25 if idx % 2 == 1 else -25)
        
        y1, y2 = cy - fh // 2, cy + fh // 2
        x1, x2 = cx - fw // 2, cx + fw // 2
        
        # Paste face
        bg[y1:y2, x1:x2] = img
        
        # Write name label below
        label_y = y2 + 30
        (tw, th), _ = cv2.getTextSize(student['name'], cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.putText(bg, student['name'], (cx - tw//2, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (50, 50, 50), 2)

    cv2.imwrite(filename, bg, [cv2.IMWRITE_JPEG_QUALITY, 98])
    return filename

def main():
    database.init_db()
    students = database.list_students()
    
    if len(students) == 0:
        print("No students in database to generate photos with.")
        return
        
    output_dir = ROOT / "samples" / "generated_groups"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Generating 10 synthetic group photos using {len(students)} enrolled students...")
    
    for i in range(10):
        # Pick a random number of students between 3 and min(10, total)
        count = random.randint(3, min(10, len(students)))
        # Pick random students
        selected = random.sample(students, count)
        
        filename = str(output_dir / f"test_group_{i+1:02d}.jpg")
        create_synthetic_group_photo(selected, filename)
        
        names = [s['name'].split()[0] for s in selected]
        print(f"Generated {filename} with {count} students: {', '.join(names)}")

    print(f"\nSuccessfully created 10 test photos in {output_dir}")

if __name__ == "__main__":
    main()
