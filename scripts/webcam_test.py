#!/usr/bin/env python3
"""Live webcam test for FaceMark detection pipeline.

Usage: python scripts/webcam_test.py [mode]
  mode: fast | fused | accurate (default: fused)

Press 'q' to quit, 'm' to cycle modes, 's' to save frame.
"""
from __future__ import annotations

import sys
import cv2
import numpy as np

from backend.detector import get_detector
from backend.recognizer import get_recognizer
from backend import database, config

MODES = ["fast", "fused", "accurate"]


def draw_hud(frame: np.ndarray, mode: str, fps: float, faces: list) -> None:
    """Draw heads-up display on frame."""
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (320, 100), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    cv2.putText(frame, f"FaceMark - {mode.upper()}", (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    cv2.putText(frame, f"FPS: {fps:.1f}  Faces: {len(faces)}", (20, 70),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    cv2.putText(frame, "Keys: [m]ode  [s]ave  [q]uit", (20, 95),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)


def draw_faces(frame: np.ndarray, faces: list, recognizer=None, gallery=None) -> None:
    """Draw face boxes with recognition labels."""
    for f in faces:
        x1, y1, x2, y2 = map(int, f.box)
        color = (0, 255, 0)
        label = f"Face {f.conf:.2f}"

        if recognizer and gallery and len(faces) > 0:
            # Try recognition (simplified - would need full pipeline)
            label += f" | {f.source}"

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, y1 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # Draw landmarks if available
        if f.landmarks is not None:
            for (lx, ly) in f.landmarks.astype(int):
                cv2.circle(frame, (lx, ly), 3, (255, 0, 255), -1)


def main():
    mode_idx = MODES.index("fused")
    if len(sys.argv) > 1 and sys.argv[1] in MODES:
        mode_idx = MODES.index(sys.argv[1])

    print(f"Starting webcam test in {MODES[mode_idx]} mode...")
    print("Keys: [m] cycle mode | [s] save frame | [q] quit")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Could not open webcam")
        return

    # Set resolution
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    detector = get_detector()
    recognizer = get_recognizer()
    gallery = database.load_gallery()

    frame_count = 0
    t_start = cv2.getTickCount()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Detect faces
        faces = detector.detect(frame, mode=MODES[mode_idx])

        # Calculate FPS
        frame_count += 1
        if frame_count % 30 == 0:
            t_now = cv2.getTickCount()
            fps = 30 * cv2.getTickFrequency() / (t_now - t_start)
            t_start = t_now
        else:
            fps = 0

        # Draw
        draw_faces(frame, faces, recognizer, gallery)
        draw_hud(frame, MODES[mode_idx], fps, faces)

        cv2.imshow("FaceMark Webcam Test", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('m'):
            mode_idx = (mode_idx + 1) % len(MODES)
            print(f"Switched to {MODES[mode_idx]} mode")
        elif key == ord('s'):
            fname = f"webcam_capture_{cv2.getTickCount()}.jpg"
            cv2.imwrite(fname, frame)
            print(f"Saved {fname}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()