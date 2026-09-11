"""Versioned object annotation sidecars for LEVI.

The package deliberately contains no model imports.  Model adapters write the
stable records defined here, which keeps the core workbench usable on CPU-only
machines and makes the sidecar format independent from a particular model.
"""

from .rle import decode_rle, encode_rle, validate_rle
from .sam3_protocol import validate_annotations_for_plan
from .schema import (
    ObjectAnnotation,
    ObjectEdit,
    ObjectTrack,
    ReviewStatus,
    Sam3Plan,
)
from .sidecar import SidecarStore

__all__ = [
    "ObjectAnnotation",
    "ObjectEdit",
    "ObjectTrack",
    "ReviewStatus",
    "Sam3Plan",
    "SidecarStore",
    "decode_rle",
    "encode_rle",
    "validate_annotations_for_plan",
    "validate_rle",
]
