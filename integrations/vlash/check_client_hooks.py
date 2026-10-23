"""Exercise approved client extension functions without importing hardware modules."""

import argparse
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


def extract(path, name, namespace):
    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            fn,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # noqa: S102 - approved local function under isolated fake dependencies
    return namespace[name]


def suite(root):
    client = root / "examples/fr3_local/run_robotiq_client.py"
    collect = root / "examples/fr3_local/run_robotiq_client_collect_data.py"

    class Hooks(unittest.TestCase):
        def test_factory_keeps_original_loop_and_independent_diagnostics(self):
            scheduler = SimpleNamespace(
                mode="vlash", stats={"requests": 2}, summary=lambda: {"prefetches": 1}
            )
            closed = []
            scheduler.close = lambda: closed.append(True)
            seen = []

            def loop(*args, **kwargs):
                seen.append(kwargs)
                return "timeout", {"steps": 3, "inferences": 0}

            fn = extract(client, "_run_episode", {"_episode_loop": loop})
            outcome, stats = fn(
                None,
                None,
                SimpleNamespace(rtc=False),
                "task",
                None,
                None,
                scheduler_factory=lambda *args: scheduler,
            )
            self.assertEqual(outcome, "timeout")
            self.assertIs(seen[0]["rtc"], scheduler)
            self.assertEqual(closed, [True])
            self.assertEqual(stats["inferences"], 2)
            self.assertIn("vlash", stats)
            self.assertNotIn("rtc", stats)

        def test_factory_is_closed_when_original_safety_loop_fails(self):
            closed = []
            scheduler = SimpleNamespace(mode="vlash", close=lambda: closed.append(True))

            def fail(*args, **kwargs):
                raise RuntimeError("mock guard fault")

            fn = extract(client, "_run_episode", {"_episode_loop": fail})
            with self.assertRaisesRegex(RuntimeError, "mock guard fault"):
                fn(
                    None,
                    None,
                    SimpleNamespace(rtc=False),
                    "task",
                    None,
                    None,
                    scheduler_factory=lambda *args: scheduler,
                )
            self.assertEqual(closed, [True])

        def test_rtc_and_external_factory_are_refused_before_use(self):
            fn = extract(client, "_run_episode", {})
            with self.assertRaises(ValueError):
                fn(
                    None,
                    None,
                    SimpleNamespace(rtc=True),
                    "task",
                    None,
                    None,
                    scheduler_factory=lambda *args: self.fail("must not construct"),
                )

        def test_vlash_without_explicit_hooks_fails_before_connect(self):
            fn = extract(collect, "main", {})
            with self.assertRaises(ValueError):
                fn(SimpleNamespace(inference_mode="vlash", rtc=False))

    return unittest.defaultTestLoader.loadTestsFromTestCase(Hooks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openpi-root", required=True, type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(suite(args.openpi_root))
    raise SystemExit(not result.wasSuccessful())
