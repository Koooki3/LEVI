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

`--workspace` is the LEVI workspace (the `LEVI_WORKSPACE` folder); the usage
samples are read from `outputs/LEVI/workbench/agent/workbench.sqlite3` in it and
the cost records from `outputs/LEVI/datasets/*/tasks/*/ledger.json`. The
workbench folder itself is accepted too.

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
| `pixels` | `levi.conversion.media.inspect(path, pixels=True)`, the conversion's pixel statistics, by both `LEVI_PIXEL_STATS` methods (time, speed-up, largest difference) |

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
| `packet` | Read the container's packet timestamps instead (no decoding): 4 to 80 times faster on measured synthetic video, the same list for ordinary files (B-frames, variable frame rate). It trusts the container, so it steps aside whenever the container's packets are not one-to-one with the frames shown: a packet flagged discard (`D`) or corrupt (`C`), which an MP4 edit list or a start before zero produces; a negative timestamp; anything ffprobe reports on stderr (truncated or damaged files); a packet without a timestamp; no packets; times that are not strictly increasing. Then the frame scan runs, and a warning with the file name and the reason is logged. The cache file then also records `"scan": "packet"`, and a cache written by the other scan is not used. |

Any other value is refused. In both modes a video's list is kept in memory by
content hash (16 videos), so a v3 file shared by many episodes is scanned once
per process, and the file is hashed once per camera per episode (the hash is
passed to the scan instead of being computed again). Nothing about the checks
changes: every cache is still tied to the source hash and the later time
mismatch tolerance still applies.

`packet` becomes the default only after the two scans have been compared on the
real development videos:

```
python -m levi.performance pts-compare <video or folder> [...] [--cores 4]
```

Both compare commands pin themselves to at most 4 CPUs at low priority (`--cores`, 0 leaves the process alone). `pts-compare` also lists, for each file, why the packets could not be used.

prints one row per file and exits 1 if any list differs or a file cannot be scanned.

## How often a source video is hashed

Every hash that guards the evidence stays. Per file, a snapshot hashes the
source before the copy, the copy, and the source again (so a change while the
bytes move is caught); the plan-time hashes are compared before any copy; the
check before a commit hashes the source once more; every cached evidence PNG is
hashed again before it is reused. The only redundancy found in
`levi/agent/media.py` was in `sample()`, which hashed a video to key its cache
and then again inside the timestamp index; the hash is now passed on. The
counts are asserted in `tests/test_media_hashing.py` next to tests that tamper
with each stage.

## Conversion pixel statistics (`LEVI_PIXEL_STATS`)

`levi.conversion.media.inspect(path, pixels=True)` measures the per-channel
minimum, maximum, mean and standard deviation of every decoded pixel; these end
up in `meta/stats.json` and the training normalisation.

| `LEVI_PIXEL_STATS` | Meaning |
| --- | --- |
| `float` (default) | The original method: each frame is converted to float64 and summed. |
| `histogram` | Count how often each 8-bit value occurs per channel (3 x 256 integers), then compute the four statistics once from the counts. The sums are exact integers and the variance has no cancellation. About 11 to 17 times faster per frame; min, max, mean and count equal the float method's to about 1e-11 or better. The standard deviation is compared as a variance: the float method computes it as `mean(x^2) - mean(x)^2`, which cancels when a channel is constant or nearly so, and its square root then turns a 1e-12 error into noise up to about 1e-6 (a constant 720p channel gives 2e-6 instead of 0). The histogram result is exact. Frames that are not 8-bit use the float method. |

Any other value is refused. The default stays `float` because the last digits
of `meta/stats.json` change with the method. Before `histogram` becomes the
default, compare the two on the real videos (the command exits 1 if any file is
off by more than 1e-9 in min, max, mean or count, or in the variance (std
squared), or differs in frame count, span or first-frame hash; the std difference
itself is reported but not judged):

```
python -m levi.performance pixel-compare <video or folder> [...] [--cores 4]
```

`bench --case pixels` times both methods on a synthetic video and reports the
speed-up and the largest difference.

## Cache folder with a hard size limit (`DiskBudget`)

`levi.performance.budget.DiskBudget` keeps one cache folder inside a byte limit (and optionally an entry limit) by itself. It exists because a cache that only grows eventually fills the disk: evidence for the 300 curated episodes is about 7 GB, for the 1,616 robotiq episodes about 40 GB, and three schema versions of that 120 GB, while the disk has room for none of the worst cases next to everything else (measured unit cost: about 25 MB per episode, from the clean-up audit). The limit is therefore enforced when an entry is written, not by a cleaner that may run too late.

It is a library only. **Nothing in LEVI uses it yet**; it changes no behaviour and has no setting. It is standard library only and imports no other LEVI module.

```python
from levi.performance.budget import DiskBudget, BudgetRejected

cache = DiskBudget.from_cap_gb("lab/perf-cache/evidence", 20, version="schema-3")   # 20 GiB
cache.put("episode-17", png_bytes)              # evicts the least recently used entries if needed
cache.read("episode-17")                        # bytes or None; a hit counts as a use
with cache.writing("episode-18") as tmp:        # a file or a whole directory, renamed in place at the end
    tmp.mkdir()
    ...
try:
    cache.put("x", data)
except BudgetRejected as why:                   # why.reason: too_large | no_space | no_output | lock_timeout
    ...                                         # the caller carries on without the cache
```

| Behaviour | Detail |
| --- | --- |
| Limits | `max_bytes` (hard) and `max_entries`. A write that does not fit evicts entries until it does, or is refused (`too_large`) before anything is evicted if it could never fit. Units are GiB (2**30), the unit of `cap_gb` in the janitor's quota table. |
| LRU | An entry's modification time is its last use; `get` and `read` touch it. This is the same clock the janitor's `lru_cap` finder ranks by, so the two never disagree about what is oldest. |
| TTL | An entry idle for more than `ttl_s` (default 14 days, the janitor's `perf-cache` age) is expired: never returned, dropped on the next write or `maintain()`. `put(..., ttl_s=)` sets one entry's own. |
| Versions | Every entry is written under a version string (a schema, model or prompt version). Entries of any other version are stale: never returned, evicted before valid ones, deleted when idle for `stale_grace_s` (default 7 days) or at once by `purge_stale()`. Writing the same key under the new version replaces the old entry. |
| Atomic writes | Data goes to `.tmp-<pid>-<random>` and is renamed into place after `fsync`; a reader sees the whole entry or none. An entry is a file or a directory. |
| Crash safety | The folder is the truth; `.budget.index.json` only remembers the original key, version string and per-entry TTL. If it is missing, corrupt or disagrees with the folder it is rebuilt from the folder. On open, scratch names (`.tmp-*`, `.trash-*`) of dead processes (or older than an hour) are removed. A deleted entry is renamed to `.trash-*` first, so a crash mid-delete never leaves a half-empty entry that looks valid. |
| Concurrency | Every change runs under an exclusive `flock` on `.budget.lock`. Threads and processes exclude each other; the kernel releases the lock when a process dies, even by SIGKILL. |
| Disk full | `ENOSPC` or `EDQUOT` while writing, or less than `min_free_bytes` left on the disk after the write, is `BudgetRejected("no_space")`; nothing partial is left. |

Limits of the design. Bytes of writes still in progress count only when they are committed, so the folder can briefly exceed its limit by what is being written. `min_free_bytes` is the only protection against other users of the same disk. Names that are not `<32 hex>.<8 hex>` (dot files, foreign files) are not managed.

**Relation to the janitor.** The janitor (`janitor.py`, outside this repository) is the safety net: its `perf-cache` entry has the same 20 GB cap and 14-day age and sees each cache entry (`<kind>/<entry>`) as a unit. `DiskBudget` is the first line: it enforces the limit at write time, in the product workspace as well, which the janitor never touches. If the janitor deletes an entry, `DiskBudget` simply sees it gone; if the janitor ranks entries, it ranks by the same modification time.
