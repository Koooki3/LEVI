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
