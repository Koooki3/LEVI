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

`levi.performance.budget.DiskBudget` keeps one cache folder inside a byte limit (and optionally an entry limit) by itself. It exists because a cache that only grows eventually fills the disk: evidence for a few hundred episodes is some GB, for a few thousand tens of GB, and every new schema, model or prompt version starts a second copy. The limit is enforced when an entry is written, by the process that writes it, not by a cleaner that may run too late.

It is a library only. **Nothing in LEVI uses it yet**; it changes no behaviour and has no setting. It uses only the standard library and imports no other LEVI module.

```python
from levi.performance.budget import DiskBudget, BudgetRejected

cache = DiskBudget.from_cap_gb("/path/to/evidence-cache", 20, version="schema-3")   # 20 GiB
cache.put("episode-17", png_bytes)                  # reserves room first, evicts the least recently used if needed
cache.read("episode-17")                            # bytes or None; a hit counts as a use

with cache.writing("episode-18", reserve=40_000_000) as tmp:    # an empty folder to fill
    with cache.open_file(tmp / "0001.png") as f:                # stops at the reserve
        f.write(frame)
# a clean exit renames the folder into place as the entry

with cache.get("episode-18") as lease:              # a read lease: the entry cannot change or go while held
    first = (lease.path / "0001.png").read_bytes()

try:
    cache.put("x", data)
except BudgetRejected as why:       # why.reason: too_large | busy | no_space | no_output | reclaimed | unavailable | lock_timeout
    ...                             # the caller carries on without the cache
```

| Behaviour | Detail |
| --- | --- |
| Limit covers data being written | A writer asks for room before it writes: `put` reserves `len(data)`, `writing(reserve=N)` reserves `N` (default a quarter of the budget). Committed entries plus all live reservations never exceed `max_bytes`, so least recently used entries are evicted to make the room, and a write that cannot be given room is refused before any data is written (`too_large`: could never fit; `busy`: other writers' reservations or readers' leases hold the rest). Each entry also costs a small `.meta` file; a reservation includes 256 bytes for it. Bytes written through `open_file` stop at the reserve; anything written to the folder directly is measured at commit (and on demand by `charge(tmp)`) and refused if it is over. On the disk the folder therefore holds at most `max_bytes` plus a few hundred bytes of bookkeeping per live writer, however many processes write. Without `open_file` or `charge`, a writer that ignores its reserve can use disk space until it commits. |
| Entries | An entry is a folder named `<key hash>.<version hash>.<generation>.<size>` holding the payload and `.meta`. The name carries what the accounting needs, so there is no index that could disagree with the folder. Entries are immutable once committed; rewriting a key creates a new generation and removes the old one as soon as nobody reads it. `put`/`read` use one file, `data`, in the entry. |
| LRU | An entry's last use is the modification time of its folder and of its `.meta` file; `get` and `read` refresh both, so a tool that looks only at files also sees the use. |
| TTL | An entry idle for more than `ttl_s` (default 14 days) is expired: never returned, dropped by the next housekeeping or when room is needed. |
| Versions | Every entry is written under a version string (a schema, model or prompt version). Entries of any other version are stale: never returned, evicted before valid ones, deleted when idle for `stale_grace_s` (default 7 days) or at once by `purge_stale()`. |
| Read leases | `get` returns a `Lease` (shared `flock` on the entry). A leased entry is never evicted, replaced or deleted by this class; if the leases hold so much that a write cannot fit, the write is refused as `busy`. Release a lease with `close()` or `with`; one that is garbage collected is released too. A path obtained any other way is only a hint. |
| Atomic writes | Data goes to a private folder under `.budget/w` and is renamed into place once, after an `fsync` (`durable=True`, default). A reader sees the whole entry or none. A failing `fsync` aborts the commit (`ENOSPC` and `EDQUOT` as `no_space`, anything else is raised); a directory that does not support `fsync` is ignored. |
| Crash safety | Every writer holds an exclusive `flock` on its record under `.budget/w` while it lives. A record whose lock can be taken belongs to a dead writer (also after SIGKILL; pid reuse, file times and the clock do not matter) and is reclaimed with its data; a live writer is never reclaimed, however long it takes. Deleting an entry renames it into `.budget/trash` first, so a crash mid-delete never leaves a half-empty entry that looks valid; an entry whose `.meta` is missing (deleted in place by another tool) is dropped. Housekeeping runs when the object is created, in `maintain()`, and at most every `housekeeping_s` (default 10 minutes) during writes and reads, so a long-running process reclaims dead writers too. `maintain()` also corrects entry sizes and prunes to the current limits. |
| Concurrency | Every change runs under an exclusive `flock` on the cache folder itself. Threads and processes exclude each other; the kernel drops the lock when a process dies. There is no lock file for a cleaner to delete. Do not `fork` inside the `with` blocks of this class (the child would inherit the lock). |
| Disk full | `ENOSPC` or `EDQUOT` while writing is `BudgetRejected("no_space")`, and so is a reservation that would leave less than `min_free_bytes` free on the disk (checked once, before anything is written, counting what eviction gives back). Nothing partial is left. If the folder or its hidden subfolder is removed, the next call recreates it; a writer whose scratch folder was removed gets `BudgetRejected("reclaimed")`. |

Scale. Every write and read lists the cache folder and every entry costs one `stat`, so cost grows with the number of entries: fine for thousands (one entry per episode), not for one entry per frame. Keep entries coarse.

**Cleaning the folder from outside.** A scheduled clean-up script may still work on the folder as a safety net, if it follows two rules: skip names that start with `.` (they are the bookkeeping, `.budget`), and treat a whole entry folder as one unit. Because the last use is recorded in files inside the entry as well as on the entry folder, a script that ranks entries by the newest file time sees the same order. If it deletes an entry, this class simply finds it gone; if it deletes part of one, the missing `.meta` is noticed and the rest removed.
