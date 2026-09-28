import { describe, expect, test } from "bun:test";
import {
  anchoredMarkers,
  anchoredSummary,
  eventText,
} from "@/components/anchored-lanes";
import type { AnchoredEpisode, AnchoredEvent } from "@/types/anchored.types";

const event = (
  timestamp: number,
  verdict: AnchoredEvent["verdict"],
  colour: string,
): AnchoredEvent => ({
  frame_index: Math.round(timestamp * 10),
  timestamp,
  answer: { held_before: "yes", plate_colour: colour, stays: "yes" },
  checks: [
    {
      field: "held_before",
      value: "yes",
      result: "supported",
    },
    {
      field: "plate_colour",
      value: colour,
      result: verdict,
    },
  ],
  verdict,
  valid: verdict === "supported",
  frames: [],
});

const record = (events: AnchoredEvent[]): AnchoredEpisode => ({
  schema: "levi.anchored.v1",
  run_id: "review-20260928T1200",
  episode_index: 3,
  status: "waiting_for_review",
  spec: { id: "plates-release-ar2", version: 1 },
  channel: "observation.state.gripper",
  event: "open",
  outcome: "failure",
  basis: { valid_labels: ["pink"], missing_labels: ["white"] },
  events,
});

describe("anchoredMarkers", () => {
  test("nothing without a record", () => {
    expect(anchoredMarkers(null, 10)).toEqual([]);
    expect(anchoredMarkers(undefined, 10)).toEqual([]);
  });

  test("one marker per event at its time, coloured by verdict", () => {
    const markers = anchoredMarkers(
      record([
        event(2.5, "supported", "pink"),
        event(7.5, "unknown", "unclear"),
      ]),
      10,
    );
    expect(markers.map((m) => [m.left, m.verdict])).toEqual([
      [25, "supported"],
      [75, "unknown"],
    ]);
    expect(markers[0].meta).toBe("open · 2.50s · f25 · valid");
    expect(markers[1].meta).toContain("unknown");
  });

  test("an event past the video's end has no place on the track", () => {
    const [marker] = anchoredMarkers(
      record([event(12, "supported", "pink")]),
      10,
    );
    expect(marker.left).toBeNull();
  });
});

describe("wording", () => {
  test("answers, then each condition's reading", () => {
    expect(eventText(event(1, "contradicted", "green"))).toBe(
      "held_before: yes · plate_colour: green · stays: yes\n✓ held_before  ✗ plate_colour",
    );
  });

  test("summary counts valid events and names the labels", () => {
    expect(
      anchoredSummary(
        record([
          event(1, "supported", "pink"),
          event(2, "contradicted", "green"),
        ]),
      ),
    ).toBe("1/2 valid · pink");
  });
});
