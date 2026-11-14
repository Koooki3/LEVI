import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { describe, expect, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";
import ReportView from "../report-view";

setupDom();

function withService(answer: () => Response) {
  const original = globalThis.fetch;
  const asked: string[] = [];
  globalThis.fetch = ((url: string) => {
    asked.push(String(url));
    return Promise.resolve(answer());
  }) as unknown as typeof fetch;
  return { asked, restore: () => void (globalThis.fetch = original) };
}

const down = () =>
  new Response(JSON.stringify({ detail: "HTTP 502" }), { status: 502 });

async function open(language: "en" | "zh") {
  window.localStorage.setItem("levi-language", language);
  const view = await render(
    <LocaleProvider>
      <ReportView />
    </LocaleProvider>,
  );
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
  await flush();
  return view;
}

describe("the report page when the service does not answer", () => {
  test("English: a three-part error with the answer under the details and a retry", async () => {
    const service = withService(down);
    try {
      const { host } = await open("en");
      const problem = host.querySelector(".pg-problem")!;
      expect(problem.getAttribute("role")).toBe("alert");
      expect(problem.textContent).toContain("The report is unavailable");
      expect(problem.textContent).toContain(
        "The LEVI service did not answer, or answered with an error.",
      );
      expect(problem.textContent).toContain("then reload the page");
      expect(problem.querySelector("details pre")?.textContent).toBe(
        "HTTP 502",
      );
      const before = service.asked.length;
      const retry = [...problem.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("Try again"),
      );
      await click(retry!);
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      expect(service.asked.length).toBeGreaterThan(before);
    } finally {
      service.restore();
      window.localStorage.removeItem("levi-language");
    }
  });

  test("Chinese: the sentence and the advice are Chinese, the raw answer is folded away", async () => {
    const service = withService(down);
    try {
      const { host } = await open("zh");
      const problem = host.querySelector(".pg-problem")!;
      expect(problem.textContent).toContain("报告");
      expect(problem.textContent).toContain("LEVI 服务没有回应");
      expect(problem.textContent).toContain("重试");
      // The only English left is inside the folded technical details.
      const visible = problem.cloneNode(true) as HTMLElement;
      visible.querySelector("details")?.remove();
      expect(visible.textContent).not.toMatch(
        /did not answer|Try again|unavailable/,
      );
    } finally {
      service.restore();
      window.localStorage.removeItem("levi-language");
    }
  });
});

describe("the report page's headings", () => {
  const payload = (markdown: string | null) => ({
    configured: true,
    dir: "/r",
    exists: true,
    lang: "en",
    document_lang: "en",
    markdown,
    status: null,
    errors: [],
    mtime: 1,
    etag: "e1",
  });
  const h1s = (host: HTMLElement) => [...host.querySelectorAll("h1")];

  test("one h1: the document's first; a later # is an h2 with its anchor, and the eyebrow is no heading", async () => {
    const service = withService(
      () =>
        new Response(
          JSON.stringify(
            payload("# First title\n\ntext\n\n# Second\n\n## Part"),
          ),
        ),
    );
    try {
      const { host } = await open("en");
      expect(h1s(host).map((h) => h.textContent)).toEqual(["First title"]);
      const second = [...host.querySelectorAll("h2")].find(
        (h) => h.textContent === "Second",
      );
      expect(second?.id).toBe("second");
      const eyebrow = host.querySelector(".lr-brand .ds-eyebrow")!;
      expect(eyebrow.closest("h1, h2, h3, [role=heading]")).toBeNull();
    } finally {
      service.restore();
      window.localStorage.removeItem("levi-language");
    }
  });

  test("a document without a # title, and the error state, still have exactly one (hidden) h1", async () => {
    const noTitle = withService(
      () => new Response(JSON.stringify(payload("## Only a section\n\ntext"))),
    );
    try {
      const { host } = await open("en");
      expect(h1s(host)).toHaveLength(1);
      expect(h1s(host)[0].className).toContain("ds-sr-only");
    } finally {
      noTitle.restore();
    }
    const down502 = withService(down);
    try {
      const { host } = await open("en");
      expect(h1s(host)).toHaveLength(1);
    } finally {
      down502.restore();
      window.localStorage.removeItem("levi-language");
    }
  });
});
