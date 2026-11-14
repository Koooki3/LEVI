import { describe, expect, mock, test } from "bun:test";
import type * as THREE from "three";
import { createGuardedRenderer } from "../webgl-guard";

const renderer = { render() {} } as unknown as THREE.WebGLRenderer;
const settled = async (promise: Promise<unknown>) => {
  let done = false;
  promise.then(
    () => (done = true),
    () => (done = true),
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  return done;
};

describe("createGuardedRenderer", () => {
  test("builds the renderer from the Canvas defaults plus our own settings", async () => {
    const create = mock((parameters: THREE.WebGLRendererParameters) => {
      void parameters;
      return renderer;
    });
    const onFailure = mock(() => {});
    const gl = createGuardedRenderer({ antialias: true }, onFailure, create);
    expect(
      await gl({ powerPreference: "high-performance", antialias: false }),
    ).toBe(renderer);
    expect(create.mock.calls[0][0]).toEqual({
      powerPreference: "high-performance",
      antialias: true,
    });
    expect(onFailure).not.toHaveBeenCalled();
  });

  test("a renderer that cannot be made is reported once, never thrown, and not tried again", async () => {
    const boom = new Error("Error creating WebGL context.");
    const create = mock(() => {
      throw boom;
    });
    const onFailure = mock((error: unknown) => void error);
    const gl = createGuardedRenderer({}, onFailure, create);
    // The Canvas asks again after every render.
    const answers = [gl({}), gl({}), gl({})];
    expect(onFailure.mock.calls).toEqual([[boom]]);
    expect(create).toHaveBeenCalledTimes(1);
    // Nothing is thrown and nothing resolves: the Canvas stops where it is.
    for (const answer of answers) expect(await settled(answer)).toBe(false);
  });

  test("a new guard (Retry mounts a new scene) tries again and can succeed", async () => {
    let webgl = false;
    const create = () => {
      if (!webgl) throw new Error("Error creating WebGL context.");
      return renderer;
    };
    const failed = mock(() => {});
    void createGuardedRenderer({}, failed, create)({});
    expect(failed).toHaveBeenCalledTimes(1);
    webgl = true;
    const again = mock(() => {});
    expect(await createGuardedRenderer({}, again, create)({})).toBe(renderer);
    expect(again).not.toHaveBeenCalled();
  });
});
