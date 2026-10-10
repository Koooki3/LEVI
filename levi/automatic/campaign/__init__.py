"""Multi-arm evaluation campaigns of AERI (design X3 §1).

A campaign compares several policies (arms) on one task: ``spec`` reads the
``campaign`` block of a job file and expands it into one ordinary AERI job
file per segment; ``schedule`` decides which arm runs which layout slots in
what order; ``journal`` keeps the campaign's hash-chained event log
(``levi.aeri.campaign_event.v1``); ``conductor`` moves the campaign through
its states (policy switch, operator confirmation, child run, sealing) and
recovers it after a crash to a place where a person confirms; ``switch``
stops and starts the policy services and checks passively that the right
one listens.

See docs/AUTOMATIC_CAMPAIGN.md ("Plan and schedule", "State machine and
recovery").
"""
