import { setupDom, click, flush, fire, focus, press, render } from "./dom";
import { describe, expect, mock, test } from "bun:test";
import { Pencil } from "lucide-react";
import { Button } from "../Button";
import { IconButton } from "../IconButton";
import { Tooltip } from "../Tooltip";
import { Field, Input, Select, Textarea } from "../Field";
import { Checkbox, Radio, RadioGroup, Switch } from "../Choice";
import { Icon } from "../Icon";

setupDom();

describe("Button", () => {
  test("renders variant and size classes, type=button by default", async () => {
    const { host } = await render(
      <Button variant="primary" size="lg">
        Approve
      </Button>,
    );
    const button = host.querySelector("button")!;
    expect(button.getAttribute("type")).toBe("button");
    expect(button.className).toContain("ds-btn--primary");
    expect(button.className).toContain("ds-btn--lg");
    expect(button.textContent).toBe("Approve");
  });

  test("loading sets aria-busy, keeps the label and swallows clicks", async () => {
    const onClick = mock(() => undefined);
    const { host } = await render(
      <Button loading onClick={onClick}>
        Saving
      </Button>,
    );
    const button = host.querySelector("button")!;
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.textContent).toContain("Saving");
    expect(button.querySelector(".ds-spin")).not.toBeNull();
    await click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  test("click fires when not loading; disabled is native", async () => {
    const onClick = mock(() => undefined);
    const { host } = await render(
      <>
        <Button onClick={onClick}>Go</Button>
        <Button disabled>Off</Button>
      </>,
    );
    const [go, off] = Array.from(host.querySelectorAll("button"));
    await click(go);
    expect(onClick).toHaveBeenCalledTimes(1);
    expect((off as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("IconButton and Tooltip", () => {
  test("has an aria-label and a tooltip with the same words", async () => {
    const { host } = await render(<IconButton icon={Pencil} label="Edit" />);
    const button = host.querySelector("button")!;
    expect(button.getAttribute("aria-label")).toBe("Edit");
    expect(button.hasAttribute("title")).toBe(false);
    const tip = host.querySelector('[role="tooltip"]')!;
    expect(tip.textContent).toBe("Edit");
    // The tooltip repeats the name, so it is not also a description.
    expect(button.hasAttribute("aria-describedby")).toBe(false);
    expect(tip.getAttribute("aria-hidden")).toBe("true");
    expect(button.querySelector("svg")!.getAttribute("aria-hidden")).toBe(
      "true",
    );
  });

  test("tooltip opens on keyboard focus, closes on Escape and when focus leaves", async () => {
    const { host } = await render(
      <Tooltip content="Copies the path">
        <button type="button">Copy</button>
      </Tooltip>,
    );
    const button = host.querySelector("button")!;
    const tip = host.querySelector('[role="tooltip"]') as HTMLElement;
    expect(button.getAttribute("aria-describedby")).toBe(tip.id);
    expect(tip.hidden).toBe(true);
    await focus(button);
    expect(tip.hidden).toBe(false);
    await press(document.body, "Escape");
    expect(tip.hidden).toBe(true);
    await fire(button, new FocusEvent("focusout", { bubbles: true }));
    await fire(button, new FocusEvent("focusin", { bubbles: true }));
    expect(tip.hidden).toBe(false);
    await fire(button, new FocusEvent("focusout", { bubbles: true }));
    expect(tip.hidden).toBe(true);
  });

  test("tooltip opens after a hover delay, not on a click", async () => {
    const { host } = await render(
      <Tooltip content="Later">
        <button type="button">Hover</button>
      </Tooltip>,
    );
    const anchor = host.querySelector(".ds-tooltip-anchor")!;
    const tip = host.querySelector('[role="tooltip"]') as HTMLElement;
    await fire(anchor, new PointerEvent("pointerover", { bubbles: true }));
    await fire(anchor, new PointerEvent("pointerenter", { bubbles: false }));
    expect(tip.hidden).toBe(true);
    await flush(550);
    expect(tip.hidden).toBe(false);
    await fire(anchor, new PointerEvent("pointerout", { bubbles: true }));
    await fire(anchor, new PointerEvent("pointerleave", { bubbles: false }));
    expect(tip.hidden).toBe(true);
    await fire(
      host.querySelector("button"),
      new PointerEvent("pointerdown", { bubbles: true }),
    );
    await focus(host.querySelector("button"));
    expect(tip.hidden).toBe(true);
  });
});

describe("Icon", () => {
  test("decorative by default, labelled when given a label", async () => {
    const { host } = await render(
      <>
        <Icon icon={Pencil} />
        <Icon icon={Pencil} size="lg" label="Edited" />
      </>,
    );
    const [plain, labelled] = Array.from(host.querySelectorAll("svg"));
    expect(plain.getAttribute("aria-hidden")).toBe("true");
    expect(plain.getAttribute("width")).toBe("16");
    expect(plain.getAttribute("stroke-width")).toBe("1.75");
    expect(labelled.getAttribute("role")).toBe("img");
    expect(labelled.getAttribute("aria-label")).toBe("Edited");
    expect(labelled.getAttribute("width")).toBe("24");
    expect(labelled.getAttribute("stroke-width")).toBe("1.5");
  });
});

describe("Field, Input, Textarea, Select", () => {
  test("label, hint and error are wired to the control", async () => {
    const { host } = await render(
      <Field label="Name" hint="Lowercase" error="Taken" required>
        <Input defaultValue="x" />
      </Field>,
    );
    const input = host.querySelector("input")!;
    const label = host.querySelector("label")!;
    expect(label.getAttribute("for")).toBe(input.id);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.required).toBe(true);
    const described = input.getAttribute("aria-describedby")!.split(" ");
    expect(described).toHaveLength(2);
    expect(
      host.querySelector(`#${CSS.escape(described[0])}`)!.textContent,
    ).toBe("Lowercase");
    expect(
      host.querySelector(`#${CSS.escape(described[1])}`)!.textContent,
    ).toBe("Taken");
  });

  test("no error means no aria-invalid; textarea and select get ids", async () => {
    const { host } = await render(
      <>
        <Field label="Notes">
          <Textarea />
        </Field>
        <Field label="Mode">
          <Select defaultValue="b">
            <option value="a">A</option>
            <option value="b">B</option>
          </Select>
        </Field>
      </>,
    );
    const textarea = host.querySelector("textarea")!;
    const select = host.querySelector("select")!;
    expect(textarea.hasAttribute("aria-invalid")).toBe(false);
    expect(textarea.id).not.toBe("");
    expect(select.id).not.toBe("");
    expect(select.value).toBe("b");
    expect(
      host.querySelector(".ds-select__chevron")!.getAttribute("aria-hidden"),
    ).toBe("true");
  });
});

describe("Checkbox, Radio, Switch", () => {
  test("checkbox toggles and shows the mixed state", async () => {
    const onChange = mock(() => undefined);
    const { host } = await render(
      <>
        <Checkbox label="Failures" onChange={onChange} />
        <Checkbox label="Cameras" indeterminate readOnly />
      </>,
    );
    const [plain, mixed] = Array.from(
      host.querySelectorAll<HTMLInputElement>("input"),
    );
    expect(plain.type).toBe("checkbox");
    await click(plain);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(plain.checked).toBe(true);
    expect(mixed.indeterminate).toBe(true);
    // The label wraps the control, so clicking the words toggles it.
    expect(plain.closest("label")!.textContent).toContain("Failures");
  });

  test("radios share a name inside a fieldset with a legend", async () => {
    const { host } = await render(
      <RadioGroup legend="Outcome">
        <Radio name="o" label="Success" defaultChecked />
        <Radio name="o" label="Failure" />
      </RadioGroup>,
    );
    expect(host.querySelector("legend")!.textContent).toBe("Outcome");
    const [a, b] = Array.from(host.querySelectorAll<HTMLInputElement>("input"));
    expect(a.checked).toBe(true);
    await click(b);
    expect(b.checked).toBe(true);
    expect(a.checked).toBe(false);
  });

  test("switch is a checkbox with role=switch", async () => {
    const { host } = await render(<Switch label="Live overlay" />);
    const input = host.querySelector("input")!;
    expect(input.getAttribute("role")).toBe("switch");
    expect(input.type).toBe("checkbox");
    await click(input);
    expect(input.checked).toBe(true);
  });
});
