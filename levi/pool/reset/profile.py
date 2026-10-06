"""The thresholds a reset analysis uses, in one place.

They were set on a sample of the collection that motivated this feature
(about 150 real releases of FR3 + Robotiq captures at 10 Hz, wrist camera on
the hand): objects that stayed between the open fingers scored high on the
same-place or the best-match correlation, objects that fell flat or rolled
away scored low on both. A different robot or camera needs its own look at
the metrics (``levi pool reset-analyze`` prints them); the version string is
recorded in every plan and export so a result says which thresholds made it.
"""

VERSION = "reset-profile-1"

# Releases: the gripper command goes closed -> open.
HOLD_ROWS_BEFORE = 3  # hold frame: one of the last rows before the command edge
SETTLE_ROWS_AFTER = 12  # rest frame: searched this many rows after the fingers opened
OPEN_FRACTION = 0.95  # fingers count as open at this share of the widest opening
HOLD_WIDTH_MAX = 0.078  # m: wider than this at release, nothing was held

# The arm may not have moved between hold frame and rest frame for the wrist
# camera to see the same scene from the same place.
# A rest frame may be this far from the hold frame; the seam then goes at the
# earliest settled one, so the jump it leaves is usually a few millimetres.
SEAM_POSITION_TOL = 0.025  # m
SEAM_ROTATION_TOL = 0.12  # rad
JOIN_POSITION_TOL = 0.015  # m: where a recording meets the reversed episode
JOIN_ROTATION_TOL = 0.10  # rad

# Wrist camera, hold frame vs rest frame (see vision.py).
SAME_PLACE_NCC = 0.75  # the object did not move at all
REACH_NCC = 0.60  # a scale-tolerant match is found near the centre
ESCAPED_NCC = 0.50  # neither is found: the object left the fingers' reach
ESCAPED_HIST = 0.50
REACH_SHIFT = 0.30  # largest match shift, as a share of the image size
TEXTURE_MIN = 14.0  # grey-level std of the hold frame's centre
BLUR_RATIO = 0.35  # rest frame this much blurrier than the hold frame: still moving

# Gripper latency: rows by which a command precedes the visible finger motion
# when no measured width is available.
DEFAULT_LEAD_ROWS = 3

# A recorded stretch (bridge) must begin where the forward episode ended and
# end where the reversed part takes over.
SPLICE_START_TOL = 0.03  # m: record's first pose vs the forward episode's last
SPLICE_SAME = 0.40  # wrist-camera agreement at the join (see vision.measure)
SPLICE_HIST = 0.60
