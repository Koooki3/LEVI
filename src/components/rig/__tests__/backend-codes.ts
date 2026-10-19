// The stable codes the rig backend can answer with, read from its source
// (levi/rig/deploy.py, command.py, catalog_api.py, deploy_api.py, site.py), so
// a code the backend gains that the page cannot word fails a test here instead
// of showing an English sentence in a Chinese page.
import { readFileSync } from "fs";
import { join } from "path";

const root = join(import.meta.dir, "../../../..");
const source = (file: string) =>
  readFileSync(join(root, "levi/rig", file), "utf8");

/** The keys of a top-level `NAME = { "key": ..., }` table of deploy.py. */
function table(name: string): string[] {
  const text = source("deploy.py");
  const start = text.indexOf(`\n${name} = {`);
  if (start < 0) throw new Error(`deploy.py has no table ${name}`);
  const end = text.indexOf("\n}\n", start);
  return [...text.slice(start, end).matchAll(/^ {4}"([a-z_]+)": /gm)].map(
    (match) => match[1],
  );
}

export const REFUSALS = table("REFUSALS");
export const REASONS = table("REASONS");
export const FAILURES = table("FAILURES");
export const CHECKS = table("CHECK_OK");

/** The codes of the command form's refusals (command.py) and of the routes in
 * front of it (catalog_api.py). */
export const FORM_CODES = [
  ...new Set([
    ...[...source("command.py").matchAll(/FormError\(\s*"([a-z_]+)"/g)].map(
      (match) => match[1],
    ),
    ...[...source("command.py").matchAll(/"([a-z_]+_out_of_range)"/g)].map(
      (match) => match[1],
    ),
    ...[...source("catalog_api.py").matchAll(/"(command_self_check)"/g)].map(
      (match) => match[1],
    ),
  ]),
];

/** What the site file and the auth guard answer on every route. */
export const GUARD_CODES = [
  "site_missing",
  "site_invalid",
  "person_only",
  "ui_token",
];
