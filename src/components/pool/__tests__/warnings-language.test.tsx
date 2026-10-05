import { render, setupDom } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { PoolWarnings } from "../composition-panel";
import { warningText, type PoolWarning } from "../types";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const CJK = /[㐀-鿿]/;
const tzh = (text: string) =>
  (zh as Record<string, string>)[text.replace(/\s+/g, " ").trim()] ?? text;

// What levi/pool/corrections.py sends: both languages in one sentence, and
// ``episodes`` as the list of the episodes' folders, not a count.
const conflict: PoolWarning = {
  code: "copy_task_conflict",
  blocking: false,
  count: 4,
  episodes: ["/pool/screws_alt/demo_0000", "/pool/screws_alt/demo_0001"],
  message:
    "4 picked episode(s) have copies carrying another task text; the canonical copy's text is used. List them with `levi pool corrections copies`, and settle them with a reviewed correction / 4 个选中片段的副本带着不同的任务文本，现用规范副本的文本；用 `levi pool corrections copies` 列出，经人审核的订正可以定下文本",
};

describe("pool warnings read in one language", () => {
  test("Chinese shows the Chinese half only", () => {
    const text = warningText(conflict, tzh, "zh");
    expect(text).toMatch(CJK);
    expect(text).not.toContain("canonical");
    expect(text).not.toContain(" / ");
  });
  test("English shows the English half only", () => {
    const text = warningText(conflict, (s) => s, "en");
    expect(text).toContain("canonical copy");
    expect(text).not.toMatch(CJK);
  });
  test("the list of episode folders is not printed as a number", async () => {
    localStorage.setItem("levi-language", "zh");
    const { host } = await render(
      <LocaleProvider>
        <PoolWarnings warnings={[conflict]} />
      </LocaleProvider>,
    );
    expect(host.textContent).not.toContain("/pool/screws_alt");
    expect(host.textContent).not.toContain("canonical");
    expect(host.textContent).toContain("提示");
  });
  test("the list is a list: the live region is its wrapper, not the <ul>", async () => {
    const { host } = await render(<PoolWarnings warnings={[conflict]} />);
    expect(host.querySelector("ul")!.getAttribute("role")).toBeNull();
    expect(host.querySelector("div[role=status] > ul > li")).not.toBeNull();
  });
  test("a worker message with numbers is translated, not left in English", () => {
    const text = warningText(
      {
        code: "task_corrections_not_applied",
        blocking: false,
        message:
          "Approved task corrections not applied: 2 stale (stale: the episode's text is no longer the one corrected; unmatched: no such episode in the index; ambiguous: the path is found under more than one pool root)",
      },
      tzh,
      "zh",
    );
    expect(text).toContain("2 条已过期");
    expect(text).not.toContain("Approved");
  });
});
