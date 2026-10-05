import { fire, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
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
