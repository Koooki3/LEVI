import {
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { useEffect, type ReactNode } from "react";
import { act } from "react";
import { LocaleProvider } from "@/components/levi-locale";
import {
  DatasetSourceProvider,
  useDatasetSource,
} from "@/context/dataset-source-context";
import {
  AnnotationsProvider,
  useAnnotations,
} from "@/context/annotations-context";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import AnnotationRecorder from "@/components/annotation-recorder";
import LeviReview from "@/components/levi-review";
import {
  LinkedDatasetNotice,
  ReadOnlyReason,
} from "@/components/linked-dataset-notice";
import { LINKED_READ_ONLY } from "@/utils/linkedDataset";
import type { LanguageAtom } from "@/types/language.types";

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const LINKED_ENTRY = {
  id: "local/live.run1",
  name: "live.run1",
  path: "/somewhere/else/captures/run1",
  linked: {
    kind: "live",
    source: "run1",
    workspace: "levi-live-ws",
    readonly: true,
  },
};

/** What the service answers for the page's reads and writes; `writes` collects
 * every request that is not a read. */
function serve(
  entry: unknown | null,
  writes: { method: string; url: string }[] = [],
) {
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method !== "GET") writes.push({ method, url });
    if (url.includes("/api/levi/catalog/"))
      return Promise.resolve(entry ? json(entry) : json({}, 404));
    if (url.includes("/atoms") && method === "GET")
      return Promise.resolve(json({ atoms: [] }));
    if (url.includes("/api/levi/review")) return Promise.resolve(json({}));
    return Promise.resolve(json({}));
  }) as unknown as typeof fetch;
  return writes;
}

const atom = (over: Partial<LanguageAtom> = {}): LanguageAtom => ({
  role: "assistant",
  content: "reach",
  style: "subtask",
  timestamp: 1,
  camera: null,
  tool_calls: null,
  ...over,
});

function Dataset({
  dataset,
  children,
}: {
  dataset: string;
  children: ReactNode;
}) {
  return (
    <LocaleProvider>
      <DatasetSourceProvider org="local" dataset={dataset}>
        {children}
      </DatasetSourceProvider>
    </LocaleProvider>
  );
}

describe("the viewer's note on a live evaluation workspace's dataset", () => {
  test("says it is read-only and where to label it, once the catalog says it is linked", async () => {
    serve(LINKED_ENTRY);
    const { host } = await render(
      <Dataset dataset="live.run1">
        <LinkedDatasetNotice />
      </Dataset>,
    );
    const note = await waitFor(() => host.querySelector(".vw-note--linked"));
    expect(note.getAttribute("role")).toBe("note");
    expect(note.textContent).toContain("Live evaluation · read-only");
    expect(note.textContent).toContain(
      "This is a dataset of the live evaluation workspace: read-only.",
    );
    expect(note.textContent).toContain("where the live service keeps them");
    expect(note.querySelector(".ds-badge--info")).not.toBeNull();
  });

  test("is in Chinese on a Chinese page", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(LINKED_ENTRY);
    const { host } = await render(
      <Dataset dataset="live.run1">
        <LinkedDatasetNotice />
      </Dataset>,
    );
    const note = await waitFor(() => host.querySelector(".vw-note--linked"));
    await flush(20);
    expect(note.textContent).toContain("实时评测 · 只读");
    expect(note.textContent).toContain("这是实时评测工作区的数据集：只读。");
    expect(note.textContent).toContain("要标注或复核");
    expect(note.textContent).not.toContain("This is a dataset");
  });

  test("not on a dataset of the user's own, even one named live.x (the catalog decides)", async () => {
    serve({ id: "local/live.mine", name: "live.mine", path: "/w/mine" });
    const { host } = await render(
      <Dataset dataset="live.mine">
        <LinkedDatasetNotice />
        <ReadOnlyReason id="why" />
      </Dataset>,
    );
    // Before the entry answers the name's prefix says "linked" (the safe side) ...
    await flush(0);
    // ... and once it has answered, the entry has the last word.
    await waitFor(() => host.querySelector(".vw-note--linked") === null);
    expect(host.querySelector("#why")).toBeNull();
    const plain = await render(
      <Dataset dataset="run2">
        <LinkedDatasetNotice />
      </Dataset>,
    );
    await flush(20);
    expect(plain.host.querySelector(".vw-note--linked")).toBeNull();
  });

  test("the reason beside a disabled control is words, with the id the control points at", async () => {
    serve(LINKED_ENTRY);
    const { host } = await render(
      <Dataset dataset="live.run1">
        <ReadOnlyReason id="why" />
      </Dataset>,
    );
    const reason = await waitFor(() => host.querySelector("#why"));
    expect(reason.textContent).toBe(
      "Read-only: belongs to the live evaluation workspace",
    );
    expect(reason.hasAttribute("title")).toBe(false);
  });
});

/** Puts the episode in the provider and exposes the context to the test. */
function Probe({ out }: { out: { ctx?: ReturnType<typeof useAnnotations> } }) {
  const ctx = useAnnotations();
  out.ctx = ctx;
  const { setEpisode } = ctx;
  useEffect(() => {
    setEpisode(3, { repoId: "local/live.run1" });
  }, [setEpisode]);
  return null;
}

describe("annotations of a linked dataset", () => {
  test("nothing edits, saves or deletes them, and nothing is sent", async () => {
    const writes = serve(LINKED_ENTRY);
    const out: { ctx?: ReturnType<typeof useAnnotations> } = {};
    await render(
      <Dataset dataset="live.run1">
        <AnnotationsProvider>
          <Probe out={out} />
        </AnnotationsProvider>
      </Dataset>,
    );
    await waitFor(() => out.ctx?.episodeId === 3);
    await waitFor(() => out.ctx?.readOnly === true);
    await act(async () => {
      out.ctx!.addAtom(atom());
      out.ctx!.addAtoms([atom({ timestamp: 2 })]);
      out.ctx!.setDrawMode("auto");
      out.ctx!.setPendingDraw({
        kind: "keypoint",
        point: [0.5, 0.5],
        label: "x",
      });
    });
    expect(out.ctx!.atoms).toEqual([]);
    expect(out.ctx!.dirty).toBe(false);
    expect(out.ctx!.drawMode).toBe("off");
    expect(out.ctx!.pendingDraw).toBeNull();
    const saved = await out.ctx!.save();
    expect(saved).toEqual({ ok: false, error: LINKED_READ_ONLY });
    const removed = await out.ctx!.deleteEpisodeFile();
    expect(removed).toEqual({ ok: false, error: LINKED_READ_ONLY });
    await out.ctx!.flushAllEpisodes();
    expect(sessionStorage.length).toBe(0);
    expect(writes.filter((w) => w.url.includes("/atoms"))).toEqual([]);
  });

  test("a dataset of the user's own still edits (the guard is only for linked ones)", async () => {
    serve({ id: "local/run2", name: "run2", path: "/w/run2" });
    const out: { ctx?: ReturnType<typeof useAnnotations> } = {};
    function Own() {
      const ctx = useAnnotations();
      out.ctx = ctx;
      const { setEpisode } = ctx;
      useEffect(() => {
        setEpisode(3, { repoId: "local/run2" });
      }, [setEpisode]);
      return null;
    }
    await render(
      <Dataset dataset="run2">
        <AnnotationsProvider>
          <Own />
        </AnnotationsProvider>
      </Dataset>,
    );
    await waitFor(() => out.ctx?.episodeId === 3);
    await act(async () => out.ctx!.addAtom(atom()));
    expect(out.ctx!.readOnly).toBe(false);
    expect(out.ctx!.atoms).toHaveLength(1);
    expect(out.ctx!.dirty).toBe(true);
  });

  test("the recorder offers no button, only the reason", async () => {
    serve(LINKED_ENTRY);
    const { host } = await render(
      <Dataset dataset="live.run1">
        <AnnotationsProvider>
          <Probe out={{}} />
          <AnnotationRecorder />
        </AnnotationsProvider>
      </Dataset>,
    );
    const box = await waitFor(() =>
      host.querySelector('[data-testid="annotation-recorder"]'),
    );
    await waitFor(() => box.textContent?.includes("Read-only"));
    expect(box.querySelector("button")).toBeNull();
    expect(box.textContent).toContain(
      "Read-only: belongs to the live evaluation workspace",
    );
  });

  test("the review export and notes are off, with the reason next to them", async () => {
    const writes = serve(LINKED_ENTRY);
    const { host } = await render(
      <Dataset dataset="live.run1">
        <FlaggedEpisodesProvider repoId="local/live.run1">
          <LeviReview repoId="local/live.run1" />
        </FlaggedEpisodesProvider>
      </Dataset>,
    );
    await waitFor(() => host.querySelector("#review-readonly-why"));
    const buttons = [...host.querySelectorAll("button")];
    expect(buttons.length).toBeGreaterThanOrEqual(2);
    for (const button of buttons) {
      expect(button.disabled).toBe(true);
      expect(button.getAttribute("aria-describedby")).toBe(
        "review-readonly-why",
      );
    }
    expect(host.querySelector("#review-readonly-why")!.textContent).toContain(
      "Read-only",
    );
    expect(writes).toEqual([]);
  });
});

describe("what the source context says", () => {
  test("a dataset on the Hub is never linked", async () => {
    serve(null);
    let seen: boolean | null = null;
    function Seen() {
      seen = useDatasetSource().linked;
      return null;
    }
    await render(
      <DatasetSourceProvider org="lerobot" dataset="live.run1">
        <Seen />
      </DatasetSourceProvider>,
    );
    await flush(20);
    expect(seen).toBe(false);
  });
});
