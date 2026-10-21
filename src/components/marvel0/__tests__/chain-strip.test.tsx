import { act } from "react";
import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { chainView, CHAIN, nodeAnchor, type ChainInput } from "../chain-logic";
import { ChainStrip } from "../chain-strip";
import { stackFromCards, type Marvel0Card } from "../marvel0-logic";
import { actView } from "../stack-act-logic";
import { stackView } from "../stack-status-logic";
import { stackDoc } from "./ctl-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const KNOWN = new Set(Object.keys(en));
const card = (
  id: Marvel0Card["id"],
  state: Marvel0Card["state"],
): Marvel0Card => ({
  id,
  title: "t",
  state,
  summary: { key: "k" },
  facts: [],
  notes: [],
  hint: "h",
});
const stack = (name: string) => {
  const doc = stackDoc(name);
  const passive = stackView(doc, KNOWN)!;
  return { passive, act: actView(doc, KNOWN, passive) };
};
const input = (name: string): ChainInput => ({
  strip: stackFromCards([card("desk", "ok")], null),
  stack: stack(name),
  deploy: null,
  run: null,
});

describe("the chain at the top", () => {
  test("after a gripper test that failed node 04 reads 'Ready · test failed' (warning) and is the line under the chain", async () => {
    const { host } = await render(
      <ChainStrip chain={chainView(input("gripper_failed"))} cause={null} />,
    );
    const b = host.querySelector("[data-testid=marvel0-strip-B]")!;
    expect(b.textContent).toContain("04");
    expect(b.textContent).toContain(
      en["marvel0.chain.state.ready_test_failed"],
    );
    expect(b.getAttribute("data-state")).toBe("attention");
    expect(b.getAttribute("data-detail")).toBe("test_failed");
    const focus = host.querySelector("[data-testid=marvel0-chain-focus]")!;
    expect(focus.getAttribute("data-node")).toBe("B");
    expect(focus.getAttribute("data-kind")).toBe("check");
    expect(focus.textContent).toContain("Check B");
    expect(focus.textContent).toContain(en["marvel0.chain.reason.test_failed"]);
    expect(zh["marvel0.chain.state.ready_test_failed"]).toBe("就绪 · 测试失败");
  });

  test("seven connected nodes with their state in words, and the first one that is not ready marked with its reason", async () => {
    const { host } = await render(
      <ChainStrip chain={chainView(input("a_starting"))} cause={null} />,
    );
    const items = Array.from(host.querySelectorAll("li"));
    expect(items.map((li) => li.getAttribute("data-testid"))).toEqual(
      CHAIN.map((id) => `marvel0-strip-${id}`),
    );
    const states = Object.fromEntries(
      items.map((li) => [
        li.getAttribute("data-testid"),
        li.getAttribute("data-state"),
      ]),
    );
    expect(states).toMatchObject({
      "marvel0-strip-M": "ready",
      "marvel0-strip-desk": "ready",
      "marvel0-strip-A": "working",
      "marvel0-strip-B": "idle",
    });
    // The state is written, not only coloured.
    expect(items[2].textContent).toContain("Working");
    // The node the person should look at is marked, once, with the reason.
    const marked = host.querySelectorAll("[data-focus]");
    expect(marked).toHaveLength(1);
    expect(marked[0].getAttribute("data-testid")).toBe("marvel0-strip-A");
    expect(
      marked[0].querySelector("button")!.getAttribute("aria-current"),
    ).toBe("step");
    const focus = host.querySelector("[data-testid=marvel0-chain-focus]")!;
    expect(focus.getAttribute("data-node")).toBe("A");
    expect(focus.textContent).toContain("is working");
    expect(focus.textContent).toContain("12 s so far");
    // Connected: a link between neighbours, none after the last.
    expect(host.querySelectorAll(".rg-chain__link")).toHaveLength(6);
  });

  test("each node carries a glyph of its state, its step number and, when it has one, its letter; the count says how many are ready", async () => {
    const { host } = await render(
      <ChainStrip chain={chainView(input("a_starting"))} cause={null} />,
    );
    expect(
      host.querySelector("[data-testid=marvel0-strip-count]")!.textContent,
    ).toBe("2 of 7 ready");
    const nodes = Array.from(host.querySelectorAll(".rg-node"));
    expect(nodes).toHaveLength(7);
    for (const [index, node] of nodes.entries()) {
      expect(node.querySelector(".rg-node__glyph svg")).not.toBeNull();
      expect(node.querySelector(".rg-node__idx")!.textContent).toBe(
        String(index + 1).padStart(2, "0"),
      );
    }
    // "A · Arm" is a letter and a name; Desk has no letter.
    expect(nodes[2].querySelector(".rg-node__code")!.textContent).toBe("A");
    expect(nodes[1].querySelector(".rg-node__code")).toBeNull();
    // The wire after a node carries the state of what flows out of it.
    expect(
      Array.from(host.querySelectorAll(".rg-chain__link")).map((link) =>
        link.className.replace("rg-chain__link rg-chain__link--", ""),
      ),
    ).toEqual(["ready", "ready", "working", "idle", "idle", "unknown"]);
    // The decoration is hidden from a screen reader; the words stay.
    for (const hidden of host.querySelectorAll(
      ".rg-node__glyph, .rg-node__idx, .rg-chain__link",
    ))
      expect(hidden.getAttribute("aria-hidden")).toBe("true");
  });

  test("in Chinese the count and the letters are worded", async () => {
    try {
      localStorage.setItem("levi-language", "zh");
    } catch {
      // no storage in this DOM
    }
    const { host } = await render(
      <LocaleProvider>
        <ChainStrip chain={chainView(input("a_starting"))} cause={null} />
      </LocaleProvider>,
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(
      host.querySelector("[data-testid=marvel0-strip-count]")!.textContent,
    ).toBe("2/7 已就绪");
    expect(host.querySelector(".rg-node__code")!.textContent).toBe("M");
  });

  test("a blocked node says why in the backend's words", async () => {
    const chain = chainView({
      ...input("idle"),
      strip: stackFromCards([card("desk", "ok")], null),
    });
    const { host } = await render(<ChainStrip chain={chain} cause={null} />);
    const focus = host.querySelector("[data-testid=marvel0-chain-focus]")!;
    expect(focus.getAttribute("data-kind")).toBe("next");
    expect(focus.textContent).toContain("Next step");
  });

  test("a click on a node moves to its card and puts the keyboard there", async () => {
    const target = document.createElement("section");
    target.id = nodeAnchor("B");
    target.tabIndex = -1;
    let scrolled = 0;
    target.scrollIntoView = () => {
      scrolled += 1;
    };
    document.body.appendChild(target);
    const { host } = await render(
      <ChainStrip chain={chainView(input("a_ready"))} cause={null} />,
    );
    await click(host.querySelector("[data-testid=marvel0-strip-B] button"));
    expect(scrolled).toBe(1);
    expect(document.activeElement).toBe(target);
    target.remove();
  });

  test("the nodes are buttons the keyboard reaches, in order", async () => {
    const { host } = await render(
      <ChainStrip chain={chainView(input("idle"))} cause={null} />,
    );
    const buttons = Array.from(host.querySelectorAll("button"));
    expect(buttons).toHaveLength(7);
    for (const b of buttons) expect(b.tabIndex).toBe(0);
  });

  test("what several nodes share comes first, above the nodes", async () => {
    const { host } = await render(
      <ChainStrip
        chain={chainView(input("idle"))}
        cause={{ key: "marvel0.cause.monitor_missing" }}
      />,
    );
    const cause = host.querySelector("[data-testid=marvel0-strip-cause]")!;
    expect(
      cause.compareDocumentPosition(host.querySelector("ol")!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("in Chinese the title, the states and the focus line are worded", async () => {
    localStorage.setItem("levi-language", "zh");
    const { host } = await render(
      <LocaleProvider>
        <ChainStrip chain={chainView(input("a_starting"))} cause={null} />
      </LocaleProvider>,
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(host.textContent).toContain(zh["marvel0.strip.title"]);
    expect(host.textContent).toContain(zh["marvel0.chain.state.working"]);
    expect(host.textContent).toContain("正在进行");
    expect(host.textContent).not.toContain("Working");
  });

  test("when everything is ready the chain says so and marks no node", async () => {
    const all = chainView({
      strip: stackFromCards(
        (
          [
            "monitor",
            "desk",
            "arm",
            "gripper",
            "robot_server",
            "policy",
            "demo",
          ] as const
        ).map((id) => card(id, "ok")),
        null,
      ),
      stack: null,
      deploy: null,
      run: null,
    });
    const { host } = await render(<ChainStrip chain={all} cause={null} />);
    expect(host.querySelectorAll("[data-focus]")).toHaveLength(0);
    expect(
      host.querySelector("[data-testid=marvel0-chain-focus]")!.textContent,
    ).toBe(en["marvel0.chain.focus.done"]);
  });
});
