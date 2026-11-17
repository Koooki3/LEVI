import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { tick, tickAll, textFields } from "./actions-helpers";
import { dialog, keyButton, openStep, text } from "./autonomy-helpers";
import { callsOf, keyOf, runDoc } from "./autonomy-fixtures";

// L2-STEP5-SIMPLIFY-1117, item 4: the confirm phrase `execute <run>` is never
// typed. The page fills it from the backend's document when the person confirms
// the start of the run (the one click on Confirm), and the key's window has no
// text field.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

async function confirmExecute(id: "execute" | "execute_variant", name: string) {
  const f = await openStep({ run: runDoc(name) });
  await waitFor(() => !keyButton(f.host, id)!.disabled, { label: "the key" });
  await click(keyButton(f.host, id));
  const d = dialog()!;
  expect(textFields(d)).toHaveLength(0);
  await tickAll(d);
  const confirm = Array.from(d.querySelectorAll("button")).find(
    (b) => !b.disabled && b.className.includes("ds-btn--primary"),
  )!;
  await click(confirm);
  await tick(3, 10);
  return f;
}

describe("the phrase of the start of a run", () => {
  test("`execute <run>` is the backend's own phrase for this run, sent by the page, with nothing typed", async () => {
    const doc = runDoc("await_phrase");
    const phrase = keyOf(doc, "execute")!.phrase as string;
    expect(phrase).toBe(`execute ${doc.name}`);
    const f = await confirmExecute("execute", "await_phrase");
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ action: "execute", confirm: phrase });
  });

  test("the long phrase after an unfinished policy switch is the backend's too", async () => {
    const doc = runDoc("await_phrase_variant");
    const phrase = keyOf(doc, "execute_variant")!.phrase as string;
    expect(phrase).toBe(`execute ${doc.name} after policy switch check`);
    const f = await confirmExecute("execute_variant", "await_phrase_variant");
    expect(callsOf(f.state, "key")[0]).toMatchObject({
      action: "execute_variant",
      confirm: phrase,
    });
  });
});
