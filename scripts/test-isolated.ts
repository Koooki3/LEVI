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
import { Glob } from "bun";
import { cpus } from "node:os";

const filters = process.argv.slice(2);
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
const count = (text: string, word: string) =>
  Number(text.match(new RegExp(`^\\s*(\\d+) ${word}$`, "m"))?.[1] ?? 0);

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
console.log(
  `\n${pass} pass, ${fail} fail across ${files.length} files, ${jobs} at a time` +
    (bad.length ? `; failing files: ${bad.map((r) => r.file).join(", ")}` : ""),
);
process.exit(bad.length ? 1 : 0);
