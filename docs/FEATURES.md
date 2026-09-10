# 功能对照 / Feature parity

Baseline: `huggingface/lerobot-dataset-visualizer@dc59887796fd41f37040c0df6b10e6f6a30a1854`.

| Reference feature | LEVI implementation | Verification |
| --- | --- | --- |
| Hub browsing, search, pagination | Retained explore grid; original LEVI landing/search | Real default Hub cards and episode loading |
| Private datasets / OAuth | Retained OAuth + local Token sign-in + same-origin video proxy | Public proxy and cookie route checks; live private-account login needs the user's credentials |
| Multi-camera synchronized playback | Retained player, shared time context, keyboard, fullscreen/hide | Three real 640×480 camera feeds loaded in Chromium |
| Action / state signal charts | Retained chart grouping, toggles, live cursor | Reference episode signal rendering |
| Language annotations and timeline | Retained upstream schema/editor; v2 backend compatibility added | v2/v3 Parquet round-trip, exact event snapping, original file unchanged |
| Grounded VQA | Retained drag bbox / click point and overlays; text answer modes | Browser interaction and persisted atoms |
| Dataset statistics | Retained metadata and episode length panels | v2 length histogram + JSONL tests; v3.1 multi-chunk browser fixture |
| Filtering and CLI export | Retained movement, smoothness and length filtering | Browser tabs, dataset-scoped flags and export tests |
| First / last frames | Retained gallery and camera selector | Real 10-episode gallery |
| Action autocorrelation | Retained upstream normalized analysis | Reference evaluation collection: suggested 31-step chunk |
| State/action alignment | Retained upstream differential cross-correlation | Reference collection: mean peak at lag 2 |
| Speed and cross-episode variance | Retained histogram and heatmap | Reference collection charts |
| 3D URDF replay | Retained upstream models, mapping, controls, end-effector trails; enabled for compatible v2 robots too | Real SO-100 model loaded in Chromium |
| Doctor | Native version-aware local checks + external original doctor entry | Synthetic anomalies, full 10-episode real reference report |
| Annotation export / Hub push | Retained backend API; source-safe new exports | Export round-trip tested; no Hub upload performed |
| Built-in conversion | Full capture workflow, configurable stages and automatic registration | Real CSV/image/video integration tests |
| Chinese / English | Added React locale boundaries, catalogs, live switch | Both languages and viewport checks |
| Independent environment / distribution | uv.lock, .venv, local Bun, launcher, Dockerfile, CI, bilingual docs | Python suite, frontend suite and production build |

## Native Doctor

Unlike the external reference doctor, LEVI does not equate v2.1 metadata layout with corruption. Checks group results by metadata, temporal consistency, action quality, video integrity, distributions, episode health, feature consistency, training readiness, anomalies and portability. Reports include affected episode IDs for review.

- Timestamps: missing/non-finite values, duplicates, ordering, nominal FPS spacing, nonconsecutive frame indices.
- Numeric features: missing fields, inconsistent shapes, NaN/Inf, constant actions/actuators, mean absolute action jumps beyond `mean + 8σ`, extreme values beyond `10σ`.
- Metadata: accepted format, task metadata, frame totals for complete inspection, portable relative file templates.
- Video: explicit skip without decode; optional first/middle/last-frame decoding, FPS agreement and nearly identical sampled images.
- Training preparation: presence of stored normalization statistics. This is not a training loader acceptance guarantee.

Status counts count individual results, not just category headings. Sampling scope and skipped video checks are explicit. Heuristics can flag intentional static joints or binary grippers; review is necessary. The native and external doctor implement different checks, so their exact counts need not match.

## Formats and retained constraints

- v2.0/v2.1: episode-per-file Parquet and video, JSON/JSONL metadata.
- v3.0/v3.1: shared Parquet/video files and episode metadata Parquet. Multi-chunk metadata iteration is retained; a two-chunk v3.1 shared-video fixture was verified in Chromium.
- Browser support depends on the video's codec. H.264 and the demonstrated AV1 videos were tested in Chromium. Unsupported camera codecs may require conversion.
- Embedded-image-only datasets are rejected by the retained upstream loader.
- Hub video, OAuth, external doctor, URDF meshes and research-paper links require network access. Their text is not translated by LEVI.
- Auto chunk suggestions and alignment estimates preserve upstream mathematical behavior, not universal training prescriptions.
- Runtime is a source checkout, not a standalone frontend bundled inside a PyPI wheel. Use the documented source install or Docker image.
- Stopping the service terminates conversion workers; interrupted jobs and partial outputs remain for inspection. Restart never automatically resumes writes.
