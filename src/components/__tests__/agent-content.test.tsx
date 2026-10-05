import { click, fire, flush, render, setupDom } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { Actions, Disclosure, GatedButton } from "../agent-ui";
import AgentPlan, { type HarnessPlan } from "../agent-plan";
import AgentReviewQueue from "../agent-review-queue";
import ChipMultiSelect from "../chip-multi-select";
import OllamaRuntime from "../ollama-runtime";
import AgentActivity from "../agent-activity";

setupDom();

describe("GatedButton: a disabled button says why", () => {
  test("with a reason it is disabled and described by the reason", async () => {
    const { host } = await render(
      <GatedButton reason="Write a note first.">Accept</GatedButton>,
    );
    const button = host.querySelector("button")!;
    expect(button.disabled).toBe(true);
    const why = host.querySelector(
      `#${CSS.escape(button.getAttribute("aria-describedby")!)}`,
    )!;
    expect(why.textContent).toBe("Write a note first.");
    expect(why.className).toContain("ag-why");
  });

  test("without a reason it is a plain enabled Button", async () => {
    const { host } = await render(<GatedButton>Accept</GatedButton>);
    const button = host.querySelector("button")!;
    expect(button.disabled).toBe(false);
    expect(button.className).toContain("ds-btn");
    expect(button.getAttribute("aria-describedby")).toBeNull();
    expect(host.querySelector(".ag-why")).toBeNull();
  });
});

describe("Disclosure", () => {
  test("a native details with a chevron; open and defaultOpen both open it", async () => {
    const { host } = await render(
      <>
        <Disclosure summary="One" defaultOpen>
          body
        </Disclosure>
        <Disclosure summary="Two">body</Disclosure>
      </>,
    );
    const [one, two] = Array.from(host.querySelectorAll("details"));
    expect(one.open).toBe(true);
    expect(two.open).toBe(false);
    expect(one.querySelector("summary svg")).not.toBeNull();
    expect(one.querySelector("summary")!.textContent).toBe("One");
  });

  test("Actions wraps its buttons in one row", async () => {
    const { host } = await render(
      <Actions>
        <GatedButton>A</GatedButton>
        <GatedButton>B</GatedButton>
      </Actions>,
    );
    expect(host.querySelectorAll(".ag-actions > button").length).toBe(2);
  });
});

const PLAN: HarnessPlan = {
  revision: 1,
  digest: "abc",
  approval: null,
  pilot_episode: 0,
  pilot_review: null,
  questions: [],
  estimate: { minimum_requests: 3, tokens: "unknown" },
  excluded: [],
};

describe("AgentPlan", () => {
  test("approve is the primary action, marked as a person's, and enabled", async () => {
    const onApprove = mock(() => undefined);
    const { host } = await render(
      <AgentPlan
        plan={PLAN}
        busy={false}
        completed={[]}
        onApprove={onApprove}
        onPilot={() => undefined}
      />,
    );
    expect(host.querySelector(".pg-human-mark .ds-badge")).not.toBeNull();
    const approve = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Approve execution plan"),
    )!;
    expect(approve.className).toContain("ds-btn--primary");
    expect(approve.disabled).toBe(false);
    await click(approve);
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  test("open questions block approval and the reason is written beside it", async () => {
    const { host } = await render(
      <AgentPlan
        plan={{
          ...PLAN,
          questions: [{ field: "cameras", message: "Pick a camera" }],
        }}
        busy={false}
        completed={[]}
        onApprove={() => undefined}
        onPilot={() => undefined}
      />,
    );
    const approve = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Approve execution plan"),
    )!;
    expect(approve.disabled).toBe(true);
    const id = approve.getAttribute("aria-describedby")!;
    expect(host.querySelector(`#${CSS.escape(id)}`)!.textContent).toContain(
      "questions above",
    );
    expect(host.querySelector('[role="alert"]')!.textContent).toContain(
      "Pick a camera",
    );
  });

  test("pilot buttons wait for a review note, and say so", async () => {
    const { host } = await render(
      <AgentPlan
        plan={{ ...PLAN, approval: { actor: "you" } }}
        busy={false}
        completed={[0]}
        draftRevision={1}
        onApprove={() => undefined}
        onPilot={() => undefined}
      />,
    );
    const accept = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Accept pilot quality"),
    )!;
    expect(accept.disabled).toBe(true);
    const why = host.querySelector(
      `#${CSS.escape(accept.getAttribute("aria-describedby")!)}`,
    )!;
    expect(why.textContent).toContain("review note");
    expect(host.querySelector("textarea")!.className).toContain("ds-input");
  });
});

describe("AgentReviewQueue", () => {
  const proposals = [
    {
      episode_index: 0,
      kind: "segment",
      content: "Pick",
      start: 0,
      end: 2,
      evidence_ids: [],
    },
  ];
  const queue = (extra: Record<string, unknown> = {}) => (
    <AgentReviewQueue
      proposals={proposals}
      decisions={{}}
      disabled={false}
      onChange={() => undefined}
      onDecision={() => undefined}
      onEvidence={() => undefined}
      {...extra}
    />
  );

  test("fields are ds fields with labels; start and end time are named", async () => {
    const { host } = await render(queue());
    const labels = Array.from(host.querySelectorAll("label")).map(
      (l) => l.textContent,
    );
    expect(labels).toContain("Start time");
    expect(labels).toContain("End time");
    expect(host.querySelector("select")!.closest(".ds-select")).not.toBeNull();
    expect(host.querySelector("textarea")!.className).toContain("ds-input");
  });

  test("committed changes close the decisions and the reason is next to them", async () => {
    const { host } = await render(
      queue({ disabled: true, closedReason: "Committed, so closed." }),
    );
    const accept = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Accept & next"),
    )!;
    expect(accept.disabled).toBe(true);
    const why = host.querySelector(
      `#${CSS.escape(accept.getAttribute("aria-describedby")!)}`,
    )!;
    expect(why.textContent).toBe("Committed, so closed.");
  });
});

describe("ChipMultiSelect", () => {
  test("chips are ds Buttons used as listbox options; pressing one selects it", async () => {
    const onChange = mock((_next: string[]) => undefined);
    const { host } = await render(
      <ChipMultiSelect
        label="Episodes"
        options={[
          { value: "0", label: "0" },
          { value: "1", label: "1" },
        ]}
        selected={["1"]}
        onChange={onChange}
      />,
    );
    expect(
      host.querySelector('[role="listbox"]')!.getAttribute("aria-label"),
    ).toBe("Episodes");
    const chips = Array.from(host.querySelectorAll('[role="option"]'));
    expect(chips.length).toBe(2);
    expect(chips.every((c) => c.className.includes("ds-btn"))).toBe(true);
    expect(chips[0].getAttribute("aria-selected")).toBe("false");
    expect(chips[1].getAttribute("aria-selected")).toBe("true");
    expect(chips[1].className).toContain("ds-btn--primary");
    await fire(
      chips[0],
      new Event("pointerdown", { bubbles: true, cancelable: true }),
    );
    expect(onChange).toHaveBeenCalledWith(["0", "1"]);
  });
});

describe("OllamaRuntime", () => {
  const original = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = original;
  });

  test("an owned-service action stays blocked until it is authorised, and says so", async () => {
    globalThis.fetch = mock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            installed: true,
            running: false,
            url: "http://127.0.0.1:11435",
            models_path: "/m",
            log_path: "/l",
          }),
        ),
      ),
    ) as unknown as typeof fetch;
    const { host } = await render(
      <OllamaRuntime
        configured={async () => undefined}
        connectionExists={false}
      />,
    );
    await click(
      Array.from(host.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Check runtime installation"),
      )!,
    );
    await flush(10);
    const start = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Start owned service"),
    )!;
    expect(start.disabled).toBe(true);
    const why = host.querySelector(
      `#${CSS.escape(start.getAttribute("aria-describedby")!)}`,
    )!;
    expect(why.textContent).toContain("authorization");
    // The consent is a ds Checkbox; ticking it frees the button.
    const consent = host.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement;
    expect(consent.className).toContain("ds-checkbox");
    await click(consent);
    expect(
      (
        Array.from(host.querySelectorAll("button")).find((b) =>
          b.textContent?.includes("Start owned service"),
        ) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });
});

describe("AgentActivity follows the reduced-motion setting when it scrolls", () => {
  const originalFetch = globalThis.fetch;
  const originalSource = (globalThis as { EventSource?: unknown }).EventSource;
  afterEach(() => {
    globalThis.fetch = originalFetch;
    (globalThis as { EventSource?: unknown }).EventSource = originalSource;
  });
  function stubNetwork() {
    globalThis.fetch = mock(() =>
      Promise.resolve(new Response(JSON.stringify({ events: [], tasks: [] }))),
    ) as unknown as typeof fetch;
    (globalThis as { EventSource?: unknown }).EventSource = class {
      close() {}
    };
  }

  test("smooth by default, instant under data-motion=reduce", async () => {
    stubNetwork();
    const calls: Array<ScrollToOptions> = [];
    const proto = HTMLElement.prototype as unknown as {
      scrollTo: (o: ScrollToOptions) => void;
    };
    const before = proto.scrollTo;
    proto.scrollTo = function (o: ScrollToOptions) {
      calls.push(o);
    };
    try {
      await render(<AgentActivity open />);
      await flush(10);
      expect(calls.at(-1)?.behavior).toBe("smooth");
      calls.length = 0;
      await render(
        <div data-motion="reduce">
          <AgentActivity open />
        </div>,
      );
      await flush(10);
      expect(calls.at(-1)?.behavior).toBe("auto");
    } finally {
      proto.scrollTo = before;
    }
  });
});
