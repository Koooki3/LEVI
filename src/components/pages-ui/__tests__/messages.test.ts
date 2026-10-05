import { readFileSync } from "fs";
import { join } from "path";
import { describe, expect, test } from "bun:test";
import zh from "@/i18n/zh.json";
import { describeMessage, pickLanguage, serverSentence } from "../messages";

/** The page's `t` for exact catalogue keys (the locale's patterns are not
 * needed for what these tests check). */
const tzh = (text: string) =>
  (zh as Record<string, string>)[text.replace(/\s+/g, " ").trim()] ?? text;
const ten = (text: string) => text;
const CJK = /[㐀-鿿]/;

describe("a sentence the service wrote in both languages", () => {
  const both =
    "4 picked episode(s) have copies carrying another task text; the canonical copy's text is used. List them with `levi pool corrections copies`, and settle them with a reviewed correction / 4 个选中片段的副本带着不同的任务文本，现用规范副本的文本";
  test("shows one half, the one for the language", () => {
    expect(pickLanguage(both, "zh")).toStartWith("4 个选中片段");
    expect(pickLanguage(both, "zh")).not.toContain("canonical");
    expect(pickLanguage(both, "en")).toEndWith("reviewed correction");
    expect(pickLanguage(both, "en")).not.toMatch(CJK);
  });
  test("leaves ordinary text with a slash alone", () => {
    expect(pickLanguage("read / write access", "zh")).toBe(
      "read / write access",
    );
    expect(pickLanguage("训练 / 评测", "zh")).toBe("训练 / 评测");
  });
});

describe("known service sentences", () => {
  const cases: [string, RegExp][] = [
    [
      "LEVI backend unavailable. Start both services with uv run levi serve.",
      /没有回应/,
    ],
    ["Path must remain inside the configured workspace", /工作区/],
    [
      "Export directory /x/exports/a is outside LEVI_EXPORT_ROOTS (/x/exports, /y)",
      /导出目录 \/x\/exports\/a 不在/,
    ],
    [
      "Export directory /s/a lies inside the source dataset /s",
      /位于来源数据集 \/s 之内/,
    ],
    [
      "Export directory /s would contain the source dataset /s/a",
      /会把来源数据集 \/s\/a 包含在内/,
    ],
    ["Export directory already exists: /x/exports/a", /\/x\/exports\/a 已存在/],
    ["An unfinished export is in the way: /x/.a.partial", /未完成的导出/],
    ["Refusing to write inside pool source /s", /拒绝写入训练池来源 \/s/],
    ["The worker died from signal SIGKILL (exit -9)", /SIGKILL.*-9/],
    ["The worker exited with code 3 without a result", /退出码 3/],
    ["HTTP 500", /HTTP 500/],
    [
      "worker environment not found at /x/.venv/bin/python; run integrations/segmentation/setup.sh",
      /在 \/x\/\.venv\/bin\/python 没有找到学生模型运行环境/,
    ],
    ["worker source is missing from this checkout", /缺少学生模型工作进程/],
    [
      "SAM3 worker environment not found at /y/.venv/bin/python",
      /在 \/y\/\.venv\/bin\/python 没有找到 SAM3 运行环境/,
    ],
    ["SAM3 checkpoint is not downloaded", /SAM3 模型检查点还没有下载/],
    [
      "The SAM3 teacher is not ready: SAM3 checkpoint is not downloaded",
      /SAM3 教师模型未就绪：SAM3 模型检查点还没有下载/,
    ],
    [
      "Not a LeRobot dataset (meta/info.json) or a recognized raw capture",
      /不是 LeRobot 数据集.*不是可识别的原始采集/,
    ],
    [
      "2 approved task correction(s) disagree with another one for the same recording; reject one of them or apply fewer versions",
      /2 条已批准/,
    ],
    [
      "Approved task corrections not applied: 1 stale, 2 unmatched (stale: the episode's text is no longer the one corrected; unmatched: no such episode in the index; ambiguous: the path is found under more than one pool root)",
      /1 条已过期，2 条未匹配/,
    ],
  ];
  test.each(cases)("%s reads in Chinese, once", (raw, expected) => {
    const described = describeMessage(raw, tzh, "zh");
    expect(described.text).toMatch(expected);
    expect(described.details).toBeUndefined();
    expect(described.text).not.toMatch(/\b(outside|inside|worker|workspace)\b/);
  });
  test("an unknown reason inside the SAM3 teacher sentence keeps its own words", () => {
    const described = describeMessage(
      "The SAM3 teacher is not ready: CUDA out of memory at /x/y",
      tzh,
      "zh",
    );
    expect(described.text).toMatch(/SAM3 教师模型未就绪/);
    expect(described.text).toContain("CUDA out of memory at /x/y");
    expect(described.text).not.toMatch(/技术细节/);
  });
  test("a known sentence says what to do about it", () => {
    const outside = describeMessage(
      "Export directory /x/e is outside LEVI_EXPORT_ROOTS (/x)",
      tzh,
      "zh",
    );
    expect(outside.fix).toContain("LEVI_EXPORT_ROOTS");
    expect(outside.fix).toMatch(CJK);
    const down = describeMessage(
      "LEVI backend unavailable. Start both services with uv run levi serve.",
      tzh,
      "zh",
    );
    expect(down.fix).toContain("uv run levi serve");
  });
  test("English gets the plain wording, with the names and paths kept", () => {
    const described = describeMessage(
      "Export directory /x/e is outside LEVI_EXPORT_ROOTS (/x)",
      ten,
      "en",
    );
    expect(described.text).toContain("outside the folders LEVI may export to");
    expect(described.text).toContain("/x/e");
  });
});

describe("a sentence nobody translated", () => {
  test("Chinese shows a plain line and keeps the original as details", () => {
    const described = describeMessage("Something odd happened", tzh, "zh");
    expect(described.text).toMatch(CJK);
    expect(described.text).not.toContain("Something odd");
    expect(described.details).toBe("Something odd happened");
  });
  test("English shows the original, with no details", () => {
    expect(describeMessage("Error: busy", ten, "en")).toEqual({ text: "busy" });
  });
  test("a catalogue sentence is translated, not demoted", () => {
    const described = describeMessage(
      "Held-out entries that match no indexed episode",
      tzh,
      "zh",
    );
    expect(described.details).toBeUndefined();
    expect(described.text).toMatch(CJK);
  });
  test("an inline sentence falls back to the original", () => {
    expect(serverSentence("Something odd happened", tzh, "zh")).toBe(
      "Something odd happened",
    );
    expect(
      serverSentence(
        "Path must remain inside the configured workspace",
        tzh,
        "zh",
      ),
    ).toMatch(CJK);
  });
});

describe("pages.css keeps the fixes that tests cannot see in a DOM", () => {
  const css = readFileSync(
    join(import.meta.dir, "../pages.css"),
    "utf8",
  ).replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector: string) => {
    const found = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter((m) =>
      m[1]
        .split(",")
        .map((s) => s.trim())
        .includes(selector),
    );
    return found.map((m) => m[2]).join(";");
  };
  test("a composition row's task name may shrink, so the buttons stay inside the card", () => {
    expect(
      rule(
        ".pg-pool-order .pg-pool-row > .ds-tooltip-anchor:has(> .pg-pool-ellipsis)",
      ),
    ).toMatch(/min-width:\s*0/);
    expect(rule(".pg-pool-order .pg-pool-row .pg-pool-ellipsis")).toMatch(
      /overflow:\s*hidden/,
    );
    expect(rule(".pg-pool-order .pg-pool-row")).toMatch(/min-width:\s*0/);
  });
  test("Conversion & review, Training pool and Explore share one page width", () => {
    expect(rule(".pg-workbench")).toMatch(/--pg-max:\s*1360px/);
    expect(css).not.toMatch(/\.pg-pool\s*\{[^}]*--pg-max/);
    expect(css).not.toMatch(/\.pg-workbench\.pg-pool/);
  });
  test("a home job row shows the figure, not the bar's label a second time", () => {
    expect(rule(".pg-home-job__bar .ds-progress__label")).toMatch(
      /display:\s*none/,
    );
  });
});
