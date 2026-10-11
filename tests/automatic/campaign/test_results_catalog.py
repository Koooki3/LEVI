import test_ledger_fixtures as fx

from levi.automatic.campaign import ledger as L
from levi.automatic.campaign import report as R
from levi.evaluation_results import register


def test_real_campaign_writer_contract(tmp_path):
    layout = fx.layout(per_arm=2, segment_trials=1)
    ledger = L.derive(layout, fx.synthetic_facts(layout))
    info = R.CampaignInfo(
        campaign_id=fx.CAMPAIGN,
        arms=(R.ArmInfo("A"), R.ArmInfo("B")),
        task="synthetic task",
        seed=7,
        trials_per_arm=2,
        comparison=("B", "A"),
    )
    report_root = tmp_path / "report"
    R.write_report(
        report_root,
        ledger,
        info,
        "operator_label",
        campaign_state="REPORTED",
        layout=layout,
        now=0,
        pdf=False,
        resamples=20,
    )
    rollout_root = tmp_path / "rollouts/models"
    rollout_dir = rollout_root / "model-a/task-a"
    rollout_dir.mkdir(parents=True)
    row = register(
        tmp_path / "catalog",
        report=report_root / "operator_label",
        rollout_root=rollout_root,
        rollout_dir=rollout_dir,
        model="model-a",
        task="task-a",
        session="eval-001",
    )
    assert not row["progress_only"]
    assert "data/analysis.json" in row["report"]["files"]
    assert any(p.endswith(".svg") for p in row["report"]["files"])
