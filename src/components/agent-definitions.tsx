"use client";
import { T, useLocale } from "./levi-locale";
type Definition = {
  id: string;
  label: string;
  definition: string;
  starts_when: string;
  ends_when: string;
  success_when: string;
  confusions: string;
};
const fields: [keyof Definition, string][] = [
  ["id", "Subtask ID"],
  ["label", "Label"],
  ["definition", "Semantic definition"],
  ["starts_when", "Observable start"],
  ["ends_when", "Observable end"],
  ["success_when", "Observable success"],
  ["confusions", "Ambiguities and confusions"],
];
export default function AgentDefinitions({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useLocale();
  const items = JSON.parse(value) as Definition[];
  function save(next: Definition[]) {
    onChange(JSON.stringify(next));
  }
  return (
    <T>
      <div className="levi-review-queue">
        <h3>Subtask definitions</h3>
        {items.map((d, i) => (
          <details key={i} open>
            <summary>{d.label || t("New subtask")}</summary>
            {fields.map(([k, label]) => (
              <label key={k}>
                {t(label)}
                <input
                  value={d[k]}
                  onChange={(e) =>
                    save(
                      items.map((row, j) =>
                        j === i ? { ...row, [k]: e.target.value } : row,
                      ),
                    )
                  }
                />
              </label>
            ))}
            <button
              type="button"
              onClick={() => save(items.filter((_, j) => i !== j))}
            >
              Remove definition
            </button>
          </details>
        ))}
        <button
          type="button"
          onClick={() =>
            save([
              ...items,
              {
                id: `subtask_${items.length + 1}`,
                label: "",
                definition: "",
                starts_when: "",
                ends_when: "",
                success_when: "",
                confusions: "",
              },
            ])
          }
        >
          Add subtask definition
        </button>
      </div>
    </T>
  );
}
