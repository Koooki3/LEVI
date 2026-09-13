import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

describe("locale catalogs", () => {
  test("keep English and Chinese keys in sync", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort());
    expect(Object.values(en).every((value) => typeof value === "string")).toBe(
      true,
    );
    expect(Object.values(zh).every((value) => typeof value === "string")).toBe(
      true,
    );
    expect(
      Object.keys(en).every(
        (key) => key === key.trim() && !/[\n\t]| {2,}/.test(key),
      ),
    ).toBe(true);
  });

  test("retain the professional landing-page translations", () => {
    expect(zh["Every motion."]).toBe("每一次动作，");
    expect(zh["A clearer story."]).toBe("都清晰可见。");
    expect(zh["Search or enter a Hugging Face dataset ID"]).toBe(
      "搜索或输入 Hugging Face 数据集 ID",
    );
    expect(en["Every motion."]).toBe("Every motion.");
    expect(
      zh["All motors are inactive or discrete — no motors to evaluate."],
    ).toBe("所有执行器维度均处于非活动或离散状态，暂无可评估的连续动作。");
    expect(zh["No episode frames available."]).toBe("没有可用的片段帧。");
  });
});
