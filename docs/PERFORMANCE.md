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
| `frame` | The original scan: `ffprobe` decodes every frame and reports its best-effort timestamp. Set `LEVI_PTS_SCAN=frame` to go back to it. |
| `packet` (default) | Read the container's packet timestamps instead (no decoding): 4 to 80 times faster on measured synthetic video, the same list for ordinary files (B-frames, variable frame rate). It trusts the container, so it steps aside whenever the container's packets are not one-to-one with the frames shown: a packet flagged discard (`D`) or corrupt (`C`), which an MP4 edit list or a start before zero produces; a negative timestamp; anything ffprobe reports on stderr (truncated or damaged files); a packet without a timestamp; no packets; times that are not strictly increasing. Then the frame scan runs, and a warning with the file name and the reason is logged. The cache file then also records `"scan": "packet"`, and a cache written by the other scan is not used. |

Any other value is refused. In both modes a video's list is kept in memory by
content hash (16 videos), so a v3 file shared by many episodes is scanned once
per process, and the file is hashed once per camera per episode (the hash is
passed to the scan instead of being computed again). Nothing about the checks
changes: every cache is still tied to the source hash and the later time
mismatch tolerance still applies.

`packet` became the default after the two scans were compared on the real
development videos (2026-10-10: 1,520 files, 278,952 frames, every list
identical, 7 times faster; see the results below). A cache written before the
switch has no `"scan"` key, so it counts as a frame-scan cache: the first read
rescans once and rewrites it in the packet layout. To repeat the comparison:

```
python -m levi.performance pts-compare <video or folder> [...] [--cores 4]
```

Both compare commands pin themselves to at most 4 CPUs at low priority (`--cores`, 0 leaves the process alone). `pts-compare` also lists, for each file, why the packets could not be used.

prints one row per file and exits 1 if any list differs or a file cannot be scanned.

Measured on 1,520 H.264 640x480 files from the development sets and the live
mirror (1,400 distinct contents; B-frames, many of them with a variable frame
rate): the packet list equalled the frame list value for value in all of them,
the frame count equalled what OpenCV decodes in all of them, no file needed the
fallback, and the packet scan took a median 21 ms against 148 ms (about 7
times; the page cache was warm, so a cold disk is not covered). The development
data holds no edit-list, negative-timestamp or damaged video, no other codec and
no shared v3 video file; those cases rest on the synthetic fixtures in
`tests/test_pts_scan.py`, and on the fallback to the frame scan.

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
| `float` | The original method: each frame is converted to float64 and summed. Set `LEVI_PIXEL_STATS=float` to go back to it. |
| `histogram` (default) | Count how often each 8-bit value occurs per channel (3 x 256 integers), then compute the four statistics once from the counts. The sums are exact integers and the variance has no cancellation. About 11 to 17 times faster per frame; min, max, mean and count equal the float method's to about 1e-11 or better. The standard deviation is compared as a variance: the float method computes it as `mean(x^2) - mean(x)^2`, which cancels when a channel is constant or nearly so, and its square root then turns a 1e-12 error into noise up to about 1e-6 (a constant 720p channel gives 2e-6 instead of 0). The histogram result is exact. Frames that are not 8-bit use the float method. |

Any other value is refused. `histogram` became the default after the two methods
were compared on 61 real development videos (2026-10-10): the largest
difference in min, max, mean or count was 9e-13, in variance 1.2e-12 (the std
difference itself 1.6e-12), and the histogram method ran a median 23 times
faster (3.5 to 30 times). The last digits of `meta/stats.json` therefore differ
from a file written by the float method, by far less than the training
normalisation can feel; for a constant channel the new value is the exact one.
To repeat the comparison (the command exits 1 if any file is
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

Its first user is the diagnostic of a Hugging Face dataset (`levi/hub_snapshots.py`): the downloaded metadata, parquet and (when asked) video files are kept in `<workspace>/.cache/levi/hub-snapshots/`, at most 20 GiB, least recently used snapshots evicted first, a snapshot being inspected never evicted; a snapshot is one Hub commit seen by one credential, so a changed dataset or another account is downloaded again, and every diagnostic still asks the Hub (with the caller's credential) which commit is current and how large the files are. The reservation is that exact size, so one that cannot fit is known before downloading and goes once to the old per-dataset folder; the response says so (`cache_note`, not kept in the report file). Costs against the old folder: a new commit is downloaded in full (the old folder fetched only changed files), and an interrupted download starts again. When the Hub cannot be reached, an earlier download in the old folder is inspected as it is (`cache_note` says so); access errors still fail. Folders left there by older versions are not moved; `levi clean` removes them. `DiskBudget` has no setting; it uses only the standard library and imports no other LEVI module. It needs a local file system (`flock`, `rename`); network file systems are not supported.

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

Reads can be refused too (`get` and `read` raise `BudgetRejected("lock_timeout")` or `"unavailable"`); treat any call as one that may say no.

| Behaviour | Detail |
| --- | --- |
| Limit covers data being written | A writer asks for room before it writes: `put` reserves `len(data)`, `writing(reserve=N)` reserves `N` (default a quarter of the budget), each plus the real size of the entry's `.meta` file (the key and version string, UTF-8, at most 4 KiB, so a long or non-ASCII key costs more). Committed entries plus all live reservations never exceed `max_bytes`, so least recently used entries are evicted to make the room, and a write that cannot be given room is refused before any data is written (`too_large`: could never fit; `busy`: other writers' reservations or readers' leases hold the rest). Bytes written through `open_file` stop at the reserve; anything written to the folder directly is measured at commit (and on demand by `charge(tmp)`) and refused if it is over. On the disk the folder therefore holds at most `max_bytes` plus a few hundred bytes of bookkeeping per live writer, however many processes write, with one exception each: a writer that ignores its reserve (no `open_file`, no `charge`) can use disk space until it commits, and entries evicted but not yet deleted are counted (see Crash safety) rather than free. |
| Entries | An entry is a folder named `<key hash>.<version hash>.<generation>.<size>` holding the payload and `.meta`. The name carries what the accounting needs, so there is no index that could disagree with the folder. Entries are immutable once committed: they are measured at commit, and a later check treats one that shrank as damaged. Rewriting a key creates a new generation; the old one is removed at the next commit or housekeeping that finds nobody reading it. `put`/`read` use one file, `data`, in the entry. `entries()` returns each key and version string whole. |
| LRU | An entry's last use is the modification time of its folder and of its `.meta` file; `get` and `read` refresh both, so a tool that looks only at files also sees the use. |
| TTL | An entry idle for more than `ttl_s` (default 14 days) is expired: never returned, dropped by the next housekeeping or when room is needed. Idle time is enforced when the cache is used; a cache nobody opens any more is not touched (see "Abandoned caches"). |
| Versions | Every entry is written under a version string (a schema, model or prompt version). Entries of any other version are stale: never returned, evicted before valid ones, deleted when idle for `stale_grace_s` (default 7 days) or at once by `purge_stale()`. |
| Read leases | `get` returns a `Lease` (shared `flock` on the entry). A leased entry is never evicted, replaced or deleted by this class; if the leases hold so much that a write cannot fit, the write is refused as `busy`. Release a lease with `close()` or `with`; one that is garbage collected is released too, and a forked child never inherits one. A path obtained any other way is only a hint. |
| Atomic writes | Data goes to a private folder under `.budget/w` and is renamed into place once, after an `fsync` (`durable=True`, default). A reader sees the whole entry or none. A failing `fsync` aborts the commit (`ENOSPC` and `EDQUOT` as `no_space`, anything else is raised); a failing `fsync` of the cache folder after the rename takes the entry out of sight again before the error is raised; a directory that does not support `fsync` is ignored. A failed rewrite can have removed the key's older generation already (a miss for a cache). |
| Crash safety | Every writer holds an exclusive `flock` on its record under `.budget/w` while it lives. A record whose lock can be taken belongs to a dead writer (also after SIGKILL; pid reuse, file times and the clock do not matter) and is reclaimed with its data, as is a scratch folder that has no record; a live writer is never reclaimed, however long it takes. Deleting an entry renames it into `.budget/trash` (the size is in the name, and counts against the limit until removal finishes) first, so a crash mid-delete never leaves a half-empty entry that looks valid. An entry whose `.meta` is missing or that is smaller than when it was committed (partly deleted in place by another tool) is damaged and dropped by `maintain()` and by `get`. Housekeeping runs when the object is created, in `maintain()`, and at most every `housekeeping_s` (default 10 minutes) during writes and reads, so a long-running process reclaims dead writers too. `maintain()` also prunes to the current limits. |
| Concurrency | Every change runs under an exclusive `flock` on the cache folder itself. Threads and processes exclude each other; the kernel drops the lock when a process dies. There is no lock file for a cleaner to delete. A forked child drops its copy of every lock descriptor at once. |
| Disk full | `ENOSPC` or `EDQUOT` while writing is `BudgetRejected("no_space")`, and so is a reservation that would leave less than `min_free_bytes` free on the disk once the other live writers' reservations are also written (checked once, before anything is written, counting what eviction gives back). Nothing partial is left. If the folder or its hidden subfolder is removed, the next call recreates it; a writer whose scratch folder was removed gets `BudgetRejected("reclaimed")`. |

Scale. Every write and read lists the cache folder and every entry costs one `stat`, so cost grows with the number of entries: fine for thousands (one entry per episode), not for one entry per frame. Keep entries coarse. Creating an instance runs `maintain()`, which walks every entry's files once under the lock; processes that start often can pass `recover=False` and let one long-lived process call `maintain()`.

**Abandoned caches.** Idle time and the limit are enforced by whoever uses the cache, so a cache that nobody opens again keeps its bytes. `claim_abandoned(root, idle_s)` is the interface for an outside cleaner that wants to remove such a cache as a whole: it yields `True`, while holding the cache's lock, only if the folder is a `DiskBudget` cache, no writer is alive, no entry is leased, and nothing in it (the folder, each entry folder, each `.meta`) was touched for `idle_s` seconds; the cleaner then renames the folder to a sibling name in one `rename` and deletes that, and the next use recreates the cache. `is_abandoned(root, idle_s)` answers the same question without keeping the lock (a hint only).

**Cleaning the folder from outside.** A scheduled clean-up script may still work on the folder as a safety net, if it follows two rules: skip names that start with `.` (they are the bookkeeping, `.budget`), and treat a whole entry folder as one unit. Because the last use is recorded in files inside the entry as well as on the entry folder, a script that ranks entries by the newest file time sees the same order. If it deletes an entry, this class simply finds it gone; if it deletes part of one, the entry is found damaged and the rest removed.

## Opt-in workload admission kernel

`levi.live.workload_broker.WorkloadBroker` is an in-process admission kernel for
the existing live controller, not a new daemon or resource scheduler. It does
not probe the GPU, acquire a flock, start workers, stop processes, call a model
or contact a robot. The existing controller and resource driver remain the
physical owners. This checkpoint is not wired into the controller or services.

The kernel reuses `aeri.WorkloadSpec`, `Deadline`, `ResourcePressure`,
`ResourceLease`, `AdmissionRejected` and `WORKLOAD_PRIORITY`. Its bounded queue
orders `policy_realtime`, `judge_critical`, `resident_group`, `vision_light`,
`background_annotation`, then `batch`; within a class, the earliest due time
wins, followed by arrival order. CPU-only requests are explicitly refused by
this GPU-only kernel; `any` selects GPU without a hidden CPU fallback. P0 safety,
watchdog and stop paths never call or wait for this object.

`submit` freezes a workload's run, episode, profile SHA-256, complete p99 duration
and memory estimate. `admit` requires fresh same-run and same-clock pressure, an
open existing gate and a caller-authorized `SafeWindow` for the same episode and
profile. Window and ownership proofs also carry the current broker identity; ownership
proofs bind the current lease epoch (and worker exit proofs its lease ID).
Unknown VRAM, unknown policy presence, unknown/loading model state, old/future
pressure, a closed or contradictory gate, missing memory or duration estimates
all fail closed. A free flock is not proof of free VRAM or a safe control window.
Critical judgement refuses a cold start. The complete duration estimate must
include startup/wake, inference and result delivery; p99 plus a margin must fit
both the deadline, the budget including queue time, and the safety window.

Only one GPU lease/reservation physically owns the slot at a time, including
forward/reset rotation. New broker instances default to unknown ownership.
`prove_idle` needs an `IdleProof` from the trusted controller's actual previous
worker, descendant and GPU checks before first admission. It must never be
constructed from a model response or guessed because a queue is empty.
`bind_worker` records exact PID, process start ticks and boot identity.
`accepts_result` requires that worker binding, the exact current lease/epoch,
an unrevoked permission and an unexpired deadline/lease.

Cancellation, expiry, pressure failure and priority preemption withdraw result
permission without releasing the physical slot. `preemption_request` exposes a
`request_boundary` or `cooperative` request to the owner; it issues no signal or
command. A lower preemptible worker yields to higher priority only after
`release_after_exit` receives a fresh `ExitProof` for that exact worker, all
its descendants and its owned GPU allocation. Live and unknown owners stay
reserved. `release_unstarted` needs explicit fresh confirmation that the
reservation never spawned a worker. A late worker may still be bound after
cancellation so ownership cannot disappear. An already running policy is never
preempted by a model gate. Lease IDs include a fresh broker identity and each
grant advances the epoch; old results and old exit proofs cannot release a new
owner.

The queue defaults to 128 requests, capped at 1024; full queues apply explicit
backpressure. Expired requests produce bounded rejection notices with an
observable dropped-notice count. Snapshot and preemption reads are local state;
there is no new persistent scheduler state. Atomic calls support caller threads;
cross-process coordination still uses the existing driver and GPU ownership.

**Validation boundary:** CPU/mock tests cover concurrent admission, priority,
preemption, actual-exit proof requirements, deadlines and queue elapsed time,
VRAM margins, pressure/window freshness, old epochs and restart uncertainty.
They do not prove an actual policy quiesce, GPU memory handoff or useful p99.
Real `running` still closes the existing model gate; no busy state is removed
or renamed to waiting. No trustworthy runtime policy-quiesce proof has been
wired, so autonomous intervention before the step limit remains unverified.
Controller wiring, real pressure/exit probes, GPU measurements and robot tests
are separate integration gates.


## Resident runtime and serial request ownership (opt-in)

`levi.live.runtime_ownership.RuntimeOwnership` composes the admission broker with
a separate request slot. A resident model is bound to the original physical
`ResourceLease` and exact PID/start ticks/boot identity, plus an instance ID and
runtime epoch. One broker can have only one bound request authority; losing the
Python wrapper does not clear that binding. Request tokens carry that runtime and a monotonically increasing
request epoch; their deadline is bounded by the physical lease. Fresh trusted
observations must establish that the exact runtime is alive and its actual
resource-driver/flock lease is still held. Unknown or changed identities latch
closed until verified physical exit. Restarted owners cannot adopt old tokens.

HTTP completion, cancellation, timeout and calling-thread completion only revoke
result permission (`revoke_request`). A request slot remains occupied until
`finish_request` receives a request-specific `RequestStopProof`: the actual backend
request has stopped and **all** request workers, including threads, have exited.
A backend that ignores cancellation blocks further requests. Late completion may
clear the request slot but its result is rejected. Same-runtime request rotation
never releases the resident GPU owner. Physical handoff still delegates to the
original broker's strict process, descendant and owned-GPU `ExitProof`; an active
request must first have its own stop proof. No caller may substitute HTTP status
or a finished calling thread for backend ACK, or fabricate a GPU release flag.

RuntimeOwnership itself is an in-process contract foundation. It starts no
probes, threads or services and does not change gates or configuration.
Trusted integration must supply real backend completion ACKs,
track all actual request workers and serialize lifecycle changes with these calls;
constructor-local locks are not cross-process resource ownership. The existing
request owner must retain its slot until every actual working thread ends. CPU
fakes cover cancellation ignored by a live worker, expiry, PID reuse, old epochs,
restart uncertainty and resident ownership retained across requests. They provide
no real GPU handoff or policy quiesce evidence; the real `running` gate stays shut.

### Explicit bounded runtime bridge

`levi.live.runtime_bridge.RuntimeBridge` combines these existing contracts;
`Controller(runtime_bridge=None)` preserves the default behavior. No CLI,
environment setting, service configuration or real endpoint installs the bridge.
A trusted composition must inject actual resource-driver, kernel flock and exact
PID/start/boot/port-owner observations, plus a frozen exclusive single-engine
backend capability. Unknown evidence or a changed driver generation closes
admission. It never imports a lab scheduler or accepts an environment claim as
ownership evidence.

The trusted launcher calls `admit_runtime` on the original broker before launch,
then `bind_runtime` for the actual owned leader. Strict Controller paths do not
launch, adopt an external/restart runtime or fall back to manual admission.
Background model workers are explicitly paused until they can use this same
authority; the bridge does not claim that all producers are wired.

Both online judgement and the AERI endpoint join all their actual local worker
threads and wait for a same-runtime, same-request-epoch backend barrier before
publishing success. `VllmPauseBackend` supports only the independently verified
synchronized `/pause` wait/abort barrier and `/resume`, through an injected pinned
loopback transport. Disconnect, `/abort_requests`, `/is_paused` and sleep are not
completion proofs. Exact authority is checked before and after the barrier;
barrier time counts against the request deadline. Timeout or unknown ACK revokes
the result and retains Judge busy, request slot and physical owner. A later valid
barrier, or strict process/descendant/GPU exit together with local worker exit,
can resolve the slot without reviving its result. P0 local revocation and a
physical-release attempt do not wait for the lifecycle mutex or drainer.

This bridge supports a finite physical lease only. Expiry cannot be extended
through a new wrapper or broker. Its strict sleep hook ends admission and keeps
the physical owner and flock; it does not release GPU ownership or implement
long-lived sleep/wake renewal. Physical release requires the original broker's
strict exit proof. Permanent residency and policy/reset role groups require a
separate contract. The supplied tests are CPU/memory fakes, including both real
online entrypoints. Real CPU IPC/flock assembly, backend capability validation,
GPU latency and robot validation remain separate checks; none is claimed here.


## Controlled resident groups and bounded renewal (opt-in)

`resident_group` is an explicit physical reservation class below policy and
critical judgement, above light/background/batch work. Its `ResidentGroupSpec`
declares each role and distinct owned capability. `RuntimeOwnership.bind_group`
binds a single supervisor and the complete immutable member PID/start/boot
identities. A `GroupObservation` must name the original driver lease, generation
and session, prove driver/flock ownership and complete membership, and establish
that every member is an owned, running backend. A supplied external server PID
alone provides none of these capabilities; production adapters must establish
them. This kernel does not adopt external services or run such probes.

`begin_role_request` shares one slot across forward, judge, reset and optional
background roles. Every request needs a fresh `RoleWindow` for the current
run/profile/episode/receiver clock, policy epoch and role. Its separate
`authorization_epoch` strictly increases per request; a policy epoch also
increases across episodes. All members' backend idle barriers and all local
request workers must be proven quiescent. Completion needs a new complete group
observation after the request stop proof. Client completion cannot supply it.
A model gate does not revoke the physical resident reservation; it still cannot
create a role window, authorize a request or change the existing gate.

`renew_resident` explicitly reauthorizes the same original owner while both
owner and broker locks are held. The old permission must remain valid,
nonrevoked and known, no request may be active, all members must be idle, and a
new physical `SafeWindow` (separate from episode role windows), deadline, fresh
pressure and memory margin must be valid with no higher priority handoff. The
frozen group memory estimate must include every member, startup/compile/switch
peaks and headroom; renewal keeps that reservation and checks additional free
headroom. Validation and token construction precede all lease/runtime changes.
Each segment remains at most ten minutes; both epochs rise. Old tokens cannot
cancel or release the new epoch. Heartbeats, expiry and unknown identity cannot
renew or resurrect ownership.

`GroupExitProof` must prove every original member and its descendants exited and
released its owned GPU allocation, together with the supervisor's complete
exit proof. The original broker enforces this too. Complete group exit can
resolve a stranded request without publishing its result. No method waits for a
model mutex or invokes backend work. Dedicated fake tests cover role races,
renew races, rejected/failed atomic renewal, stale tokens, incomplete idle/exit
proofs and legacy single-worker compatibility. Real adapters, process/flock
assembly, GPU memory and barrier timing, and robot integration remain separate
validation gates; this extension stays disabled until explicitly composed.

## Confirming resident model exit

The existing live controller releases its GPU flock only after `Vllm.stop()`
confirms the original PID/start ticks/boot owner has exited, a complete
`/proc` scan finds no live member of its process group, and a successful
`nvidia-smi` compute-process scan finds none of its remembered PIDs. PID reuse
does not authorize signalling the new process. Unreadable identity, boot,
process-group or GPU evidence, malformed GPU PID rows, and owner-record
read/write errors keep the stop unconfirmed. The original record and identity
are retained, with pending members saved when possible; new launches are
refused until a later complete stop confirms exit. Pending retries repeat the
process-group and GPU checks. Status-only GPU listings remain permissive.

Launch ownership is reserved in memory and an atomic pending record before
executing a command. An unreadable/unwritable reservation prevents launch.
Returned launcher PID/start/boot/group and the new server PID/identity are
retained when readable; timeout, nonzero exit, missing/unchanged PID file,
unknown identity, or final record-write failure keeps ownership unresolved.
An incomplete launch does not signal a live/unknown server or permit another
launch. Its recorded owner can recover after exit only with complete group
and GPU checks, including an observed launcher/group. Without a server PID,
even launcher exit is insufficient to confirm all detached children exited.
Preparation failures before command execution still use ordinary backoff.
Yield probes enumerate the verified record's process group and recheck its
identity. A mismatched/unreadable PID file produces a conservative probe error;
another process named by that file is never exempted as this service's GPU user.

Shutdown retains an unresolved record and the controller's lock descriptor
while the controller is alive. Process exit necessarily closes that descriptor;
a live child that inherited the same open file description may still hold
the flock. Shutdown or an HTTP response is not a physical-release or backend
idle ACK. These changes do not mount the opt-in runtime bridge, open the
`running` gate, or prove that a backend request stopped.

CPU regressions use fake GPU query sources and owned CPU children with a
scratch flock, including a child that outlives its leader. They verify
conservative ownership retention and later recovery, not actual GPU memory
handoff, backend pause ACK timing or robot behavior.

A supervisor that cannot acquire the lock or verify its inherited descriptor
keeps a live original owner record as unresolved. Construction neither stops
that server nor relabels it external; later explicit stop/tick confirmation
is still required before a new launch.

These existing checks cover the process group and remembered PIDs. A future
runtime bridge must separately establish complete descendant ownership,
including workers that leave that group.


The original authority exposes `owns_exact_request(request)` as an identity-only drain-ownership query, including revoked/late work still awaiting drain; it grants no permission or idle proof. `accepts_exact_request(request, observation, now_ns=...)` applies the original result checks only after confirming the active object with `is`, under the same lock. Equal token copies receive no exact permission. Existing `accepts_result`, revoke and finish keep their public value semantics for older callers; this addition does not install owned SDK adapters or prove hardware readiness.

## Owned SDK runtime adapters (software candidate, opt-in)

`levi.live.owned_runtime` supplies an explicit private-IPC model-worker launcher and
`OwnedGroupObserver`; `levi.automatic.owned_runtime.OwnedRoleRuntime` is a script
companion to the existing `RuntimeOwnership` and `WorkloadBroker`. It creates no
second scheduler. Existing inference cycles can instead bind each worker to the
same authority, dispatch an already granted role token, and use the group
observer for their original request-stop proof. No runtime adapter is installed
by default, and no existing external policy or VLM server can be attached.

The launcher requires original broker admission, the exact active canonical
resource-driver lease and registered live child, an inherited exclusive GPU
flock descriptor, and an already delegated cgroup v2 supervisor root with
distinct empty role leaves. It records its own actual child with `levi.children`
in an explicit runtime state directory before opening the model-import gate.
Complete root/leaf membership, PID/start/boot identities and remembered engine
descendants are checked again when observing idle or exit. Additional processes,
changed containment, unknown identity or an unresolved startup refuse further
launches. Process/HTTP completion and free VRAM do not supply these proofs.

The Torch worker synchronizes its actual CUDA device after inference and refuses
an idle ACK while Python request threads remain. The private vLLM worker calls
`AsyncLLM.pause_generation(mode="wait", clear_cache=False)` after its single
generate coroutine has exited. Its frozen implementation resolves the scheduler
Future through `collective_rpc("synchronize_device")`; the installed version and
engine/executor/worker source manifest must match the separately reviewed copy.
This SDK path replaces the HTTP `VllmPauseBackend` dependency for this owned
adapter only: it has no HTTP server or external generation ingress. A source
hash checks code consistency; the actual private launch receipt, containment and
SDK ACK establish ownership and idle. The factory and its full dispatch closure
still require independent review. Only one engine/device topology is supported;
external KV connectors and GPU objects crossing the CPU IPC boundary are refused.

An SDK timeout, wrong-epoch ACK or unknown observation retains the original
execution slot. A revoked late result may drain that slot but cannot be
published. Ingress close and token revocation do not wait for the model mutex.
Resident renewal delegates to the original kernel and updates its lease/runtime
epochs; it requires the same fresh all-member idle evidence. There is no
automatic renewal, deployment or service restart.

The first trial's JAX policy provider remains unavailable.
`levi.live.owned_jax.JaxDependencyFence` can wait for output/retained/live-array
dependencies and JAX side effects, but deliberately emits no backend-idle ACK.
The complete standard-infer dispatch closure (including retained RNG and all
transforms) has not been frozen; per-array readiness is insufficient. The new
adapters also provide no physical robot-hold or paused-camera proof.

The current operator session is outside the writable user-manager delegation,
and the trusted delegated supervisor bootstrap has not been installed. A future
explicit deployment must preserve the canonical driver/child ancestry and locked
open-file descriptor inside an approved delegated supervisor; this software
candidate does not create cgroups or systemd units. CPU fake tests exercise the
SDK ACK and original group kernel. GPU models, real process groups, installed
services and robot operation have not been exercised by those tests.

The private factory contract is now `build(device=..., sdk=...)` for Torch.
An explicit `source_root` and complete Python `source_hashes` manifest pin the
factory, parent initializers and local dependencies. Verified bytes are directly
compiled by the owned loader, so timestamp/size-compatible .pyc files are never
executed. Preloaded modules, different origins/loaders, unfrozen imports and
standard-library names resolving outside the interpreter are refused. SDK
objects are explicitly injected; a source hash alone is not a trusted loader
or approval of the complete dependency closure. No production factory is installed.

SDK execution now requires complete `torch_source_hashes` and, for vLLM,
`sdk_source_hashes` manifests of the actual installed Python packages. The eight
barrier hashes must belong to that full package manifest. Package resolution,
loader and executed function code must match the verified bytes before the
factory runs and around SDK idle acknowledgments; preloaded/shadow packages
are refused. vLLM factories receive `build(device=..., sdk=...)` too.
Unbound native/third-party imports are refused. No complete real SDK/native
closure or owned SDK child bootstrap has been installed, so the real entry
remains closed. Tiny CPU package tests measure only the binding software;
complete real-package manifest cost and worker provenance remain unmeasured.

Frozen module imports now share one resolver for static, relative, fromlist and
normal dynamic imports, including cached modules. Per-bundle importlib/builtins
facades validate returned dependencies; unbound low-level dynamic loader APIs
are refused. Standard-library origin alone does not approve returned third-party modules.

Critical SDK bindings are sealed before the factory module initializes.
Factory return and both sides of every idle ACK recheck the exact SDK module/class
attributes, function code, global aliases, defaults, closures and referenced
containers, including the actual Torch synchronize. Dynamic critical state that
cannot retain these bindings is refused. The seal does not certify unimplemented
remote/native SDK worker provenance. Ordinary tiny factory.load-to-serve tests
exercise this check without replacing load or require_function.

All eight declared vLLM barrier modules are imported through the owned loader
before the seal, so a lazily imported barrier module cannot be rebound by the
factory first; a sealed module that leaves `sys.modules` is refused at the next
check. Torch and vLLM idle checks read the bundles and seal from loader-owned
handles (`load()` returns a `FrozenVllmEngine` for vLLM), never from attributes
of the factory-returned model or engine, so a fresh seal stored on the engine
by factory-started code is ignored. The worker
interpreter runs with `-I -B -X pycache_prefix=os.devnull`: `-B` alone only stops
writing caches, and a same-mtime/size stale `__pycache__` would otherwise
replace the loader and barrier checks themselves. The worker entry refuses any
other interpreter before reading its bootstrap. Not covered: a sourceless
legacy `.pyc` replacing a deleted `.py`, native extensions, lazily imported
non-barrier modules, and the supervisor's own interpreter.

The worker no longer resolves `levi` through the inherited working directory,
`PYTHONPATH` or user site: it runs in isolated mode (`-I`) and imports `levi`
from the supervisor's own resolved checkout root, passed explicitly. Before the
bootstrap read, the child refuses unless every loaded `levi` module is an
ordinary source module under that root. A shadow `levi` package can therefore
not replace the loader that binds SDK origins. The interpreter, its
site-packages `.pth` files and native extensions remain trusted.

Because `-I` removes the working directory and `PYTHONPATH`, the factory's
`source_root` must be importable through the bootstrap: after the frozen type
check the worker appends that declared root to `sys.path`. It is appended, so
the worker root, standard library and site-packages keep precedence, and a
frozen name resolved elsewhere first is refused by the origin check. An SDK
distribution placed under the factory root is found the same way; its hash
manifest still constrains the content.

Not covered by these checks:

- SDK-started processes. vLLM's EngineCore and executor workers run in
  processes the SDK creates itself; their imports do not go through the owned
  loader, so the barrier-file hashes are verified only in the worker process
  that loads them. `-X pycache_prefix` is inherited only by `fork`: measured
  with `multiprocessing`, `spawn` and `forkserver` children report
  `sys.pycache_prefix` as `None` (they keep `-I` and `-B`), so such children
  can read stale bytecode. This is documented, not mitigated.
- The vLLM idle ACK has no local-thread liveness check; the Torch worker has
  one.
- The seal is not a sandbox. Reviewed factory code can import standard-library
  modules such as `sys`, `gc` or `ctypes` and reach or change objects outside
  the checked bindings. The final defence is human review of the factory.

Factory/SDK source loading uses one transaction. Failed hook installation,
module initialization, factory build or engine validation rolls back every
owned hook and exact newly created module. Successful private worker exit
closes the same lifetime in reverse order; explicit vLLM callers pass the
handle returned by `load()` to `close_engine`. External/preloaded/replacement modules are preserved,
and closed bundles refuse subsequent imports. This cleanup concerns Python
source lifetime only; physical device ownership still needs the kernel's
complete fresh group-exit proof.

Critical vLLM methods now resolve to the original pre-factory class descriptors: `__func__` and `__self__` must match; instance shadowing is refused even when the replacement was compiled in the same bundle. Ordinary class/static method semantics are retained. Functions' globals, defaults and closures may refer to exact passive `SimpleNamespace` holders: callable paths through namespaces/dicts/lists/tuples remain bound, while ordinary numeric counters can change. Unknown active objects, unsupported containers or unbound critical callables are refused. These checks bind the reviewed call sites; the loader is not a Python sandbox, and no real SDK/native/child bootstrap support is claimed.

Owned workers expose `collect_sdk_completion(request)` returning the original submitted token, strictly matched `SdkIdleAck` and CPU result. Timeout preserves pending work; an invalid ACK/payload or unknown inventory permanently withdraws that pending result. A genuine cached ACK can later drain the same request after a fresh successful probe. The worker never begins/finishes a kernel request or treats caller return as stop proof.

`BorrowedRoleExecution(authority, runtime, lease, observer)` executes a Cycle-issued grant via `dispatch_existing(request, payload, role=..., episode_id=..., policy_epoch=...)` and `collect_existing(request)`. It never begins, finishes or renews a grant. `BorrowedCompletion` carries the original request, SDK ACK, stop proof, fresh group observation and CPU result; Cycle alone finishes using another independent observation. Timeout keeps pending; bad ACK/probe withdraws results while preserving same-request cached drain. Late/revoked/closed results are None. Only genuine SDK ACK plus fresh complete group idle forms stop proof; a whole-worker exit is not converted to this running-group proof contract. Original `OwnedRoleRuntime.begin/collect` remains a separate legacy entry. No HTTP fallback, production mount or new SDK provider is installed.

The dedicated `test_borrowed_cycle_execution.py` check requires a fresh interpreter and explicit `LEVI_JOINT_KERNEL_ROOT`. It loads the original Kernel/Cycle and candidate owned files from their recorded source paths. CPU fake IPC verifies exactly one original begin/finish, genuine matching SDK receipt structure, and an independent final whole-group observation. A peer becoming busy after the receipt prevents publication and retains the original slot. This is software combination evidence, without a production provider, real SDK/native bootstrap, physical hold or Orchestrator mount.

Borrowed execution requires the exact Kernel queries described in the runtime ownership section above. Equal token copies are rejected before observer, worker or revoke side effects. Dispatch/result permission uses `accepts_exact_request`; collection and revocation require `owns_exact_request` for the same original object. Revoked/late originals may still drain genuine ACKs with result None. A Kernel missing the exact contract is refused; there is no value-acceptance fallback.
