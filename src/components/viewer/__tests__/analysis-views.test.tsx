import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import type {
  CrossEpisodeRequest,
  CrossEpisodeVarianceData,
  DatasetDisplayInfo,
  EpisodeLengthStats,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";
import ActionInsightsPanel from "@/components/action-insights-panel";
import StatsPanel from "@/components/stats-panel";
import { LocaleProvider } from "@/components/levi-locale";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import { AnalysisCard, fill, toneForRatio } from "../analysis-ui";

setupDom();
globalThis.fetch = mock(() =>
  Promise.resolve(new Response("{}", { status: 404 })),
) as unknown as typeof fetch;

const REQUEST: CrossEpisodeRequest = {
  scope: { kind: "all" },
  maxEpisodes: null,
};

const DATA = {
  actionNames: [],
  timeBins: [],
  variance: [],
  numEpisodes: 3,
  lowMovementEpisodes: [],
  aggVelocity: [],
  aggAutocorrelation: null,
  speedDistribution: [],
  jerkyEpisodes: [],
  aggAlignment: null,
  scope: { kind: "all" },
  scopeEpisodes: 5,
  requestedEpisodes: 3,
  sampled: true,
} as CrossEpisodeVarianceData;

function insights(
  onChange: (request: CrossEpisodeRequest) => void = () => undefined,
  data: CrossEpisodeVarianceData | null = null,
) {
  return (
    <LocaleProvider>
      <FlaggedEpisodesProvider repoId="local/demo">
        <ActionInsightsPanel
          flatChartData={[]}
          fps={30}
          crossEpisodeData={data}
          crossEpisodeLoading={false}
          totalEpisodes={5}
          tasks={["pick", "place"]}
          crossEpisodeRequest={REQUEST}
          onCrossEpisodeRequestChange={onChange}
          crossEpisodeProgress={null}
        />
      </FlaggedEpisodesProvider>
    </LocaleProvider>
  );
}

describe("analysis cards", () => {
  test("the description opens from an info button that names what it controls", async () => {
    const { host } = await render(
      <AnalysisCard title="Action Autocorrelation" info={<p>Why it matters</p>}>
        <span>body</span>
      </AnalysisCard>,
    );
    const toggle = host.querySelector<HTMLButtonElement>(
      "button[aria-expanded]",
    )!;
    expect(toggle.getAttribute("aria-label")).toBe("Toggle description");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(host.textContent).not.toContain("Why it matters");
    await click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const panel = host.querySelector(
      `#${toggle.getAttribute("aria-controls")}`,
    );
    expect(panel?.textContent).toBe("Why it matters");
    // The old plain `title=` is gone: the tooltip is the ds one.
    expect(toggle.hasAttribute("title")).toBe(false);
  });

  test("a card shows its meta text next to the title", async () => {
    const { host } = await render(
      <AnalysisCard title="Demonstrator Speed Variance" meta="(7 episodes)" />,
    );
    expect(host.querySelector("h3")!.textContent).toBe(
      "Demonstrator Speed Variance(7 episodes)",
    );
  });

  test("fill puts values into a sentence key, in any order", () => {
    expect(fill("{b} of {a}", { a: 1, b: "x" })).toBe("x of 1");
    expect(fill("no values", {})).toBe("no values");
  });

  test("a ratio maps to a tone in both directions", () => {
    expect(toneForRatio(0.1, [0.4, 0.7])).toBe("success");
    expect(toneForRatio(0.5, [0.4, 0.7])).toBe("warning");
    expect(toneForRatio(0.9, [0.4, 0.7])).toBe("danger");
    expect(toneForRatio(0.1, [0.15, 0.4], false)).toBe("danger");
    expect(toneForRatio(0.9, [0.15, 0.4], false)).toBe("success");
  });
});

describe("Action Insights scope controls", () => {
  test("the scope is a ds segmented control, not hand-made buttons", async () => {
    const { host } = await render(insights());
    const groups = [...host.querySelectorAll('[role="radiogroup"]')].map((g) =>
      g.getAttribute("aria-label"),
    );
    expect(groups).toEqual(["Analysis scope", "Analysis scope"]);
    const kinds = [
      ...host.querySelectorAll(
        '.vw-a-scope [role="radiogroup"] [role="radio"]',
      ),
    ].map((o) => o.textContent);
    expect(kinds).toEqual(["Full dataset", "Episode range", "By task"]);
    expect(
      host.querySelectorAll(".vw-a-scope button:not([role])"),
    ).toHaveLength(1);
  });

  test("Analyze is disabled with the reason on screen until the scope changes", async () => {
    const onChange = mock((request: CrossEpisodeRequest) => void request);
    const { host } = await render(insights(onChange));
    const go = host.querySelector<HTMLButtonElement>(".vw-a-scope__go")!;
    expect(go.disabled).toBe(true);
    expect(go.textContent).toBe("Analyzed");
    const reason = host.querySelector(
      `#${go.getAttribute("aria-describedby")}`,
    );
    expect(reason?.textContent).toContain("Nothing to show for this scope");

    const range = [
      ...host.querySelectorAll<HTMLElement>(".vw-a-scope [role='radio']"),
    ].find((o) => o.textContent === "Episode range")!;
    await click(range);
    const inputs = host.querySelectorAll<HTMLInputElement>(
      ".vw-a-scope input[type='number']",
    );
    expect(inputs).toHaveLength(2);
    // Labelled by visible text, never by placeholder or title only.
    for (const input of inputs)
      expect(input.labels?.[0]?.textContent).toMatch(/From|To/);
    expect(go.disabled).toBe(false);
    expect(go.textContent).toBe("Analyze");
    expect(go.hasAttribute("aria-describedby")).toBe(false);
    await click(go);
    expect(onChange).toHaveBeenCalledWith({
      scope: { kind: "range", from: 0, to: 4 },
      maxEpisodes: null,
    });
  });

  test("the sample size is a labelled ds select", async () => {
    const { host } = await render(insights());
    const select = host.querySelector<HTMLSelectElement>(".vw-a-scope select")!;
    expect(select.labels?.[0]?.textContent).toBe("Sample");
    expect(select.closest(".ds-select")).not.toBeNull();
  });
});

describe("analysis sentences in Chinese", () => {
  test("sampled-episode counts are whole sentences with their numbers", async () => {
    window.localStorage.setItem("levi-language", "zh");
    const { host } = await render(insights(undefined, DATA));
    window.localStorage.removeItem("levi-language");
    const text = host.textContent ?? "";
    expect(text).toContain("已分析范围内 3 / 5 个片段");
    expect(text).toContain("（已采样 3 个片段）");
    // No leftover English fragments, no unfilled placeholders.
    expect(text).not.toContain("episodes sampled");
    expect(text).not.toMatch(/\{[a-z]+\}/);
  });

  test("the dataset statistics label Std Dev is translated", async () => {
    window.localStorage.setItem("levi-language", "zh");
    const info = {
      repoId: "local/demo",
      robot_type: "so101",
      codebase_version: "v3.0",
      total_tasks: 2,
      total_frames: 900,
      total_episodes: 3,
      fps: 30,
      cameras: [{ name: "front", width: 640, height: 480 }],
    } as unknown as DatasetDisplayInfo;
    const stats = {
      shortestEpisodes: [{ lengthSeconds: 8 }],
      longestEpisodes: [{ lengthSeconds: 12 }],
      allEpisodeLengths: [],
      meanEpisodeLength: 10,
      medianEpisodeLength: 10,
      stdEpisodeLength: 1.5,
      episodeLengthHistogram: [{ binLabel: "8–12", count: 3 }],
    } as unknown as EpisodeLengthStats;
    const { host } = await render(
      <LocaleProvider>
        <StatsPanel
          datasetInfo={info}
          episodeLengthStats={stats}
          loading={false}
        />
      </LocaleProvider>,
    );
    window.localStorage.removeItem("levi-language");
    const terms = [...host.querySelectorAll("dt")].map((d) => d.textContent);
    expect(terms).toContain("标准差");
    expect(terms).not.toContain("Std Dev");
    expect(host.textContent).toContain("1 个区间");
  });
});
