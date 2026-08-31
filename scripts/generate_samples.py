import json
import os
import random
import sys
import time
import urllib.request
import ssl
from pathlib import Path

import cv2
import numpy as np
import requests
import urllib3

# Suppress insecure HTTPS warnings if fallback verify=False is used
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Clean broken Windows SSL environment variables if they point to non-existent files
for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    if var in os.environ and not os.path.exists(os.environ[var]):
        del os.environ[var]

ROOT = Path(__file__).resolve().parent.parent


def download_image(url, retries=3):
    """Download an image from a URL, returning a numpy array for OpenCV."""
    for i in range(retries):
        try:
            response = requests.get(url, timeout=10, verify=False)
            if response.status_code == 200:
                image_array = np.asarray(bytearray(response.content), dtype=np.uint8)
                image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
                if image is not None:
                    return image
        except Exception as e:
            try:
                # Fallback to urllib with unverified context
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(url, context=ctx, timeout=10) as req:
                    data = req.read()
                    image_array = np.asarray(bytearray(data), dtype=np.uint8)
                    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
                    if image is not None:
                        return image
            except Exception:
                pass
            print(f"Attempt {i+1} failed to download {url}: {e}")
            time.sleep(1)
    return None


def create_background(width, height):
    """Create a clean, realistic classroom/meeting background."""
    bg = np.zeros((height, width, 3), dtype=np.uint8)
    color1 = (220, 225, 230)
    color2 = (175, 185, 195)

    for y in range(height):
        alpha = y / height
        color = (
            int(color1[0] * (1 - alpha) + color2[0] * alpha),
            int(color1[1] * (1 - alpha) + color2[1] * alpha),
            int(color1[2] * (1 - alpha) + color2[2] * alpha),
        )
        bg[y, :] = color
    return bg


def generate_samples():
    print("Generating samples from real portrait photos...")

    data_dir = ROOT / "data" / "students"
    samples_ind_dir = ROOT / "samples" / "individuals"
    samples_group_dir = ROOT / "samples" / "groups"

    data_dir.mkdir(parents=True, exist_ok=True)
    samples_ind_dir.mkdir(parents=True, exist_ok=True)
    samples_group_dir.mkdir(parents=True, exist_ok=True)

    num_students = 12
    url = f"https://randomuser.me/api/?results={num_students}&gender=&nat=in,us,gb"

    print(f"Fetching {num_students} users from randomuser.me...")
    users = []
    try:
        response = requests.get(url, timeout=10, verify=False)
        data = response.json()
        users = data.get("results", [])
    except Exception as e:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, context=ctx, timeout=10) as req:
                data = json.loads(req.read().decode("utf-8"))
                users = data.get("results", [])
        except Exception as e2:
            print(f"Failed to fetch users: {e2}")

    if not users:
        print("[!] Could not reach online API. Using pre-cached or local fallback.")
        return

    students = []
    images = []

    print("\nDownloading individual photos...")
    for i, user in enumerate(users):
        student_id = f"CS-2024-{i+1:03d}"
        name = f"{user['name']['first']} {user['name']['last']}"
        pic_url = user["picture"]["large"]

        img = download_image(pic_url)
        if img is not None:
            # Add subtle margin and resize to 384x384 standard ID resolution
            padded = cv2.copyMakeBorder(img, 30, 30, 30, 30, cv2.BORDER_REFLECT_101)
            padded = cv2.resize(padded, (384, 384), interpolation=cv2.INTER_CUBIC)

            enroll_path = data_dir / f"enroll_{student_id}.jpg"
            cv2.imwrite(str(enroll_path), padded)

            ind_path = samples_ind_dir / f"{student_id}.jpg"
            cv2.imwrite(str(ind_path), padded)

            students.append({
                "id": student_id,
                "name": name,
                "enrollment": f"data/students/enroll_{student_id}.jpg",
            })
            images.append({
                "id": student_id,
                "img": padded,
            })
            print(f"  [+] Saved {student_id} - {name}")
        else:
            print(f"  [-] Failed to download image for {student_id}")

    if not images:
        print("[!] No images downloaded.")
        return

    # Create realistic group compositions
    print("\nCreating group compositions...")
    group_photos_manifest = []
    num_groups = 5

    for g in range(num_groups):
        bg = create_background(1280, 720)

        num_in_group = min(random.randint(3, 6), len(images))
        group_members = random.sample(images, num_in_group)

        expected_ids = []
        spacing = 1280 // (num_in_group + 1)

        for idx, member in enumerate(group_members):
            expected_ids.append(member["id"])
            face_img = member["img"].copy()
            scale = random.uniform(0.95, 1.15)
            h, w = face_img.shape[:2]
            nw, nh = int(w * scale), int(h * scale)
            face_img = cv2.resize(face_img, (nw, nh), interpolation=cv2.INTER_LINEAR)

            cx = spacing * (idx + 1)
            cy = 720 // 2 + random.randint(-40, 40)

            y1 = max(0, cy - nh // 2)
            y2 = min(720, cy + nh - nh // 2)
            x1 = max(0, cx - nw // 2)
            x2 = min(1280, cx + nw - nw // 2)

            face_y1 = (nh // 2) - (cy - y1)
            face_y2 = (nh // 2) + (y2 - cy)
            face_x1 = (nw // 2) - (cx - x1)
            face_x2 = (nw // 2) + (x2 - cx)

            # Soft feathering mask for natural boundary blending
            mask = np.ones((nh, nw), dtype=np.float32)
            feather = max(int(min(nw, nh) * 0.08), 4)
            for i in range(feather):
                val = (i + 1) / feather
                mask[i, :] = np.minimum(mask[i, :], val)
                mask[nh - 1 - i, :] = np.minimum(mask[nh - 1 - i, :], val)
                mask[:, i] = np.minimum(mask[:, i], val)
                mask[:, nw - 1 - i] = np.minimum(mask[:, nw - 1 - i], val)

            crop_mask = mask[face_y1:face_y2, face_x1:face_x2, np.newaxis]
            crop_face = face_img[face_y1:face_y2, face_x1:face_x2].astype(np.float32)
            crop_bg = bg[y1:y2, x1:x2].astype(np.float32)

            blended = crop_face * crop_mask + crop_bg * (1.0 - crop_mask)
            bg[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)

        group_filename = f"group_{g+1}.jpg"
        group_path = samples_group_dir / group_filename
        cv2.imwrite(str(group_path), bg)

        group_photos_manifest.append({
            "file": f"samples/groups/{group_filename}",
            "expected_ids": expected_ids,
        })
        print(f"  [+] Created {group_filename} with {len(expected_ids)} faces: {expected_ids}")

    manifest = {
        "students": students,
        "group_photos": group_photos_manifest,
    }

    manifest_path = ROOT / "samples" / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n[OK] Manifest saved to {manifest_path}")
    print("[OK] Sample generation complete!\n")


if __name__ == "__main__":
    generate_samples()