import { expect, test } from "bun:test";
import { friendlyError } from "../friendly-error";

const t = (text: string) => `T:${text}`;

test("the gate's 409 becomes a sentence about what to do", () => {
  const out = friendlyError(
    "The robot evaluation is inferring on the GPU (pi05/x is running); the evaluation is inferring, try again shortly",
    t,
  );
  expect(out.startsWith("T:Paused for the robot")).toBe(true);
});

test("other errors pass through", () => {
  expect(friendlyError("HTTP 500", t)).toBe("HTTP 500");
});
