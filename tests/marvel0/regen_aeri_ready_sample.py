"""Regenerates the samples of the AERI preparation card that the frontend tests
parse with the page's own readers, so the page and the server agree on one shape:

* ``src/components/marvel0/__tests__/marvel0-aeri.samples.json``: the exact
  ``levi.marvel0.aeri.v1`` documents of ``GET /aeri`` in every state the card
  shows, the answers of the three writes and every refusal (``{status, detail}``)
  the page words, and the closed lists of codes it must have text for;
* ``tests/marvel0/fixtures/aeri_ready.sample.json``: the same text (the
  design's name for it).

    uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py

The documents are what ``aeri_ready.document`` builds on the made-up machine of
``aeri_world.py`` (a temporary folder, a real git repository, stand-in programs,
a fixed clock; no unit, port or process of the real machine is looked at). Paths
of the temporary folder become ``/path/to``, digests that depend on it become
fixed values and the commit becomes ``5bc750ca``, so the file does not depend on
where it is built. A refusal the page can only get from a state that is not worth
building here (the run lock held, a state folder that cannot be used) is made
from ``Refuse`` itself with plausible facts and is listed in ``constructed``.
``tests/marvel0/test_marvel0_aeri_ready_sample.py`` fails, naming this command,
when a committed file differs from what this builds."""

import importlib
import json
import re
import tempfile

from aeri_world import World
from regen_deploy_sample import _lines
from regen_sample import FOLDER, ROOT, fixed_zone

from levi.automatic import fr3_policy_swap
from levi.marvel0 import aeri_prep, aeri_ready, status
from levi.marvel0 import site as marvel0_site
from levi.marvel0.aeri_prep import Refuse

SAMPLE = FOLDER / "marvel0-aeri.samples.json"
FIXTURE = ROOT / "tests" / "marvel0" / "fixtures" / "aeri_ready.sample.json"
PROFILES = {"harvest-5bc750ca": True, "bfdee2cf": False, "main-297a74c3": True}
HEAD = "5bc750cacc12e375d77f5036b4976d560ac682a3"
LATER = "9d3e54ea0000000000000000000000000000beef"
UNITS = (
    "levi-aeri-vlm-8100.service loaded active running AERI vision model\n"
    "levi-aeri-policy-forward.service loaded active running AERI policy server\n"
    "levi-aeri-policy-manual-eggplant.scope loaded active running policy server by hand\n"
)
KEEP = (
    "631bb24acb0f0ff263b2067d619459fc115d4d188a906d7e5c9f7a25ddab0537",
    "b088f3c0c7ab7c4b9bb14fb7050097628c9307b23d3c03a8306e014d3d74c8c4",
)
# What the page words for a reason a stop is not offered (``why_not.code``).
WHY_NOT = (
    "aeri_disabled",
    "arm_in_use",
    "d_active",
    "e_active",
    "gpu_in_use",
    "not_product",
    "not_running",
    "nothing_to_stop",
    "unreadable",
)
REGENERATE = "uv run --no-sync python tests/marvel0/regen_aeri_ready_sample.py"


def failure(call) -> dict:
    """``{status, detail}`` of the refusal ``call`` raises."""
    try:
        call()
    except Refuse as exc:
        error = aeri_prep.http(exc)
        return {"status": error.status_code, "detail": error.detail}
    raise AssertionError("the call was not refused")


def constructed(code, **extra) -> dict:
    error = aeri_prep.http(Refuse(code, **extra))
    return {"status": error.status_code, "detail": error.detail}


def install_profiles():
    """``aeri_state.PROFILES`` for the build: the three profiles of the design,
    whatever the module has (its other parts stay)."""
    module = importlib.import_module("levi.marvel0.aeri_state")
    before = module.PROFILES
    module.PROFILES = {k: {"start_home": v} for k, v in PROFILES.items()}
    return module, before


def restore_profiles(saved):
    module, before = saved
    module.PROFILES = before


def build_data(tmp):
    """``(data, root, heads)``: the documents and answers, the made-up machine's
    folder and the two commits its worktree had (before and after a change)."""
    world = World(tmp)
    heads = [world.head()]
    world.set_live("stopped")
    preparer = world.preparer()

    def doc():
        preparer.readiness.forget()
        return aeri_ready.document(preparer.readiness.facts(fresh=True))

    docs = {}
    # 1. nothing configured: no control file, no [aeri] in the site file
    world.facts["control"]["enabled"] = None
    good_site = world.site
    world.site = marvel0_site.parse(
        {k: v for k, v in world.site_dict().items() if k != "aeri"}, "env"
    )
    docs["unconfigured"] = doc()
    # 2. a section that is not valid
    world.facts["control"]["enabled"] = True
    world.site = marvel0_site.parse(
        world.site_dict(pin_sha="zz", levi_bin="relative"), "env"
    )
    docs["invalid_section"] = doc()
    # 3. configured, switched off
    world.site = good_site
    world.facts["control"]["enabled"] = False
    docs["disabled"] = doc()
    world.facts["control"]["enabled"] = True
    # 4. a busy machine: the live service holds 8100 and the GPU lock, the GPU
    # is half full, the drift check was never run, the stack is not read
    world.set_live("running")
    world.listen(8100)
    world.facts["gpu"] = (26500, 32607)
    world.facts["flock"] = {".gpu.lock": [4242]}
    world.facts["stack"] = World.stack_doc(a="unknown", c="unknown", health="unknown")
    world.facts["matches"] = (False, "differs:levi/automatic/console.py")
    docs["blocked"] = doc()
    # 5. the live service stopped, a run prepared, everything green
    ok_prepare = None

    def quiet():
        world.set_live("stopped")
        world.listen()
        world.facts["gpu"] = (8200, 32607)
        world.facts["flock"] = {}
        world.facts["stack"] = World.stack_doc()
        world.facts["matches"] = (True, "ok")

    quiet()
    docs["not_prepared"] = doc()
    body = world.prepare_body(request_id="req-20261021-0001")
    ok_prepare = preparer.prepare(body)
    docs["prepared_ready"] = doc()
    # 6. the prepared file changed afterwards
    job = world.runs / ok_prepare["name"] / "job.yaml"
    original = job.read_text()
    job.write_text(original + "# edited by hand\n")
    docs["prepared_changed"] = doc()
    job.write_text(original)
    # 7. what an earlier run left behind
    world.facts["units"] = status.parse_units(UNITS)
    swap = fr3_policy_swap.SwapLog.create(
        fr3_policy_swap.log_path(world.rollout, "aeri-20261021-ext-r00"),
        run_id="aeri-20261021-ext-r00",
        units={"forward": "levi-aeri-policy-forward"},
    )
    swap.append({"record": "prepared", "txn": "t1", "service_state": "forward"})
    world.facts["gpu"] = (21400, 32607)
    docs["leftovers_present"] = doc()
    world.facts["flock"] = {".gpu.lock": [4242]}
    docs["leftovers_blocked"] = doc()
    world.facts["flock"] = {}

    # the three writes, on a machine where they succeed
    world.set_live("running")
    live = preparer.live_stop(
        {"request_id": "req-20261021-0002", "confirm": "stop live service"}
    )
    left = preparer.leftovers_stop(
        {"request_id": "req-20261021-0003", "confirm": "stop leftover aeri units"}
    )

    errors_prepare, errors_live, errors_left = {}, {}, {}
    world.set_live("stopped")
    world.facts["units"] = []
    quiet()

    def prep(**change):
        return lambda: preparer.prepare(world.prepare_body(**change))

    world.facts["control"]["enabled"] = False
    errors_prepare["aeri_disabled"] = failure(prep(request_id="req-err-disabled-1"))
    world.facts["control"]["enabled"] = True
    world.facts["product"] = False
    errors_prepare["not_product"] = failure(prep(request_id="req-err-product-1"))
    world.facts["product"] = True
    world.site = marvel0_site.parse(world.site_dict(pin_sha="zz"), "env")
    errors_prepare["aeri_unconfigured"] = failure(prep(request_id="req-err-config-1"))
    world.site = good_site
    errors_prepare["confirm_required"] = failure(
        lambda: preparer.prepare(
            {**world.prepare_body(request_id="req-err-phrase-1"), "confirm": "go"}
        )
    )
    errors_prepare["request_id_invalid"] = failure(prep(request_id="bad"))
    errors_prepare["form_invalid"] = failure(
        prep(request_id="req-err-form-001", task="Pick The Pepper")
    )
    errors_prepare["request_id_used"] = failure(
        lambda: preparer.prepare(
            world.prepare_body(request_id="req-20261021-0001", rounds=6)
        )
    )
    world.reparse_site(profile="bfdee2cf")
    errors_prepare["start_home_unsupported"] = failure(
        prep(request_id="req-err-home-001")
    )
    world.site = good_site
    template = world.templates / "job.base.yaml"
    keep = template.read_text()
    template.write_text(keep + "\n")
    errors_prepare["templates_changed"] = failure(prep(request_id="req-err-templ-01"))
    template.write_text(keep)
    world.set_check(mode="fail")
    errors_prepare["check_failed"] = failure(prep(request_id="req-err-check-01"))
    world.set_check()
    world.commit_change()
    heads.append(world.head())
    errors_prepare["code_pin_mismatch"] = failure(prep(request_id="req-err-pin-0001"))
    world.facts["control"]["enabled"] = True
    world.reparse_site(pin_sha=world.head()[:8])
    errors_prepare["profile_unknown"] = constructed(
        "profile_unknown", profile="main-297a74c3"
    )
    errors_prepare["run_name_taken"] = constructed(
        "run_name_taken", name="aeri-20261021-ext-r01", where=["rollout"]
    )
    for code in ("busy", "lock_unusable", "state_dir_unusable"):
        errors_prepare[code] = constructed(code)

    world.set_live("running")
    world.facts["hosted_d"] = True
    errors_live["d_active"] = failure(
        lambda: preparer.live_stop(
            {"request_id": "req-err-live-001", "confirm": "stop live service"}
        )
    )
    world.facts["hosted_d"] = False
    world.facts["hosted_e"] = True
    errors_live["e_active"] = failure(
        lambda: preparer.live_stop(
            {"request_id": "req-err-live-002", "confirm": "stop live service"}
        )
    )
    world.facts["hosted_e"] = False
    world.facts["flock"] = {"levi-aeri-fr3-arm.lock": [4]}
    errors_live["arm_in_use"] = failure(
        lambda: preparer.live_stop(
            {"request_id": "req-err-live-003", "confirm": "stop live service"}
        )
    )
    world.facts["flock"] = {"levi-aeri-fr3-arm.lock": None}
    errors_live["state_unreadable"] = failure(
        lambda: preparer.live_stop(
            {"request_id": "req-err-live-004", "confirm": "stop live service"}
        )
    )
    world.facts["flock"] = {}
    world.set_levi(stops=False, rc=1, say="refused: a job is running")
    errors_live["live_stop_failed"] = failure(
        lambda: preparer.live_stop(
            {"request_id": "req-err-live-005", "confirm": "stop live service"}
        )
    )
    world.set_levi()
    world.set_live("stopped")
    errors_left["gpu_in_use"] = constructed("gpu_in_use")
    errors_left["stop_failed"] = constructed(
        "stop_failed",
        results=[
            {
                "name": "levi-aeri-vlm-8100.service",
                "ok": False,
                "detail": "Failed to stop levi-aeri-vlm-8100.service: Unit is masked.",
            },
            {"name": "levi-aeri-policy-forward.service", "ok": True, "detail": None},
        ],
    )

    answers = {
        "prepare": {
            "ok": {"status": 200, "body": ok_prepare},
            "errors": errors_prepare,
        },
        "live_stop": {"ok": {"status": 200, "body": live}, "errors": errors_live},
        "leftovers_stop": {"ok": {"status": 200, "body": left}, "errors": errors_left},
    }
    return {"docs": docs, "answers": answers}, world.root, heads


def normalise(value, root, heads):
    """The same value with the temporary folder, the commits and the digests that
    depend on them replaced by fixed ones."""
    text = json.dumps(value).replace(str(root), "/path/to")
    for head, fixed in zip(heads, (HEAD, LATER), strict=False):
        text = text.replace(head, fixed).replace(head[:8], fixed[:8])
    seen = {}

    def digest(hit):
        old = hit.group(0)
        if old in KEEP:
            return old
        if old not in seen:
            n = len(seen) + 1
            seen[old] = f"{n:02x}" * 32
        return seen[old]

    text = re.sub(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", digest, text)
    return json.loads(text)


def build() -> str:
    saved = install_profiles()
    try:
        with tempfile.TemporaryDirectory(dir=_tmp_parent()) as tmp, fixed_zone():
            data, root, heads = build_data(tmp)
            data = normalise(data, root, heads)
    finally:
        restore_profiles(saved)
    docs, answers = data["docs"], data["answers"]
    out = {
        "schema": "levi.marvel0.aeri_sample.v1",
        "note": (
            "Built by tests/marvel0/regen_aeri_ready_sample.py from the real code on a "
            "made-up machine; paths, the commit and the digests that depend on them "
            "are fixed values. Texts are English; the page maps code to its own text "
            "and falls back to text."
        ),
        "codes": {
            "readiness": list(aeri_ready.ROW_CODES),
            "levels": ["ok", "warn", "refuse", "unknown"],
            "errors": sorted(aeri_prep.REFUSALS),
            "why_not": list(WHY_NOT),
            "notices": sorted(aeri_ready.NOTICES),
            "control": [
                "aeri.enabled",
                "aeri.disabled",
                "aeri.missing",
                "aeri.invalid",
            ],
            "site": [
                "aeri_site.ok",
                "aeri_site.no_section",
                "aeri_site.missing_file",
                "aeri_site.invalid",
            ],
            "problems": [
                "run_json_missing",
                "files_changed",
                "code_changed",
                "profile_changed",
                "digest_missing",
            ],
            "live_states": ["running", "stopped", "absent", "unknown"],
            "leftover_states": ["none", "present", "unknown"],
            "swap_states": ["clean", "unclean", "unknown"],
            "unit_kinds": ["vlm", "policy"],
            "modes": ["external", "local"],
        },
        "constructed": [
            "busy",
            "gpu_in_use",
            "lock_unusable",
            "profile_unknown",
            "run_name_taken",
            "state_dir_unusable",
            "stop_failed",
        ],
        "get": docs,
        "prepare": answers["prepare"],
        "live_stop": answers["live_stop"],
        "leftovers_stop": answers["leftovers_stop"],
    }
    return "\n".join(_lines(out, 0, "", "")) + "\n"


def _tmp_parent():
    parent = ROOT / ".state" / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def main():
    text = build()
    SAMPLE.write_text(text)
    FIXTURE.write_text(text)
    print(f"wrote {SAMPLE.relative_to(ROOT)} and {FIXTURE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
