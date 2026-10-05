import {
  click,
  mockMatchMedia,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import Sidebar from "@/components/side-nav";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import type { DatasetDisplayInfo } from "@/app/[org]/[dataset]/[episode]/fetch-data";

setupDom();

const info = {
  repoId: "local/demo",
  total_frames: 1200,
  total_episodes: 3,
  fps: 30,
} as unknown as DatasetDisplayInfo;

function renderSidebar(
  props: Partial<React.ComponentProps<typeof Sidebar>> = {},
) {
  return render(
    <FlaggedEpisodesProvider repoId="local/demo-test">
      <Sidebar
        datasetInfo={info}
        paginatedEpisodes={[0, 1, 2]}
        episodeId={1}
        totalPages={1}
        currentPage={1}
        prevPage={() => undefined}
        nextPage={() => undefined}
        showFlaggedOnly={false}
        onShowFlaggedOnlyChange={() => undefined}
        onEpisodeSelect={() => undefined}
        {...props}
      />
    </FlaggedEpisodesProvider>,
  );
}

describe("episode list", () => {
  test("is a labelled navigation with the current episode marked", async () => {
    const { host } = await renderSidebar();
    const nav = host.querySelector("nav")!;
    expect(nav.getAttribute("aria-label")).toBe("Episode list");
    const rows = [...host.querySelectorAll(".vw-episode")];
    expect(rows).toHaveLength(3);
    expect(rows[1].getAttribute("aria-current")).toBe("true");
    expect(rows[0].getAttribute("aria-current")).toBeNull();
    expect(rows[1].querySelector(".vw-episode-link")!.textContent).toBe(
      "Episode 1",
    );
  });

  test("selecting an episode calls back with its index", async () => {
    const onSelect = mock((episode: number) => void episode);
    const { host } = await renderSidebar({ onEpisodeSelect: onSelect });
    await click(host.querySelectorAll(".vw-episode-link")[2]);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  test("outcomes are a shape and words, not colour alone, and cycle when editable", async () => {
    const onOutcome = mock(
      (episode: number, outcome: string | null) => void [episode, outcome],
    );
    const { host } = await renderSidebar({
      episodeOutcomes: { "0": "success", "2": "failure" },
      humanOutcomes: new Set(["0"]),
      onOutcomeChange: onOutcome,
    });
    const marks = [...host.querySelectorAll(".vw-outcome")];
    expect(marks.map((m) => m.getAttribute("data-outcome"))).toEqual([
      "success",
      "none",
      "failure",
    ]);
    expect(marks[0].getAttribute("aria-label")).toContain("Episode succeeded");
    expect(marks[0].getAttribute("data-human")).toBe("true");
    expect(marks[2].getAttribute("aria-label")).toContain("Episode failed");
    expect(marks[0].querySelector("svg")).not.toBeNull();
    // A human success label goes to failure next.
    await click(marks[0]);
    expect(onOutcome).toHaveBeenCalledWith(0, "failure");
    // A metadata outcome is overridden by a human success first.
    await click(marks[2]);
    expect(onOutcome).toHaveBeenLastCalledWith(2, "success");
  });

  test("the flag is a pressed toggle with a name", async () => {
    const { host } = await renderSidebar();
    const flag = host.querySelectorAll(".vw-flag")[0] as HTMLButtonElement;
    expect(flag.getAttribute("aria-pressed")).toBe("false");
    expect(flag.getAttribute("aria-label")).toBe("Flag · Episode 0");
    await click(flag);
    expect(
      host.querySelectorAll(".vw-flag")[0].getAttribute("aria-pressed"),
    ).toBe("true");
    // The "Flagged" filter appears once something is flagged.
    const chip = [...host.querySelectorAll(".vw-chip")].find((c) =>
      c.textContent?.startsWith("Flagged"),
    );
    expect(chip?.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("the narrow window's episode list drawer", () => {
  const toggle = (host: HTMLElement) =>
    host.querySelector<HTMLButtonElement>(".vw-sidebar-toggle button")!;

  test("the toggle says expanded, not pressed, and names what it controls", async () => {
    const { host } = await renderSidebar();
    const button = toggle(host);
    const nav = host.querySelector("nav")!;
    expect(button.hasAttribute("aria-pressed")).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-controls")).toBe(nav.id);
    await click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(nav.getAttribute("data-mobile-hidden")).toBeNull();
  });

  test("Escape folds it and returns focus to the toggle; not from a text field, not on a wide window", async () => {
    const restoreNarrow = mockMatchMedia(["max-width: 899px"]);
    try {
      const { host } = await renderSidebar();
      const button = toggle(host);
      const nav = host.querySelector("nav")!;
      await click(button);
      // A text field keeps its own Escape.
      const field = document.createElement("input");
      document.body.appendChild(field);
      await press(field, "Escape");
      expect(nav.getAttribute("data-mobile-hidden")).toBeNull();
      field.remove();
      await press(document.body, "Escape");
      expect(nav.getAttribute("data-mobile-hidden")).toBe("true");
      expect(button.getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(button);
    } finally {
      restoreNarrow();
    }
    // Wide window: the list is always there; Escape does nothing to it.
    const restoreWide = mockMatchMedia([]);
    try {
      const { host } = await renderSidebar();
      await click(toggle(host));
      await press(document.body, "Escape");
      expect(toggle(host).getAttribute("aria-expanded")).toBe("true");
    } finally {
      restoreWide();
    }
  });
});
