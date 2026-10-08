import { click, render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { CompositionPanel } from "../composition-panel";
import { EMPTY_FILTERS, FacetsPanel } from "../facets-panel";
import { EpisodeTable } from "../pool-tables";
import {
  OUTCOME_SOURCE_LABELS,
  PICK_REASONS,
  REASON_LABELS,
  WARNING_LABELS,
  warningText,
  type EpisodeRow,
  type Facets,
  type PoolWarning,
  type Preview,
  type Recipe,
} from "../types";

setupDom();

function episode(extra: Partial<EpisodeRow>): EpisodeRow {
  return {
    key: "/pool/a/demo_0001",
    source: "a",
    source_path: "/pool/a",
    category: "rollout",
    task: "stack the plates",
    episode: "demo_0001",
    canonical: true,
    exportable: true,
    outcome: "success",
    outcome_source: "robot_flag",
    frames: 10,
    format: "robot_capture",
    heldout: false,
    ...extra,
  } as unknown as EpisodeRow;
}

function table(rows: EpisodeRow[], includeHoldback?: boolean) {
  return (
    <EpisodeTable
      rows={rows}
      total={rows.length}
      offset={0}
      pageSize={50}
      chosenTasks={["stack the plates"]}
      includeHoldback={includeHoldback}
      exclude={[]}
      onPage={() => {}}
      onToggleExclude={() => {}}
    />
  );
}

describe("verified outcomes in the episode table", () => {
  test("the outcome source says verified", async () => {
    const { host } = await render(
      table([episode({ outcome_source: "verified" })]),
    );
    expect(host.querySelector(".pg-pool-outcome-source")!.textContent).toBe(
      " · verified",
    );
  });

  test("a human label and the robot flag keep their words", async () => {
    const { host } = await render(
      table([
        episode({ key: "h", outcome_source: "human", human_label: "success" }),
        episode({ key: "r", outcome_source: "robot_flag" }),
      ]),
    );
    const words = Array.from(
      host.querySelectorAll(".pg-pool-outcome-source"),
    ).map((n) => n.textContent);
    expect(words).toEqual([" · human label", " · robot flag"]);
  });
});

describe("held-back episodes in the episode table", () => {
  test("a held-back row has a badge instead of a checkbox", async () => {
    const { host } = await render(
      table([episode({ holdback: true, holdback_set: "parked" })]),
    );
    expect(host.querySelector("input[type=checkbox]")).toBeNull();
    expect(host.textContent).toContain("held back");
  });

  test("when the recipe includes them it is a normal row, still badged", async () => {
    const { host } = await render(
      table([episode({ holdback: true, holdback_set: "parked" })], true),
    );
    expect(host.querySelector("input[type=checkbox]")).not.toBeNull();
    expect(host.textContent).toContain("held back");
  });

  test("held-out wins over held-back", async () => {
    const { host } = await render(
      table([episode({ holdback: true, heldout: true })], true),
    );
    expect(host.querySelector("input[type=checkbox]")).toBeNull();
    expect(host.textContent).toContain("held-out");
  });

  test("an episode that is not held back is as before", async () => {
    const { host } = await render(table([episode({})]));
    expect(host.querySelector("input[type=checkbox]")).not.toBeNull();
    expect(host.textContent).not.toContain("held back");
  });
});

const FACETS = {
  episodes: 6,
  categories: {},
  sources: [],
  formats: {},
  policies: {},
  policy_models: {},
  policy_checkpoints: {},
  policy_methods: {},
  outcomes: { checked_success: 1 },
  date_min: null,
  date_max: null,
  hidden_heldout: 0,
  hidden_copies: 0,
  archive: 0,
} as unknown as Facets;

describe("the held-back filter", () => {
  test("is not offered when nothing is held back", async () => {
    const { host } = await render(
      <FacetsPanel
        facets={FACETS}
        filters={EMPTY_FILTERS}
        onChange={() => {}}
      />,
    );
    expect(host.textContent).not.toContain("Held back");
  });

  test("shows how many are held back and sets the filter", async () => {
    const seen: string[] = [];
    const { host } = await render(
      <FacetsPanel
        facets={{ ...FACETS, holdback: 3 }}
        filters={EMPTY_FILTERS}
        onChange={(next) => seen.push(next.holdback)}
      />,
    );
    expect(host.textContent).toContain("Held back");
    const only = Array.from(host.querySelectorAll("label")).find((l) =>
      l.textContent?.includes("Only held back"),
    )!;
    expect(only.textContent).toContain("3");
    await click(only.querySelector("input"));
    expect(seen).toEqual(["only"]);
  });

  test("stays offered while a filter is set, so it can be undone", async () => {
    const { host } = await render(
      <FacetsPanel
        facets={FACETS}
        filters={{ ...EMPTY_FILTERS, holdback: "hide" }}
        onChange={() => {}}
      />,
    );
    expect(host.textContent).toContain("Without held back");
  });

  test("the outcome list offers human-labelled or verified success", async () => {
    const { host } = await render(
      <FacetsPanel
        facets={FACETS}
        filters={EMPTY_FILTERS}
        onChange={() => {}}
      />,
    );
    expect(host.textContent).toContain("Human-labelled or verified success");
  });
});

const RECIPE: Recipe = {
  name: "",
  categories: [],
  sources: [],
  tasks: [{ task: "t", count: null, success_ratio: null, strategy: "quality" }],
  outcome: "checked_success",
  per_task_cap: null,
  seed: 0,
  date_from: null,
  date_to: null,
  policies: [],
  exclude: [],
  include_nonstandard: false,
};

const PREVIEW: Preview = {
  warnings: [],
  outcome_sources: {},
  excluded_label_conflicts: 0,
  episodes: 1,
  frames: 10,
  tasks: [],
  tasks_without_episodes: [],
  categories: {},
  formats: {},
  outcomes: {},
  excluded: {},
  excluded_heldout: 0,
  excluded_duplicates: 0,
  excluded_nonstandard: 0,
  excluded_unsupported: 0,
};

function panel(recipe: Recipe, preview: Partial<Preview> | null) {
  return createElement(CompositionPanel, {
    recipe,
    preview: preview && { ...PREVIEW, ...preview },
    previewError: "",
    previewing: false,
    recipes: [],
    onChange: () => {},
    onSave: () => {},
    onLoad: () => {},
    onDelete: () => {},
    onClear: () => {},
    onListEpisodes: () => Promise.resolve({ episodes: [] } as never),
    refreshKey: "",
  });
}

describe("the composition panel", () => {
  test("offers to include held-back episodes only when some were left out", async () => {
    const none = await render(panel(RECIPE, null));
    expect(none.host.textContent).not.toContain("Include held-back episodes");
    const left = await render(
      panel(RECIPE, { excluded_holdback: 2, excluded: { held_back: 2 } }),
    );
    expect(left.host.textContent).toContain("Include held-back episodes");
    expect(left.host.textContent).toContain("Held back (set aside for now)");
  });

  test("names the chosen outcome and where the outcomes come from", async () => {
    const { host } = await render(
      panel(RECIPE, { outcome_sources: { verified: 1 } }),
    );
    expect(host.textContent).toContain("Human-labelled or verified success");
    expect(host.textContent).toContain("verified 1");
  });
});

describe("the words are in both languages", () => {
  const strings = [
    ...Object.values(WARNING_LABELS),
    ...Object.values(OUTCOME_SOURCE_LABELS),
    ...Object.values(PICK_REASONS),
    REASON_LABELS.held_back,
    "Held back",
    "held back",
    "Only held back",
    "Without held back",
    "Include held-back episodes",
    "Human-labelled or verified success",
    "Hold-back lists",
    "Verified-outcome lists",
  ];
  test("every one has a Chinese entry", () => {
    const missing = strings.filter((text) => !(text in zh) || !(text in en));
    expect(missing).toEqual([]);
  });

  test("the hold-back words are the agreed ones", () => {
    const zhText = zh as Record<string, string>;
    expect(zhText["held back"]).toBe("暂留");
    expect(zhText["verified"]).toBe("核实");
  });

  test("a new warning reads from its label, not the server sentence", () => {
    const warning: PoolWarning = {
      code: "holdback_lists_changed",
      blocking: true,
      message: "The hold-back lists changed since the last scan; scan again",
    };
    expect(
      warningText(warning, (s) => (zh as Record<string, string>)[s] ?? s, "zh"),
    ).toBe("暂留清单在上次扫描后有改动：请重新扫描。");
  });
});
