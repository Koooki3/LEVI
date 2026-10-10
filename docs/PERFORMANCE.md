# Performance baseline

[中文](PERFORMANCE.zh-CN.md)

LEVI keeps three kinds of timing and cost records already: the live service's
`live/stats.jsonl` (see [Live annotation service](LIVE.md)), the agent usage
samples (`levi/agent/usage.py`) and the per-run harness cost
(`levi/harness/cost.py`). This page describes the read-only view and the CPU
benchmark that sit on top of them. Neither stores anything of its own, uses the
GPU, talks to a model or touches a robot.

## Trace

```
python -m levi.performance trace --live-dir <workspace>/live --workspace <workspace> [--since EPOCH] [--limit N] [--summary-only]
```

Prints one JSON document (`levi.performance.trace.v1`) merged from
`live/stats.jsonl`, `live/gate.jsonl`, the agent usage samples and the closed-run
cost records. Each source is optional. Per labelled episode it gives a
`trace_id` (a hash of dataset, demo, session, batch and episode index; nothing
is stored), the stage times (mirror, plan, first request, commit, verdict),
model seconds and tokens, the time not spent in the model, and how long the GPU
gate was closed during the episode. `summary` gives the median, p90, max and mean
of each. `unmeasured` lists what no record carries yet (frame decode,
presentation-timestamp scan, hashing, queue time, peak memory). The usage
samples are opened read-only; the gate file's session names are not copied.

## Benchmark

```
python -m levi.performance bench [--case hash|pts|pixels] [--repeat 3] [--frames 182] [--size 640x480] [--fps 10] [--cores 4] [--scratch DIR]
```

Makes a synthetic H.264 video with ffmpeg in a scratch folder (removed
afterwards) and times, on at most four CPU cores at low priority:

| Case | What is timed |
| --- | --- |
| `hash` | SHA-256 of the whole file |
| `pts` | listing every presentation time by decoding frames, and from packets; says whether the two lists are identical |
| `pixels` | `levi.conversion.media.inspect(path, pixels=True)`, the conversion's pixel statistics |

The result (`levi.performance.bench.v1`) holds the parameters, the machine, the
library versions and per case the median, 95th percentile and minimum seconds.
Compare two runs on the same machine; the absolute figures do not travel.

## Presentation-timestamp scan (`LEVI_PTS_SCAN`)

Evidence frames are found by a video's presentation times, not by frame number
divided by fps. LEVI lists those times once per video and caches the list next
to the evidence (`*--video-index.json`, keyed by the file's SHA-256).

| `LEVI_PTS_SCAN` | Meaning |
| --- | --- |
| `frame` (default) | The original scan: `ffprobe` decodes every frame and reports its best-effort timestamp. |
| `packet` | Read the container's packet timestamps instead (no decoding): 4 to 80 times faster on measured synthetic video, the same list. If a packet has no timestamp, the packets are missing or their times are not strictly increasing, the frame scan runs instead. The cache file then also records `"scan": "packet"`, and a cache written by the other scan is not used. |

Any other value is refused. In both modes a video's list is kept in memory by
content hash (16 videos), so a v3 file shared by many episodes is scanned once
per process, and the file is hashed once per camera per episode (the hash is
passed to the scan instead of being computed again). Nothing about the checks
changes: every cache is still tied to the source hash and the later time
mismatch tolerance still applies.

`packet` becomes the default only after the two scans have been compared on the
real development videos:

```
python -m levi.performance pts-compare <video or folder> [...]
```

prints one row per file and exits 1 if any list differs or a file cannot be scanned.
