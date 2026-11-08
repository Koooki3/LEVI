import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterAll, beforeAll, describe, expect, mock, test } from "bun:test";
import { SimpleVideosPlayer } from "@/components/simple-videos-player";
import { TimeProvider } from "@/context/time-context";
import { AnnotationsProvider } from "@/context/annotations-context";
import type { VideoInfo } from "@/types";

setupDom();

const realFetch = globalThis.fetch;
beforeAll(() => {
  globalThis.fetch = mock(() =>
    Promise.reject(new Error("offline")),
  ) as unknown as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

type Primary = { el: HTMLVideoElement; segmentStart: number } | null;
const player = (
  videosInfo: VideoInfo[],
  onPrimaryVideo: (p: Primary) => void,
) => (
  <TimeProvider duration={10}>
    <AnnotationsProvider>
      <SimpleVideosPlayer
        videosInfo={videosInfo}
        onPrimaryVideo={onPrimaryVideo}
      />
    </AnnotationsProvider>
  </TimeProvider>
);
const two: VideoInfo[] = [
  { filename: "cam_a", url: "/a.mp4" },
  { filename: "cam_b", url: "/b.mp4" },
];

describe("SimpleVideosPlayer: onPrimaryVideo (what the 3D replay follows)", () => {
  test("announces the first camera's <video>, not the second", async () => {
    const seen: Primary[] = [];
    const { host } = await render(player(two, (p) => seen.push(p)));
    const videos = host.querySelectorAll("video");
    expect(videos.length).toBe(2);
    expect(seen.at(-1)?.el).toBe(videos[0] as HTMLVideoElement);
    expect(seen.at(-1)?.segmentStart).toBe(0);
  });

  test("a segmented video reports its segment start; a plain one reports 0", async () => {
    const seen: Primary[] = [];
    await render(
      player(
        [
          {
            filename: "cam_a",
            url: "/a.mp4",
            isSegmented: true,
            segmentStart: 12.5,
            segmentEnd: 20,
          },
        ],
        (p) => seen.push(p),
      ),
    );
    expect(seen.at(-1)?.segmentStart).toBe(12.5);
    const plain: Primary[] = [];
    await render(
      player([{ filename: "cam_a", url: "/a.mp4", segmentStart: 99 }], (p) =>
        plain.push(p),
      ),
    );
    // not segmented: the timeupdate handler subtracts nothing, so neither does the replay
    expect(plain.at(-1)?.segmentStart).toBe(0);
  });

  test("hiding the first camera makes the second one primary", async () => {
    const seen: Primary[] = [];
    const { host } = await render(player(two, (p) => seen.push(p)));
    const videos = host.querySelectorAll("video");
    await click(host.querySelector('[aria-label="Hide Video"]'));
    expect(seen.at(-1)?.el).toBe(videos[1] as HTMLVideoElement);
  });

  test("unmounting announces null", async () => {
    const seen: Primary[] = [];
    const rendered = await render(player(two, (p) => seen.push(p)));
    expect(seen.at(-1)).not.toBeNull();
    await rendered.rerender(<div />);
    expect(seen.at(-1)).toBeNull();
  });

  test("an inline callback is not announced again on every render", async () => {
    let calls = 0;
    const rendered = await render(player(two, () => calls++));
    const first = calls;
    await rendered.rerender(player(two, () => calls++));
    expect(calls).toBe(first);
  });
});
