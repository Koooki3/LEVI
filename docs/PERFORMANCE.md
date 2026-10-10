

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
