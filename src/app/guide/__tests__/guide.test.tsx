import { fire, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { demoVideoUrl } from "@/lib/demo-datasets";
import { DemoThumb } from "../demo-thumb";

setupDom();

describe("the guide's example thumbnails", () => {
  test("a quiet placeholder with an icon until a frame has loaded", async () => {
    const { host } = await render(
      <DemoThumb id="lerobot/aloha_static_coffee" />,
    );
    const media = host.querySelector(".levi-guide-demos__media")!;
    expect(media.getAttribute("data-state")).toBe("loading");
    expect(
      media.querySelector(".levi-guide-demos__placeholder svg"),
    ).toBeTruthy();
    await fire(media.querySelector("video"), new Event("loadeddata"));
    expect(media.getAttribute("data-state")).toBe("ready");
    expect(media.querySelector(".levi-guide-demos__placeholder")).toBeNull();
  });

  test("when the remote file cannot be reached it stays a placeholder, without a video", async () => {
    const { host } = await render(
      <DemoThumb id="lerobot/svla_so101_pickplace" />,
    );
    await fire(host.querySelector("video"), new Event("error"));
    const media = host.querySelector(".levi-guide-demos__media")!;
    expect(media.getAttribute("data-state")).toBe("failed");
    expect(media.querySelector("video")).toBeNull();
    expect(media.querySelector(".levi-guide-demos__placeholder")).toBeTruthy();
  });

  test("a video that loaded or failed before hydration is not left on the placeholder", async () => {
    const proto = HTMLVideoElement.prototype;
    const ready = Object.getOwnPropertyDescriptor(proto, "readyState");
    Object.defineProperty(proto, "readyState", {
      configurable: true,
      get: () => 4,
    });
    try {
      const { host } = await render(
        <DemoThumb id="lerobot/aloha_static_coffee" />,
      );
      expect(
        host
          .querySelector(".levi-guide-demos__media")!
          .getAttribute("data-state"),
      ).toBe("ready");
    } finally {
      if (ready) Object.defineProperty(proto, "readyState", ready);
      else delete (proto as unknown as Record<string, unknown>).readyState;
    }
    const errorDescriptor = Object.getOwnPropertyDescriptor(proto, "error");
    Object.defineProperty(proto, "error", {
      configurable: true,
      get: () => ({ code: 4 }),
    });
    try {
      const { host } = await render(
        <DemoThumb id="lerobot/svla_so101_pickplace" />,
      );
      const media = host.querySelector(".levi-guide-demos__media")!;
      expect(media.getAttribute("data-state")).toBe("failed");
      expect(media.querySelector("video")).toBeNull();
    } finally {
      if (errorDescriptor)
        Object.defineProperty(proto, "error", errorDescriptor);
      else delete (proto as unknown as Record<string, unknown>).error;
    }
  });

  test("the thumbnail asks for the v3.0 file of the dataset, the one Explore uses", async () => {
    const { host } = await render(
      <DemoThumb id="lerobot/aloha_static_coffee" />,
    );
    const src = host.querySelector("video")!.getAttribute("src")!;
    expect(src).toBe(
      "/api/proxy/datasets/lerobot/aloha_static_coffee/resolve/main/videos/observation.images.cam_high/chunk-000/file-000.mp4#t=0.1",
    );
    // Not the v2.x per-episode path that 404s on these datasets.
    expect(src).not.toContain("episode_000000");
    const unknown = await render(<DemoThumb id="someone/else" />);
    expect(unknown.host.querySelector("video")).toBeNull();
    expect(
      unknown.host
        .querySelector(".levi-guide-demos__media")!
        .getAttribute("data-state"),
    ).toBe("failed");
  });

  test("Explore and the guide share one definition of the demo videos", () => {
    expect(demoVideoUrl("lerobot/svla_so101_pickplace")).toContain(
      "videos/observation.images.up/chunk-000/file-000.mp4",
    );
    expect(demoVideoUrl("nobody/nothing")).toBeNull();
    const explore = readFileSync(
      join(import.meta.dir, "../../explore/page.tsx"),
      "utf8",
    );
    expect(explore).toContain("demoVideoUrl");
    expect(explore).not.toContain("chunk-000/file-000");
  });

  test("the diagnose section names the merged Analysis tab", () => {
    const page = readFileSync(join(import.meta.dir, "../page.tsx"), "utf8");
    expect(page).toContain('"04 / Analyse"');
    expect(page).not.toContain("04 / Diagnose");
    const body = /"04 \/ Analyse",\s*"([^"]+)"/.exec(page)![1];
    expect(body).toContain("The Analysis tab holds three views");
    expect((en as Record<string, string>)[body]).toBe(body);
    expect((zh as Record<string, string>)[body]).toContain("「分析」");
  });
});

describe("the guide's first steps", () => {
  test("open with the same three steps as the home page, linked, in order", async () => {
    const { default: Guide } = await import("../page");
    const { host } = await render(<Guide />);
    const items = Array.from(
      host.querySelectorAll<HTMLElement>(".levi-guide-first > li"),
    );
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toContain("Open a public dataset");
    expect(items[0].querySelector("a")!.getAttribute("href")).toBe("/explore");
    expect(items[1].querySelector("a")!.getAttribute("href")).toBe(
      "/workbench",
    );
    // Connecting a model has no page of its own: a button, not a link.
    expect(items[2].querySelector("button")).toBeTruthy();
    expect(items[2].querySelector("a")).toBeNull();
  });
});
