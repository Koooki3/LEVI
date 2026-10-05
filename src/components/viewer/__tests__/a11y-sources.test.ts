import { describe, expect, test } from "bun:test";
import ts from "typescript";
import { readFileSync } from "fs";
import { join } from "path";

const src = join(import.meta.dir, "../../..");

function sources(): Array<{ file: string; sf: ts.SourceFile }> {
  const out: Array<{ file: string; sf: ts.SourceFile }> = [];
  for (const file of new Bun.Glob("**/*.tsx").scanSync(src)) {
    if (file.includes("__tests__") || file.startsWith("app/design")) continue;
    out.push({
      file,
      sf: ts.createSourceFile(
        file,
        readFileSync(join(src, file), "utf8"),
        ts.ScriptTarget.Latest,
        true,
        ts.ScriptKind.TSX,
      ),
    });
  }
  return out;
}

const CONTROLS = new Set(["input", "select", "textarea"]);
const tagOf = (node: ts.Node) =>
  ts.isJsxElement(node)
    ? node.openingElement.tagName.getText()
    : ts.isJsxSelfClosingElement(node)
      ? node.tagName.getText()
      : null;
const attrsOf = (node: ts.JsxElement | ts.JsxSelfClosingElement) =>
  ts.isJsxElement(node)
    ? node.openingElement.attributes.properties
    : node.attributes.properties;
const has = (
  node: ts.JsxElement | ts.JsxSelfClosingElement,
  name: string,
): boolean =>
  attrsOf(node).some((a) => ts.isJsxAttribute(a) && a.name.getText() === name);

function visitAll(
  sf: ts.SourceFile,
  fn: (node: ts.JsxElement | ts.JsxSelfClosingElement) => void,
) {
  const walk = (node: ts.Node) => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) fn(node);
    ts.forEachChild(node, walk);
  };
  walk(sf);
}

describe("form labels and chart images", () => {
  test("a <label> is tied to its control (htmlFor, or it wraps one)", () => {
    const loose: string[] = [];
    for (const { file, sf } of sources())
      visitAll(sf, (node) => {
        if (tagOf(node) !== "label" || !ts.isJsxElement(node)) return;
        if (has(node, "htmlFor")) return;
        let wraps = false;
        const walk = (n: ts.Node) => {
          const tag = tagOf(n);
          if (tag && (CONTROLS.has(tag) || (/^[A-Z]/.test(tag) && tag !== "T")))
            wraps = true;
          ts.forEachChild(n, walk);
        };
        node.children.forEach(walk);
        if (!wraps) {
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
          loose.push(`${file}:${line + 1}`);
        }
      });
    expect(loose).toEqual([]);
  });

  test("a histogram or heatmap svg is an image with a name", () => {
    const bare: string[] = [];
    for (const { file, sf } of sources())
      visitAll(sf, (node) => {
        if (tagOf(node) !== "svg") return;
        const cls = attrsOf(node)
          .map((a) => (ts.isJsxAttribute(a) ? a.getText() : ""))
          .join(" ");
        if (!/vw-a-histogram/.test(cls)) return;
        if (!(has(node, "role") && has(node, "aria-label"))) {
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
          bare.push(`${file}:${line + 1}`);
        }
      });
    expect(bare).toEqual([]);
  });

  test("typed-in labels and selects of the annotation popups have a name", () => {
    const code = (file: string) => readFileSync(join(src, file), "utf8");
    expect(code("components/annotations-timeline.tsx")).toMatch(
      /<input\s+type="text"\s+aria-label=\{t\("label \(e\.g\. grasp the sponge\)"\)\}/,
    );
    expect(code("components/video-overlay-canvas.tsx")).toMatch(
      /aria-label=\{t\("Question kind"\)\}/,
    );
    expect(code("components/object-annotation-panel.tsx")).toMatch(
      /aria-label=\{t\("Preset name"\)\}/,
    );
  });
});
