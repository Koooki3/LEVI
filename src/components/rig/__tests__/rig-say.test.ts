import { describe, expect, test } from "bun:test";
import { sayText } from "../rig-say";

const catalogue: Record<string, string> = {
  "Port {port}": "端口 {port}",
  "{label}: free": "{label}：空闲",
  "s ago": " 秒前",
  "min ago": "分钟前",
  hello: "你好",
};
const zh = (text: string) => catalogue[text] ?? text;
const en = (text: string) => text;

describe("sayText", () => {
  test("a key is looked up and its placeholders filled", () => {
    expect(sayText({ key: "Port {port}", params: { port: 5000 } }, zh)).toBe(
      "端口 5000",
    );
    expect(sayText({ key: "Port {port}", params: { port: 5000 } }, en)).toBe(
      "Port 5000",
    );
    expect(
      sayText({ key: "{label}: free", params: { label: "levi" } }, zh),
    ).toBe("levi：空闲");
  });

  test("a placeholder with no value is left as it is, and a key with none is plain", () => {
    expect(sayText({ key: "Port {port}", params: {} }, en)).toBe("Port {port}");
    expect(sayText({ key: "hello" }, zh)).toBe("你好");
    expect(sayText({ key: "not in the catalogue" }, zh)).toBe(
      "not in the catalogue",
    );
  });

  test("text from a server is passed on as written, braces and all", () => {
    expect(sayText({ raw: "{port} stays" }, zh)).toBe("{port} stays");
  });

  test("an age is worded with the locale's units", () => {
    expect(sayText({ ago: 12 }, en)).toBe("12 s ago");
    expect(sayText({ ago: 12 }, zh)).toBe("12秒前");
    expect(sayText({ ago: 600 }, zh)).toBe("10分钟前");
  });

  test("a placeholder can hold another piece: an age is worded, not printed as a number", () => {
    const sayings: Record<string, string> = {
      "Updated {age}.": "更新于{age}。",
      unknown: "未知",
    };
    const inZh = (text: string) => sayings[text] ?? catalogue[text] ?? text;
    expect(
      sayText({ key: "Updated {age}.", params: { age: { ago: 12 } } }, en),
    ).toBe("Updated 12 s ago.");
    expect(
      sayText({ key: "Updated {age}.", params: { age: { ago: 12 } } }, inZh),
    ).toBe("更新于12秒前。");
    expect(
      sayText(
        { key: "Updated {age}.", params: { age: { key: "unknown" } } },
        inZh,
      ),
    ).toBe("更新于未知。");
    // No age at all is a dash, as everywhere else on the page.
    expect(
      sayText({ key: "Updated {age}.", params: { age: { ago: null } } }, en),
    ).toBe("Updated —.");
  });

  test("pieces are joined with a dot, nested ones too", () => {
    expect(
      sayText(
        {
          join: [{ key: "hello" }, { join: [{ raw: "pid 7" }, { ago: 3 }] }],
        },
        zh,
      ),
    ).toBe("你好 · pid 7 · 3秒前");
    expect(sayText({ join: [] }, zh)).toBe("");
  });
});
