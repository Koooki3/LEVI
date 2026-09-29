# LEVI fast segmentation worker

Isolated environment for LEVI's fast instance segmentation: a distilled RF-DETR-Seg student (Apache-2.0) with ByteTrack (supervision, MIT). LEVI's core starts it as a subprocess and never imports Torch.

```bash
./setup.sh                                   # uv venv (torch cu128, rfdetr 1.11.0)
.venv/bin/python -m levi_seg_worker.cli --check
```

Commands (plans are written by LEVI): `label` (offline labelling), `distil` (SAM3 pseudo-labels -> training -> held-out evaluation -> model folder), `live` (follows the player clock on stdin, one JSON line per camera frame on stdout), `bench` (the live pipeline with a synthetic clock). The `fake` provider needs only NumPy, OpenCV and pyarrow, so LEVI's CPU tests run it with LEVI's own Python.

See [docs/SEGMENTATION.md](../../docs/SEGMENTATION.md).
