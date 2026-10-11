import { expect, test } from "bun:test";
import { sessionStatisticsMatch } from "../session-stats-source";
import {
  statisticsFixture,
  statisticsSourceFixture,
} from "./statistics-fixture";
import { resultFixture } from "./result-fixture";
const scope = {
  dataset: "model__task",
  session: "run-1",
  includeExcluded: false,
};
const identity = resultFixture().identity;

test("matches the full current source and preserves a genuinely absent canonical session id", () => {
  expect(sessionStatisticsMatch(statisticsFixture(), scope, identity)).toBe(
    true,
  );
  expect(
    sessionStatisticsMatch(
      statisticsFixture({
        source_identities: [statisticsSourceFixture({ session_id: null })],
      }),
      scope,
      { ...identity, session_id: null },
    ),
  ).toBe(true);
  expect(sessionStatisticsMatch(statisticsFixture(), scope, null)).toBe(false);
});
for (const key of [
  "root",
  "group",
  "task",
  "run_id",
  "session_id",
  "datasets",
] as const) {
  test(`rejects missing actual source ${key} without inferring it from the index`, () => {
    const source: Record<string, unknown> = { ...statisticsSourceFixture() };
    delete source[key];
    expect(
      sessionStatisticsMatch(
        { ...statisticsFixture(), source_identities: [source] },
        scope,
        identity,
      ),
    ).toBe(false);
  });
  test(`rejects a wrongly typed actual source ${key}`, () => {
    const source = { ...statisticsSourceFixture(), [key]: 73 };
    expect(
      sessionStatisticsMatch(
        { ...statisticsFixture(), source_identities: [source] },
        scope,
        identity,
      ),
    ).toBe(false);
  });
}
test("accepts only server-confirmed dataset aliases under the same full canonical identity", () => {
  const datasets = ["model__task", "native-dataset"];
  const payload = statisticsFixture({
    scope: { dataset: "native-dataset", session: "run-1" },
    datasets,
    source_identities: [statisticsSourceFixture({ datasets })],
  });
  expect(
    sessionStatisticsMatch(
      payload,
      { ...scope, dataset: "native-dataset" },
      { ...identity, datasets },
    ),
  ).toBe(true);
  expect(
    sessionStatisticsMatch(
      payload,
      { ...scope, dataset: "native-dataset" },
      identity,
    ),
  ).toBe(false);
});
test("rejects empty, unknown, malformed and multiple actual source identities", () => {
  for (const source_identities of [
    null,
    {},
    [],
    [null],
    ["unknown"],
    [statisticsSourceFixture(), statisticsSourceFixture()],
  ])
    expect(
      sessionStatisticsMatch(
        { ...statisticsFixture(), source_identities },
        scope,
        identity,
      ),
    ).toBe(false);
});
