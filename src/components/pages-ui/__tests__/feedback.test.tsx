import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync, statSync } from "fs";
import { join } from "path";
import { EmptyLine, JobCard, Note, Problem, RequestProblem } from "../feedback";

setupDom();

describe("Problem: what happened, why, what to do", () => {
  test("three parts, announced, raw details collapsed", async () => {
    const { host } = await render(
      <Problem
        title="The export did not start"
        why="Export directory is outside LEVI_EXPORT_ROOTS"
        fix={<button type="button">Try again</button>}
        details="403 Forbidden"
      />,
    );
    const box = host.querySelector(".pg-problem")!;
    expect(box.getAttribute("role")).toBe("alert");
    expect(box.querySelector(".pg-problem__title")!.textContent).toBe(
      "The export did not start",
    );
    expect(box.querySelector(".pg-problem__why")!.textContent).toContain(
      "outside LEVI_EXPORT_ROOTS",
    );
    expect(box.querySelector(".pg-problem__fix button")).not.toBeNull();
    const details = box.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("403 Forbidden");
    // The status shape is drawn (an icon), not only a colour.
    expect(box.querySelector("svg.ds-icon")).not.toBeNull();
  });

  test("a standing error is not re-announced; warnings get their tone", async () => {
    const { host } = await render(
      <Problem live={false} tone="warning" title="Blocked" />,
    );
    const box = host.querySelector(".pg-problem")!;
    expect(box.hasAttribute("role")).toBe(false);
    expect(box.className).toContain("pg-problem--warning");
  });

  test("RequestProblem shows the server's words as the reason and a next step", async () => {
    const { host } = await render(
      <RequestProblem action="The scan did not start" message="busy" />,
    );
    expect(host.textContent).toContain("The scan did not start");
    expect(host.textContent).toContain("busy");
    expect(host.textContent).toContain("try again");
  });
});

describe("Note, JobCard, EmptyLine", () => {
  test("render their content with an icon", async () => {
    const { host } = await render(
      <>
        <Note tone="success" role="status">
          Dry run done
        </Note>
        <JobCard label="Export" status={<b>done</b>} title="ui" meta="1 s">
          <p>bar</p>
        </JobCard>
        <EmptyLine>No jobs yet</EmptyLine>
      </>,
    );
    expect(host.querySelector(".pg-note--success")!.getAttribute("role")).toBe(
      "status",
    );
    const card = host.querySelector("section.pg-jobcard")!;
    expect(card.getAttribute("aria-label")).toBe("Export");
    expect(card.querySelector(".pg-jobcard__meta")!.textContent).toBe("1 s");
    expect(host.querySelector(".pg-empty-inline")!.textContent).toBe(
      "No jobs yet",
    );
    expect(host.querySelectorAll("svg.ds-icon").length).toBe(2);
  });
});

describe("stage-4 stylesheets", () => {
  const dir = join(import.meta.dir, "..");
  test.each(["pages.css", "agent-content.css"])(
    "%s uses tokens, never colour literals",
    (file) => {
      const css = readFileSync(join(dir, file), "utf8").replace(
        /\/\*[\s\S]*?\*\//g,
        "",
      );
      expect(css.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g)).toBeNull();
    },
  );
  test("no infinite animation except the shared breathing dot", () => {
    for (const file of ["pages.css", "agent-content.css"]) {
      const css = readFileSync(join(dir, file), "utf8");
      for (const line of css.split("\n").filter((l) => /infinite/.test(l)))
        expect(line).toContain("ds-breathe");
    }
  });
});

describe("reduced motion inside the app (data-motion)", () => {
  const dir = join(import.meta.dir, "..");
  const strip = (file: string) =>
    readFileSync(join(dir, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  test.each(["pages.css", "agent-content.css"])(
    "%s: every animated or transitioned rule is also stopped under data-motion",
    (file) => {
      const css = strip(file);
      const reduced = new Set<string>();
      for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        if (!m[1].includes('[data-motion="reduce"]')) continue;
        if (!/animation:\s*none/.test(m[2]) || !/transition:\s*none/.test(m[2]))
          continue;
        for (const sel of m[1].split(","))
          reduced.add(sel.replace('[data-motion="reduce"]', "").trim());
      }
      const moving: string[] = [];
      for (const m of css.matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
        const body = m[2];
        const moves =
          /animation:\s*(?!\s|none)/.test(body) ||
          /transition:\s*(?!\s|none)/.test(body);
        if (!moves || m[1].includes("data-motion")) continue;
        for (const sel of m[1].split(",")) moving.push(sel.trim());
      }
      const missing = moving.filter(
        (sel) =>
          ![...reduced].some(
            (r) => sel === r || sel.startsWith(r) || r.startsWith(sel),
          ),
      );
      expect(missing).toEqual([]);
    },
  );
});

describe("no Tailwind spacing or text utilities on the stage-4 pages", () => {
  test("they lose against ds-root's reset; pg-* helpers are used", () => {
    const root = join(import.meta.dir, "../../..");
    const dirs = [
      "app/live",
      "app/workbench",
      "app/pool",
      "app/explore",
      "components/live",
      "components/conversion",
      "components/pool",
    ];
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (name === "__tests__") continue;
        if (statSync(path).isDirectory()) walk(path);
        else if (path.endsWith(".tsx")) files.push(path);
      }
    };
    for (const dir of dirs) walk(join(root, dir));
    const bad: string[] = [];
    for (const file of files) {
      const code = readFileSync(file, "utf8");
      for (const m of code.matchAll(/className="([^"]*)"/g))
        for (const token of m[1].split(" "))
          if (
            /^(m[trblxy]?|p[trblxy]?)-\d|^text-(xs|sm|base|lg)$|^w-(full|\d+)$/.test(
              token,
            )
          )
            bad.push(`${file}: ${token}`);
    }
    expect(bad).toEqual([]);
  });
});
