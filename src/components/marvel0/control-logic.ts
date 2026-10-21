// The control rate of the Client command card (`--control-hz`): what the site
// view says about it, and the numbers a rate changes, worked out the way the
// backend works them out (levi/marvel0/command.py, `control_summary`) so the
// form shows them before a command is made. A test holds the two to the same
// answers (marvel0-command-hz.sample.json is written by the backend).
//
// Pure: no React and no fetching. A value of the wrong type is no answer
// (null): the page then shows no rate field, and the server's default of
// 10 Hz applies.
import { list, num, rec } from "./marvel0-json";

/** What `GET /api/levi/marvel0/site` says under `control`. */
export interface ControlView {
  /** The rates the form offers, ascending. */
  options: number[];
  /** The rate the client runs at when none is written. */
  default: number;
  /** The rate the policy was trained at and the site's per-step caps are for. */
  referenceHz: number;
  /** Actions in one policy chunk. */
  actionHorizon: number;
  /** The inference latency of an RTC request in beats at the reference rate. */
  latencyBeats: number;
  /** An episode shorter than this, above the reference rate, is warned about. */
  shortEpisodeS: number;
  /** The site's per-step caps, written for the reference rate. */
  stepCaps: { m: number; rad: number };
  /** The 99th percentile of the training data's steps, in the same units. */
  trainStepP99: { m: number; rad: number };
}

const positive = (value: unknown): number | null => {
  const n = num(value);
  return n !== null && n > 0 ? n : null;
};

/** The `control` part of the site answer, or null when it is missing or does
 * not hold the numbers the form needs. */
export function readControl(data: unknown): ControlView | null {
  const body = rec(data);
  if (!body) return null;
  const options = list(body.options).map(positive);
  const caps = rec(body.step_caps);
  const p99 = rec(body.train_step_p99);
  const fields = {
    def: positive(body.default),
    referenceHz: positive(body.reference_hz),
    actionHorizon: positive(body.action_horizon),
    latencyBeats: positive(body.latency_beats),
    shortEpisodeS: positive(body.short_episode_s),
    capM: positive(caps?.m),
    capRad: positive(caps?.rad),
    p99M: positive(p99?.m),
    p99Rad: positive(p99?.rad),
  };
  if (
    options.length === 0 ||
    options.some((option) => option === null) ||
    Object.values(fields).some((value) => value === null)
  )
    return null;
  const rates = options as number[];
  if (!rates.includes(fields.def as number)) return null;
  return {
    options: rates,
    default: fields.def as number,
    referenceHz: fields.referenceHz as number,
    actionHorizon: fields.actionHorizon as number,
    latencyBeats: fields.latencyBeats as number,
    shortEpisodeS: fields.shortEpisodeS as number,
    stepCaps: { m: fields.capM as number, rad: fields.capRad as number },
    trainStepP99: { m: fields.p99M as number, rad: fields.p99Rad as number },
  };
}

/** Six decimals, as the backend rounds. */
const round6 = (value: number): number => Math.round(value * 1e6) / 1e6;

/** A site per-step cap as the command writes it for `hz`: the site's value up
 * to the reference rate, divided by the rate ratio above it, in the ten
 * significant digits the backend prints. */
export function scaledCap(
  control: ControlView,
  site: number,
  hz: number,
): number {
  if (hz <= control.referenceHz) return site;
  return Number(((site * control.referenceHz) / hz).toPrecision(10));
}

/** The inference latency in whole beats of `hz` (rounded up). */
export function latencyBeats(control: ControlView, hz: number): number {
  return Math.ceil((control.latencyBeats * hz) / control.referenceHz);
}

/** The RTC execute horizon an empty field means at `hz`: the site's default up
 * to the reference rate; above it the largest one that leaves the latency's
 * beats free in a chunk, never above the site's default and never below 1. */
export function rtcHorizonDefault(
  control: ControlView,
  siteDefault: number,
  hz: number,
): number {
  if (hz <= control.referenceHz) return siteDefault;
  return Math.min(
    siteDefault,
    Math.max(1, control.actionHorizon - latencyBeats(control, hz)),
  );
}

export interface ControlFacts {
  hz: number;
  /** The step limit and the horizon the numbers below are worked out for. */
  steps: number;
  horizon: number;
  /** Not the reference rate: the page words the difference. */
  changed: boolean;
  periodMs: number;
  episodeS: number;
  stepCapM: number;
  stepCapRad: number;
  speedCapMS: number;
  speedCapRadS: number;
  chunkS: number;
  horizonS: number;
  latencyBeats: number;
  /** Null when RTC is off. `margin` below zero means the client may wait. */
  rtc: { horizon: number; reached: number; margin: number } | null;
}

/** The numbers `hz` changes for a form that asks for `steps` steps and an
 * execute (or open-loop) `horizon` of actions: the same ones the backend's
 * `control_summary` gives. */
export function controlFacts(
  control: ControlView,
  input: { hz: number; steps: number; horizon: number; rtc: boolean },
): ControlFacts {
  const { hz, steps, horizon, rtc } = input;
  const capM = scaledCap(control, control.stepCaps.m, hz);
  const capRad = scaledCap(control, control.stepCaps.rad, hz);
  const beats = latencyBeats(control, hz);
  const reached = horizon + beats;
  return {
    hz,
    steps,
    horizon,
    changed: hz !== control.referenceHz,
    periodMs: round6(1000 / hz),
    episodeS: round6(steps / hz),
    stepCapM: capM,
    stepCapRad: capRad,
    speedCapMS: round6(capM * hz),
    speedCapRadS: round6(capRad * hz),
    chunkS: round6(control.actionHorizon / hz),
    horizonS: round6(horizon / hz),
    latencyBeats: beats,
    rtc: rtc
      ? { horizon, reached, margin: control.actionHorizon - reached }
      : null,
  };
}
