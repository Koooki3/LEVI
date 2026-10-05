import {
  click,
  mockMatchMedia,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";
import type { DatasetDisplayInfo } from "@/app/[org]/[dataset]/[episode]/fetch-data";
import Sidebar from "@/components/side-nav";
import StatsPanel from "@/components/stats-panel";
import { LocaleProvider } from "@/components/levi-locale";
import {
  AnnotationsProvider,
  useAnnotations,
} from "@/context/annotations-context";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import type { LanguageAtom } from "@/types/language.types";
import { InspectorLayout } from "../inspector";
import { SkipLinks } from "../skip-links";
import { isTextEntry } from "../text-entry";
import { roundTo2 } from "../time-format";

setupDom();
globalThis.fetch = mock(() =>
  Promise.resolve(new Response("{}", { status: 404 })),
) as unknown as typeof fetch;

const atom = (timestamp: number): LanguageAtom =>
  ({
    role: "assistant",
    content: "x",
    style: "subtask",
    timestamp,
  }) as LanguageAtom;

describe("numbers shown to people", () => {
  test("rates and times keep at most two decimals", () => {
    expect(roundTo2(9.349721999915758)).toBe("9.35");
    expect(roundTo2(3)).toBe("3");
    expect(roundTo2(0.1)).toBe("0.1");
    expect(roundTo2(Number.NaN)).toBe("");
  });

  const info = {
    repoId: "local/demo",
    total_frames: 1200,
    total_episodes: 3,
    fps: 9.349721999915758,
    total_tasks: 1,
    codebase_version: "v2.1",
    cameras: [],
  } as unknown as DatasetDisplayInfo;

  test("the episode list and the statistics both show the frame rate rounded", async () => {
    const { host } = await render(
      <FlaggedEpisodesProvider repoId="local/fps">
        <Sidebar
          datasetInfo={info}
          paginatedEpisodes={[0]}
          episodeId={0}
          totalPages={1}
          currentPage={1}
          prevPage={() => undefined}
          nextPage={() => undefined}
          showFlaggedOnly={false}
          onShowFlaggedOnlyChange={() => undefined}
        />
        <LocaleProvider>
          <StatsPanel
            datasetInfo={info}
            episodeLengthStats={null}
            loading={false}
          />
        </LocaleProvider>
      </FlaggedEpisodesProvider>,
    );
    expect(host.textContent).toContain("9.35");
    expect(host.textContent).not.toContain("9.3497");
  });
});

describe("episode list keyboard", () => {
  function list(episodeId: number) {
    return (
      <FlaggedEpisodesProvider repoId="local/roving">
        <Sidebar
          datasetInfo={
            {
              repoId: "local/roving",
              total_frames: 3,
              total_episodes: 3,
              fps: 10,
            } as unknown as DatasetDisplayInfo
          }
          paginatedEpisodes={[0, 1, 2]}
          episodeId={episodeId}
          totalPages={1}
          currentPage={1}
          prevPage={() => undefined}
          nextPage={() => undefined}
          showFlaggedOnly={false}
          onShowFlaggedOnlyChange={() => undefined}
          onEpisodeSelect={() => undefined}
          onOutcomeChange={() => undefined}
        />
      </FlaggedEpisodesProvider>
    );
  }

  test("the whole list is one tab stop, at the current episode", async () => {
    const { host } = await render(list(1));
    const stops = [
      ...host.querySelectorAll<HTMLElement>(".vw-episodes [data-roving]"),
    ].filter((el) => el.tabIndex === 0);
    expect(stops).toHaveLength(1);
    expect(stops[0].textContent).toBe("Episode 1");
  });

  test("left and right arrows move between a row's controls", async () => {
    const { host } = await render(list(1));
    const link = host.querySelector<HTMLElement>(
      '.vw-episode[aria-current="true"] .vw-episode-link',
    )!;
    await act(async () => link.focus());
    const row = link.closest(".vw-episode")!;
    const stops = [...row.querySelectorAll<HTMLElement>("[data-roving]")];
    expect(stops.length).toBeGreaterThanOrEqual(3);
    await press(link, "ArrowRight");
    expect(document.activeElement).toBe(stops[1]);
    await press(stops[1], "ArrowRight");
    expect(document.activeElement).toBe(stops[2]);
    await press(stops[2], "ArrowRight");
    expect(document.activeElement).toBe(stops[2]);
    await press(stops[2], "ArrowLeft");
    expect(document.activeElement).toBe(stops[1]);
  });

  test("focus follows the current episode when it changes from the keyboard", async () => {
    const { host, rerender } = await render(list(0));
    const first = host.querySelector<HTMLElement>(".vw-episode-link")!;
    await act(async () => first.focus());
    await rerender(list(1));
    expect(document.activeElement?.textContent).toBe("Episode 1");
  });
});

describe("text fields keep their own keys", () => {
  test("what counts as a text field", () => {
    const input = document.createElement("input");
    const box = document.createElement("input");
    box.type = "checkbox";
    expect(isTextEntry(input)).toBe(true);
    expect(isTextEntry(document.createElement("textarea"))).toBe(true);
    expect(isTextEntry(box)).toBe(false);
    expect(isTextEntry(document.createElement("button"))).toBe(false);
    expect(isTextEntry(null)).toBe(false);
  });

  function Probe() {
    const { atoms, addAtom, selectedIdx, selectAtom } = useAnnotations();
    return (
      <div>
        <output data-testid="count">{atoms.length}</output>
        <output data-testid="sel">{String(selectedIdx)}</output>
        <button data-testid="add" onClick={() => addAtom(atom(1))} />
        <button data-testid="select" onClick={() => selectAtom(0)} />
        <textarea data-testid="field" />
        <input data-testid="line" />
      </div>
    );
  }
  const q = (host: HTMLElement, id: string) =>
    host.querySelector<HTMLElement>(`[data-testid=${id}]`)!;

  test("Ctrl+Z in a text field is left to the browser; elsewhere it undoes", async () => {
    const { host } = await render(
      <AnnotationsProvider>
        <Probe />
      </AnnotationsProvider>,
    );
    await click(q(host, "add"));
    expect(q(host, "count").textContent).toBe("1");
    const field = q(host, "field");
    await act(async () => field.focus());
    await press(field, "z", { ctrlKey: true });
    expect(q(host, "count").textContent).toBe("1");
    const outside = q(host, "add");
    await act(async () => outside.focus());
    await press(outside, "z", { ctrlKey: true });
    expect(q(host, "count").textContent).toBe("0");
  });

  test("Escape clears the selection, but not from a text field or under a dialog", async () => {
    const { host } = await render(
      <AnnotationsProvider>
        <Probe />
      </AnnotationsProvider>,
    );
    await click(q(host, "add"));
    await click(q(host, "select"));
    expect(q(host, "sel").textContent).toBe("0");
    const line = q(host, "line");
    await act(async () => line.focus());
    await press(line, "Escape");
    expect(q(host, "sel").textContent).toBe("0");
    const modal = document.createElement("div");
    modal.setAttribute("aria-modal", "true");
    document.body.appendChild(modal);
    await press(q(host, "add"), "Escape");
    expect(q(host, "sel").textContent).toBe("0");
    modal.remove();
    await act(async () => q(host, "add").focus());
    await press(q(host, "add"), "Escape");
    expect(q(host, "sel").textContent).toBe("null");
  });

  test("on a narrow window Escape closes the inspector drawer first, then the selection", async () => {
    const restore = mockMatchMedia(["max-width: 1199px"]);
    const { host } = await render(
      <AnnotationsProvider>
        <InspectorLayout enabled>
          <Probe />
        </InspectorLayout>
      </AnnotationsProvider>,
    );
    restore();
    await click(q(host, "add"));
    await click(q(host, "select"));
    const aside = host.querySelector("aside")!;
    expect(aside.getAttribute("data-open")).toBe("false");
    await click(aside.querySelector("button[aria-expanded]")!);
    expect(aside.getAttribute("data-open")).toBe("true");
    await act(async () => q(host, "add").focus());
    await press(q(host, "add"), "Escape");
    expect(aside.getAttribute("data-open")).toBe("false");
    expect(q(host, "sel").textContent).toBe("0");
    await press(q(host, "add"), "Escape");
    expect(q(host, "sel").textContent).toBe("null");
  });
});

describe("skip links", () => {
  test("the content link focuses the main region, the inspector link its heading", async () => {
    const { host } = await render(
      <LocaleProvider>
        <SkipLinks inspector />
        <main id="vw-main" tabIndex={0} />
        <h2 id="vw-inspector-heading" tabIndex={-1} />
      </LocaleProvider>,
    );
    const links = [...host.querySelectorAll("a")];
    expect(links.map((a) => a.textContent)).toEqual([
      "Skip to content",
      "Skip to inspector",
    ]);
    await click(links[0]);
    expect(document.activeElement?.id).toBe("vw-main");
    await click(links[1]);
    expect(document.activeElement?.id).toBe("vw-inspector-heading");
  });

  test("without an inspector there is only the content link", async () => {
    const { host } = await render(
      <LocaleProvider>
        <SkipLinks inspector={false} />
      </LocaleProvider>,
    );
    expect(host.querySelectorAll("a")).toHaveLength(1);
  });
});
