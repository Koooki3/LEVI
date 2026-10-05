import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterAll, beforeAll, describe, expect, mock, test } from "bun:test";
import { SimpleVideosPlayer } from "@/components/simple-videos-player";
import { TimeProvider } from "@/context/time-context";
import { AnnotationsProvider } from "@/context/annotations-context";

setupDom();

const realFetch = globalThis.fetch;
beforeAll(() => {
  // No annotation backend: the player falls back to no object overlays.
  globalThis.fetch = mock(() =>
    Promise.reject(new Error("offline")),
  ) as unknown as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

describe("video player: enlarged camera", () => {
  test("Enlarge shows it over the page and Escape restores it", async () => {
    const { host } = await render(
      <TimeProvider duration={10}>
        <AnnotationsProvider>
          <SimpleVideosPlayer
            videosInfo={[
              { filename: "cam_a", url: "/a.mp4" },
              { filename: "cam_b", url: "/b.mp4" },
            ]}
          />
        </AnnotationsProvider>
      </TimeProvider>,
    );
    expect(host.querySelector(".vw-video-enlarged")).toBeNull();
    await click(host.querySelector('[aria-label="Enlarge"]'));
    expect(host.querySelector(".vw-video-enlarged")).not.toBeNull();
    expect(host.querySelector('[aria-label="Minimize"]')).not.toBeNull();
    await press(document.body, "Escape");
    expect(host.querySelector(".vw-video-enlarged")).toBeNull();
  });
});
