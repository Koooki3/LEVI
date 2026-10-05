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
