"""Is the object still where the gripper could take it again?

After a release the arm is often still for a few rows while the object falls
and settles. The wrist camera moves with the hand, so while the arm has not
moved, the hold frame (object between the closed fingers) and the rest frame
(fingers open, object at rest) show the same scene from the same place: any
difference is the object, plus the fingers. Three measures on the centre of
the image, where the object sits between the fingers:

- ``same``: correlation of the hold frame's centre with the same place in the
  rest frame (1: the object did not move);
- ``best`` / ``dx`` / ``dy`` / ``scale``: the best match of that centre
  anywhere near the middle at several sizes (the object may have dropped
  closer to the floor, so smaller, and shifted);
- ``hist``: how alike the colours of the two centres are (recorded, not used to
  accept: a plate or a tabletop gives the same colours with or without the
  object).

``texture``: how much the hold frame's centre varies (a plain white tray
matches itself whatever is on it, so a flat centre proves nothing).

``sharp``: the rest frame's sharpness against the hold frame's; a blurred rest
frame means the object is still moving.

This is a measurement, not recognition: it knows nothing about what the object
is. Anything it cannot place is ``unknown`` and is never reversed on its word.
"""

import cv2

from . import profile

SCALES = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25)


def _centre(image, x0, x1, y0, y1):
    h, w = image.shape[:2]
    return image[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)]


def _sharpness(gray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def measure(hold, rest) -> dict:
    """The metrics for a hold frame and a rest frame (BGR, same size)."""
    if hold.shape != rest.shape:
        raise ValueError("hold and rest frames differ in size")
    h, w = hold.shape[:2]
    template = _centre(hold, 0.35, 0.65, 0.25, 0.65)
    same_region = _centre(rest, 0.35, 0.65, 0.25, 0.65)
    same = float(cv2.matchTemplate(same_region, template, cv2.TM_CCOEFF_NORMED)[0, 0])
    sx0, sy0 = int(0.15 * w), 0
    search = rest[sy0 : int(0.85 * h), sx0 : int(0.85 * w)]
    best, where, scale = -1.0, None, 1.0
    for factor in SCALES:
        t = cv2.resize(template, None, fx=factor, fy=factor)
        if t.shape[0] >= search.shape[0] or t.shape[1] >= search.shape[1]:
            continue
        scores = cv2.matchTemplate(search, t, cv2.TM_CCOEFF_NORMED)
        _, top, _, loc = cv2.minMaxLoc(scores)
        if top > best:
            best = float(top)
            where = (loc[0] + t.shape[1] / 2 + sx0, loc[1] + t.shape[0] / 2 + sy0)
            scale = factor
    dx = dy = 9.0
    if where is not None:
        dx, dy = (where[0] - 0.5 * w) / w, (where[1] - 0.45 * h) / h

    def hist(image):
        part = cv2.cvtColor(_centre(image, 0.3, 0.7, 0.2, 0.7), cv2.COLOR_BGR2HSV)
        value = cv2.calcHist([part], [0, 1], None, [16, 8], [0, 180, 0, 256])
        return cv2.normalize(value, None).flatten()

    colours = float(cv2.compareHist(hist(hold), hist(rest), cv2.HISTCMP_CORREL))
    sharp_hold = _sharpness(
        cv2.cvtColor(_centre(hold, 0.2, 0.8, 0.1, 0.8), cv2.COLOR_BGR2GRAY)
    )
    sharp_rest = _sharpness(
        cv2.cvtColor(_centre(rest, 0.2, 0.8, 0.1, 0.8), cv2.COLOR_BGR2GRAY)
    )
    texture = float(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY).std())
    return {
        "texture": round(texture, 1),
        "same": round(same, 3),
        "best": round(best, 3),
        "dx": round(dx, 3),
        "dy": round(dy, 3),
        "scale": scale,
        "hist": round(colours, 3),
        "sharp": round(sharp_rest / sharp_hold, 3) if sharp_hold > 1e-6 else None,
    }


def classify(m: dict) -> tuple[str, str | None]:
    """(class, reason): ``in_place``, ``in_reach``, ``escaped`` or ``unknown``."""
    if m.get("texture") is not None and m["texture"] < profile.TEXTURE_MIN:
        return "unknown", "low_texture"  # a plain background matches itself
    near = abs(m["dx"]) <= profile.REACH_SHIFT and abs(m["dy"]) <= profile.REACH_SHIFT
    if m["same"] >= profile.SAME_PLACE_NCC:
        klass = "in_place"
    elif m["best"] >= profile.REACH_NCC and near:
        klass = "in_reach"
    elif m["best"] < profile.ESCAPED_NCC and m["hist"] < profile.ESCAPED_HIST:
        return "escaped", "object_left_the_fingers"
    else:
        return "unknown", "ambiguous_match"
    # A found object in a blurred rest frame is still moving (a rest frame is
    # only trusted when it is as sharp as the hold frame, give or take).
    if m.get("sharp") is not None and m["sharp"] < profile.BLUR_RATIO:
        return "unknown", "object_still_moving"
    return klass, None
