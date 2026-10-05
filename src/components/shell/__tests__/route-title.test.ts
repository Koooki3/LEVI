import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { navPages } from "../commands";
import { routePageName, routeTitle } from "../route-title";

const zhText = (text: string) => (zh as Record<string, string>)[text] ?? text;

describe("tab titles", () => {
  test("every page names itself and the product", () => {
    expect(routeTitle("/")).toBe("Home · LEVI");
    expect(routeTitle("/guide")).toBe("Guide · LEVI");
    expect(routeTitle("/report")).toBe("Report · LEVI");
    expect(routeTitle("/explore")).toBe("Explore · LEVI");
    expect(routeTitle("/workbench")).toBe("Conversion & review · LEVI");
    expect(routeTitle("/pool")).toBe("Training pool · LEVI");
    expect(routeTitle("/live")).toBe("Live evaluation · LEVI");
    expect(routeTitle("/lerobot/aloha_static_coffee/episode_3")).toBe(
      "Episode viewer · lerobot/aloha_static_coffee · LEVI",
    );
  });

  test("an address that is no page is the product name alone", () => {
    expect(routeTitle("/no/such/page/at/all")).toBe("LEVI");
    expect(routeTitle("/nope")).toBe("LEVI");
    expect(routeTitle("/org/ds/notes")).toBe("LEVI");
    expect(routeTitle("/org/ds")).toBe("Episode viewer · org/ds · LEVI");
  });

  test("the name is in the reader's language", () => {
    expect(routeTitle("/explore", zhText)).toBe("探索数据 · LEVI");
    expect(routeTitle("/pool", zhText)).toBe("训练池 · LEVI");
    expect(routeTitle("/live", zhText)).toBe("实时评测 · LEVI");
    expect(routeTitle("/", zhText)).toBe("首页 · LEVI");
    expect(routeTitle("/org/ds/episode_0", zhText)).toBe(
      "片段查看器 · org/ds · LEVI",
    );
  });

  test("every page of the navigation has a title and a Chinese name", () => {
    for (const page of navPages({ live: true, pool: true })) {
      expect(routePageName(page.href)).toBe(page.label);
      expect((en as Record<string, string>)[page.label]).toBeTruthy();
      expect((zh as Record<string, string>)[page.label]).toBeTruthy();
    }
    for (const name of ["Home", "Episode viewer"]) {
      expect((zh as Record<string, string>)[name]).toBeTruthy();
    }
  });

  test("the root layout carries the product name and mounts the title", () => {
    const layout = readFileSync(
      join(import.meta.dir, "../../../app/layout.tsx"),
      "utf8",
    );
    expect(layout).toMatch(/title: "LEVI"/);
    expect(layout).not.toMatch(/Robot Data Atelier/);
    expect(layout).toContain("<RouteTitle />");
  });
});
