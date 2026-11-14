// One Chinese word per thing on the /automatic pages: 评测 (not 测评), the
// campaign is 评测计划 (not 活动 or 多模型评测), a dry run is 演练运行, a
// segment is 时间片段, a task instruction is 指令 and a shell command is 命令.
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const zhText = zh as Record<string, string>;
const enText = en as Record<string, string>;
const keys = Object.keys(enText).filter((key) => key.startsWith("automatic."));
const offenders = (pattern: RegExp) =>
  keys.filter((key) => pattern.test(zhText[key] ?? "")).sort();

describe("the Chinese wording of the /automatic keys", () => {
  test("评测, never 测评", () => {
    expect(offenders(/测评/)).toEqual([]);
  });
  test("the campaign is 评测计划, never 活动 or 多模型评测", () => {
    expect(offenders(/活动|多模型评测/)).toEqual([]);
  });
  test("a dry run is 演练运行, never 试运行", () => {
    expect(offenders(/试运行/)).toEqual([]);
  });
  test("a segment is 时间片段, never a bare 段 (the paragraph 一段话 aside)", () => {
    const bare = keys.filter((key) => {
      const text = (zhText[key] ?? "")
        .replace(/时间片段|片段|一段话|推理段/g, "")
        .replace(/时间段/g, "");
      return text.includes("段") && /segment/i.test(enText[key]);
    });
    expect(bare.sort()).toEqual([]);
  });
  test("a shell command is 命令, not 指令", () => {
    const shell = keys.filter(
      (key) =>
        /\bcommand\b/i.test(enText[key]) &&
        !/instruction/i.test(enText[key]) &&
        /指令/.test(zhText[key] ?? ""),
    );
    expect(shell.sort()).toEqual([]);
  });
});
