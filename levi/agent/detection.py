"""Rig-independent object candidates from one evidence frame.

An agent annotating objects without SAM3 has to turn pixels into outlines. It
can write a detector per rig -- the way this was first done by hand for a plate
stacking capture -- but the *measuring* part is the same everywhere: split the
frame into coherent regions, then describe each one well enough for the agent to
decide what it is. Only the deciding is task-specific, so only the deciding is
left to the agent.

Nothing here is a model: no checkpoint, no GPU, no network. It is colour
quantisation plus connected components, which is cheap, deterministic and
explains itself through the statistics it returns.
"""

MAX_POLYGON = 40


def _stats(hsv, mask):
    import numpy as np

    hue, sat, val = (hsv[:, :, i][mask > 0] for i in range(3))
    if not len(hue):
        return None
    # Hue is circular: the mean of 1 and 179 is red, not cyan.
    angles = np.deg2rad(hue.astype(float) * 2)
    mean_hue = float(
        np.rad2deg(np.arctan2(np.sin(angles).mean(), np.cos(angles).mean()))
    )
    return {
        "hue": round((mean_hue % 360) / 2, 1),
        "saturation": round(float(np.median(sat)), 1),
        "value": round(float(np.median(val)), 1),
        "hue_spread": round(float(np.rad2deg(np.std(angles)) / 2), 1),
    }


def candidates(path, *, max_candidates=12, min_area_fraction=0.002, colours=6):
    """Describe the coherent regions of one frame, largest first.

    Each candidate carries the outline an annotation would use plus the
    measurements an agent needs to name it: where it sits, how round it is,
    whether it runs off the edge of the frame, and its colour in HSV. The
    agent reads a handful of numbers instead of a picture per object.
    """
    import cv2
    import numpy as np

    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"Frame is not readable: {path}")
    height, width = image.shape[:2]
    blurred = cv2.GaussianBlur(image, (5, 5), 0)
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)

    # Quantise colour so that one object is one region even under shading.
    flat = np.float32(blurred.reshape(-1, 3))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    cv2.setRNGSeed(0)  # deterministic: the same frame yields the same ids
    _, labels, _ = cv2.kmeans(flat, colours, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(height, width)

    minimum = max(64, int(height * width * min_area_fraction))
    kernel = np.ones((5, 5), np.uint8)
    found = []
    for index in range(colours):
        layer = cv2.morphologyEx(
            np.where(labels == index, 255, 0).astype(np.uint8),
            cv2.MORPH_OPEN,
            kernel,
        )
        contours, _ = cv2.findContours(
            layer, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < minimum:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            region = np.zeros((height, width), np.uint8)
            cv2.drawContours(region, [contour], -1, 255, -1)
            appearance = _stats(hsv, region)
            if appearance is None:
                continue
            outline = cv2.approxPolyDP(
                contour, 0.01 * cv2.arcLength(contour, True), True
            ).reshape(-1, 2)
            while len(outline) > MAX_POLYGON:
                outline = outline[::2]
            hull = cv2.convexHull(contour)
            found.append(
                {
                    "bbox_xyxy": [float(x), float(y), float(x + w), float(y + h)],
                    "polygon": [[float(px), float(py)] for px, py in outline],
                    "area_px": round(area),
                    "area_fraction": round(area / (height * width), 4),
                    "centre": [round(x + w / 2, 1), round(y + h / 2, 1)],
                    "aspect": round(w / max(h, 1), 2),
                    # A round object stays near 1.0; a hand or a cable does not.
                    "solidity": round(area / max(cv2.contourArea(hull), 1.0), 2),
                    "ellipse_fit": round(area / max(np.pi * w * h / 4, 1.0), 2),
                    "touches_edge": bool(
                        x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1
                    ),
                    "appearance": appearance,
                }
            )
    found.sort(key=lambda item: -item["area_px"])
    kept = []
    for item in found:
        # Two colour layers can trace the same object; keep the larger outline.
        if all(
            abs(item["centre"][0] - other["centre"][0]) > 0.02 * width
            or abs(item["centre"][1] - other["centre"][1]) > 0.02 * height
            for other in kept
        ):
            kept.append(item)
        if len(kept) >= max_candidates:
            break
    for position, item in enumerate(kept):
        item["candidate_id"] = f"c{position:02d}"
    return {
        "image_size": [height, width],
        "candidates": kept,
        "reading": (
            "Regions of one frame, largest first, measured not recognised. "
            "Name the ones that are objects and submit them by candidate_id; "
            "appearance is median HSV (OpenCV ranges: hue 0-179, others 0-255)."
        ),
    }


def overlay(path, destination, found):
    """A single labelled picture of the candidates, for one look instead of many."""
    import cv2
    import numpy as np

    image = cv2.imread(str(path))
    for item in found:
        points = np.array(item["polygon"], np.int32)
        cv2.polylines(image, [points], True, (60, 255, 200), 1)
        cv2.putText(
            image,
            item["candidate_id"],
            (int(item["bbox_xyxy"][0]), max(10, int(item["bbox_xyxy"][1]) - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (60, 255, 200),
            1,
        )
    cv2.imwrite(str(destination), image)
    return destination.name
