# Conversion audit / 转换流程审查

The former external capture-script adapter has been replaced by the self-contained `levi/conversion/` package. The original capture workflow was inspected before this rewrite; private paths, task correction tables and training-environment assumptions were not copied into the distribution.

| Finding | Change | Evidence |
| --- | --- | --- |
| Inner CSV joins could drop rows while videos were copied unchanged | Require identical ordered frame IDs and timestamps; reject missing/duplicate/nonfinite data | Alignment and bad-command tests |
| Unknown gripper commands silently became open | Only accept explicit `open` / `close` | Unknown-command test |
| A robot-specific Euler branch cut was moved to fit one corpus | Normalize quaternions; continuous Euler or continuous-sign quaternion output; document singularities | Wrap/sign and quaternion job tests |
| Adjacent-pair filtering could erase long slow motion | Compare against last retained state using quaternion angular distance; protect endpoint/gripper frames | Slow-motion and grasp-margin test |
| Image and CSV resampling had separate paths | One retained-position array drives all cameras and indexed CSVs; original IDs/time saved | Image/FPS integration test |
| Resolution, video codec and image statistics were constants | Inspect actual output media; compute all-pixel RGB stats and weighted aggregate moments | 64×48 fixture and decoded-stat comparison |
| Raw wall-clock timestamps required a mandatory repair afterwards | Write `frame_index / FPS` at conversion; retain original acquisition timestamps separately | Full-pipeline timestamp/provenance tests |
| Partial or skipped demos could appear as success | Fail preflight; exclusions are explicit relative capture paths; worker status uses exit code and report | Preflight/API tests |
| Staging symlinks were not immutable snapshots | Materialize independent files; reject conversion symlinks; size/mtime fingerprint before/after execution | Source hash and symlink tests |
| Task cleanup depended on private hard-coded examples | User JSON mapping; unmapped slug preview/apply fails before writes | Task mapping tests |
| FPS tools included source-video deletion / output overwrite options | New output only; rebuild aligned copies; no delete/overwrite switch | Source-byte and existing-output checks |
| End-to-end validation required an unrelated training environment | Native schema/row contracts plus full media decode within the same uv environment | Pipeline and corrupted-metadata tests |
| Task processes could survive service shutdown | Dedicated process groups, timeout termination and restart interruption status | Worker integration; lifecycle source review |
| Private parent markers and workspace variables selected output paths | `LEVI_WORKSPACE` or checkout `.state` only | Relocated-checkout verification |
| Cleanup risked deleting data or shared environments | Explicit cache allowlist, ancestor symlink checks, service-running guard; registered datasets, dependencies and data preserved | Cleanup boundary test |

## Workflow coverage

Capture statistics, frozen cameras, staging/preflight, static-pose analysis/filtering, image-to-video, FPS normalization, LeRobot conversion, timestamp repair, task normalization and final validation are all bundled stages. Their implementation is consolidated rather than distributing duplicated script helpers.

Hardware collector/ROS utilities are not needed to process the documented capture schema. The exported gripper channel is the binary command; it does not require a hardware knuckle-to-width calculation. Measured aperture and hardware-specific action semantics require a dedicated input adapter, not a silent reinterpretation of this channel.

## Performance and tradeoffs

- Pose math and statistics use NumPy arrays. Filtering performs one sequential pass and retains accumulated displacement.
- Video processing stores a single frame at a time and pipes selected frames to FFmpeg. Encoding is limited to two threads; web workers are limited to two simultaneous jobs.
- Quality preflight and final verification intentionally decode video again. They cost CPU/I/O but detect count mismatches and broken output. H.264 outputs are not byte-identical to source footage.
- Intermediates are independent snapshots; storage is higher than symlink staging. They remain reviewable outputs and are not removed by cache cleanup.
- Fingerprints detect ordinary recording edits; they are not tamper-proof content hashes. Use stopped, stable captures.
- Euler unwrap handles branch crossings, not gimbal lock. Quaternion mode is available and changes the policy feature width.
- Raw conversion and metadata repair write v2.1; browsing accepts v2/v3. Native validation is scoped, not certification by a training loader.

Primary references: [LeRobot format layouts](https://github.com/huggingface/lerobot/blob/main/docs/source/porting_datasets_v3.mdx), [LeRobot dataset API](https://huggingface.co/docs/lerobot/main/api/datasets), [FFmpeg rawvideo](https://www.ffmpeg.org/ffmpeg-formats.html#rawvideo), [FFmpeg options](https://ffmpeg.org/ffmpeg.html). The format version is explicit in output `meta/info.json`; it is not inferred from a directory name.
