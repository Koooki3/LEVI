// Runs every frontend test file in its own `bun test` process.
//
// Why: `mock.module(...)` (next/navigation, next/link, ...) is global to a bun
// process and is not undone between files, so with one process the result
// depends on the order the files are discovered in. That order differs between
// file systems: the suite passed on the maintainer's machine and failed on the
// GitHub runners (pool-errors, workbench-load, guide). One process per file
// makes the outcome independent of the order and of the machine.
//
//   bun run scripts/test-isolated.ts [substring ...]   (same as `bun run test`)
//   LEVI_TEST_JOBS=4   files run at the same time (default: half the CPUs, 2..6)
//
// The last lines report wall time, the summed per-file time (what a serial run
// would cost), and the slowest files, so a slow suite can be told apart from a
// slow file without re-running anything. `--times` lists every file.
import { Glob } from "bun";
import { cpus } from "node:os";

const args = process.argv.slice(2);
const showAllTimes = args.includes("--times");
const filters = args.filter((a) => a !== "--times");
const files = [...new Glob("src/**/*.test.{ts,tsx}").scanSync(".")]
  .filter((f) => !filters.length || filters.some((x) => f.includes(x)))
  .sort();
if (!files.length) {
  console.error("no test files match");
  process.exit(1);
}
const jobs = Math.max(
  1,
  Number(process.env.LEVI_TEST_JOBS) ||
    Math.min(6, Math.max(2, Math.floor(cpus().length / 2))),
);

type Result = { file: string; code: number; out: string; ms: number };
// bun colours its summary when FORCE_COLOR is set (agent shells do), which used
// to turn every count into 0; strip the escapes before matching.
const stripAnsi = (text: string) => text.replace(/\u001b\[[0-9;]*m/g, "");
const count = (text: string, word: string) =>
  Number(
    stripAnsi(text).match(new RegExp(`^\\s*(\\d+) ${word}$`, "m"))?.[1] ?? 0,
  );

async function runOne(file: string): Promise<Result> {
  const t0 = Date.now();
  const proc = Bun.spawn([process.execPath, "test", `./${file}`], {
    stdout: "pipe",
    stderr: "pipe",
    env: process.env,
  });
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ]);
  const code = await proc.exited;
  return { file, code, out: out + err, ms: Date.now() - t0 };
}

const results: Result[] = [];
const started = Date.now();
let next = 0;
await Promise.all(
  Array.from({ length: Math.min(jobs, files.length) }, async () => {
    while (next < files.length) {
      const file = files[next++];
      const r = await runOne(file);
      results.push(r);
      if (r.code) {
        console.log(`\n=== FAILED ${file} (${(r.ms / 1000).toFixed(1)} s) ===`);
        console.log(r.out.trimEnd());
      }
    }
  }),
);

const pass = results.reduce((n, r) => n + count(r.out, "pass"), 0);
const fail = results.reduce((n, r) => n + count(r.out, "fail"), 0);
const bad = results.filter((r) => r.code);
const wall = Date.now() - started;
const summed = results.reduce((n, r) => n + r.ms, 0);
const byTime = [...results].sort((a, b) => b.ms - a.ms);
console.log(
  `\ntime: ${(wall / 1000).toFixed(1)} s wall, ${(summed / 1000).toFixed(1)} s summed over files` +
    (showAllTimes ? "" : `; slowest ${Math.min(5, byTime.length)}:`),
);
for (const r of showAllTimes ? byTime : byTime.slice(0, 5)) {
  console.log(`  ${(r.ms / 1000).toFixed(1).padStart(6)} s  ${r.file}`);
}
console.log(
  `\n${pass} pass, ${fail} fail across ${files.length} files, ${jobs} at a time` +
    (bad.length ? `; failing files: ${bad.map((r) => r.file).join(", ")}` : ""),
);
// A run where every file passed but nothing was counted means bun's summary
// changed format (colour once turned every count into 0): say so and fail
// instead of reporting "0 pass" as success.
if (!bad.length && pass === 0) {
  console.error(
    "no test results were counted although every file exited 0: bun's summary format changed (see count() above)",
  );
  process.exit(1);
}
process.exit(bad.length ? 1 : 0);
