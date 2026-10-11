"""Bind fresh paused sources to the original journal and event Ledger.

Injection only: no camera, model transport, extra frame history or authority.
The committed pause is produced by the existing physical/backend proof cycle.
"""

from dataclasses import dataclass

from ..intervention import InterventionUnproven


@dataclass(frozen=True)
class PauseBinding:
    run_id: str
    episode_id: str
    role: str
    policy_epoch: int
    control_epoch: int
    transaction_id: str
    sequence_no: int
    committed_ns: int
    clock_domain: str
    source_serial: int


@dataclass(frozen=True)
class PausedEvidence:
    binding: PauseBinding
    proof: object
    spec: object
    images: tuple


class PausedFrames:
    def __init__(self, ledger, journal, context, fence):
        self.ledger, self.journal, self.context, self.fence = (
            ledger,
            journal,
            context,
            fence,
        )

    def _commit(self, reason):
        ctx = self.context()
        journal = self.journal
        commit = next(
            (e for e in reversed(journal.events) if e.record == "committed"), None
        )
        if (
            ctx is None
            or ctx.role not in {"forward", "reset"}
            or journal.open_transaction is not None
            or commit is None
            or commit.reason != reason
            or commit.to_state != journal.state
            or (commit.run_id, commit.episode_id, commit.episode_role)
            != (self.ledger.run_id, ctx.episode_id, ctx.role)
            or journal.run_id != self.ledger.run_id
            or type(ctx.policy_epoch) is not int
            or ctx.policy_epoch < 0
            or type(commit.policy_epoch) is not int
            or commit.policy_epoch != ctx.policy_epoch
            or type(commit.control_epoch) is not int
            or commit.control_epoch != journal.control_epoch
            or type(commit.mono_ns) is not int
            or commit.mono_ns <= 0
            or commit.clock_domain != self.ledger.domain
            or self.ledger.observer.clock.domain != self.ledger.domain
            or type(commit.sequence_no) is not int
            or commit.sequence_no < 0
            or not isinstance(commit.transaction_id, str)
            or not commit.transaction_id
        ):
            raise InterventionUnproven("committed pause/resume identity is unproven")
        return ctx, commit

    def _fresh(self, commit, observation):
        current = self.ledger.observer.latest
        if (
            current is None
            or self.ledger.observer.original is not observation
            or type(current[2]) is not int
            or current[2] < commit.mono_ns
            or type(observation.get("observed_ns")) is not int
            or not commit.mono_ns
            <= current[2]
            <= observation["observed_ns"]
            <= current[3]
        ):
            raise InterventionUnproven(
                "fresh observation after committed pause/resume required"
            )

    def stage(self, folder, observation):
        try:
            ctx = self.context()
            if ctx is None or ctx.role not in {"forward", "reset"}:
                raise InterventionUnproven("paused source context is missing")
            if self.journal.state == f"{ctx.role.upper()}_JUDGING":
                _, commit = self._commit("inference_paused")
                if self.fence.current is not None:
                    raise InterventionUnproven(
                        "paused source still has motion authority"
                    )
                self._fresh(commit, observation)
                self.ledger.stage_paused(folder, observation, commit.mono_ns)
            elif self.journal.state == f"{ctx.role.upper()}_ACTIVE":
                if self.ledger.rows and self.ledger.rows[-1].phase == "paused":
                    _, commit = self._commit("inference_resumed")
                    pause = next(
                        (
                            e
                            for e in reversed(self.journal.events)
                            if e.record == "committed"
                            and e.reason == "inference_paused"
                            and e.episode_id == ctx.episode_id
                        ),
                        None,
                    )
                    if (
                        pause is None
                        or type(pause.policy_epoch) is not int
                        or commit.policy_epoch <= pause.policy_epoch
                        or commit.sequence_no <= pause.sequence_no
                    ):
                        raise InterventionUnproven(
                            "resume must reserve a new policy epoch"
                        )
                    self._fresh(commit, observation)
                    self.ledger.stage_resumed(folder, observation, commit.mono_ns)
                else:
                    self.ledger.stage(folder, observation)
            else:
                raise InterventionUnproven("source is outside an active/paused episode")
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven("paused stage observer failed") from exc

    def _prepared(self, request, observation):
        ctx, commit = self._commit("inference_paused")
        if (
            self.journal.state != f"{ctx.role.upper()}_JUDGING"
            or self.fence.current is not None
        ):
            raise InterventionUnproven("judge source lacks committed exclusive pause")
        self._fresh(commit, observation)
        current, rows = self.ledger.observer.latest, self.ledger.rows
        last = rows[-1] if rows else None
        lo, hi = request.get("observed_from_step"), request.get("observed_through_step")
        if (
            last is None
            or last.phase != "paused"
            or (current[0], current[1], current[4], current[5])
            != (last.epoch, last.serial, last.control_step, last.clock_domain)
            or (ctx.stream, ctx.episode_id, ctx.role, ctx.last_step)
            != (last.epoch, last.episode_id, last.role, last.control_step)
            or (
                request.get("run_id"),
                request.get("episode_id"),
                request.get("episode_role"),
            )
            != (last.run_id, last.episode_id, last.role)
            or type(lo) is not int
            or type(hi) is not int
            or not 0 <= lo <= hi
            or hi != last.control_step
            or request.get("observed_through_ns") != last.observed_ns
            or observation.get("observed_ns") != last.observed_ns
            or not 0
            <= self.ledger.observer.clock.now() - last.observed_ns
            <= self.ledger.config.max_age_ns
        ):
            raise InterventionUnproven(
                "judge request is not bound to staged paused source"
            )
        return PauseBinding(
            last.run_id,
            last.episode_id,
            last.role,
            commit.policy_epoch,
            commit.control_epoch,
            commit.transaction_id,
            commit.sequence_no,
            commit.mono_ns,
            commit.clock_domain,
            last.serial,
        )

    def prepare_evidence(self, request, observation):
        try:
            self._prepared(request, observation)
            return True
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven("paused evidence observer failed") from exc

    def freeze(self, request, observation, module, judge):
        """Return frozen CPU payload for an already granted owned worker, no HTTP."""
        try:
            binding = self._prepared(request, observation)
            proof, spec, images = self.ledger.freeze(request, module, judge)
            # Every declared required view must include this actual paused endpoint.
            for role, _ in spec.views:
                if not any(
                    p.role == role
                    and p.source.serial == binding.source_serial
                    and p.source.phase == "paused"
                    for p in proof.picks
                ):
                    raise InterventionUnproven("fixed picks omit paused endpoint")
            return PausedEvidence(binding, proof, spec, tuple(images))
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven("paused evidence freeze failed") from exc
