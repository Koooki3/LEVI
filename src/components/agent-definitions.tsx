"use client";
import { Plus, Trash2 } from "lucide-react";
import { T, useLocale } from "./levi-locale";
import { Button, Field, Input } from "@/components/ds";
import { Actions, Disclosure } from "./agent-ui";
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
      <div className="ag-section">
        <h3>Subtask definitions</h3>
        {items.map((d, i) => (
          <Disclosure key={i} defaultOpen summary={d.label || t("New subtask")}>
            <div className="ag-form">
              {fields.map(([k, label]) => (
                <Field key={k} label={t(label)}>
                  <Input
                    value={d[k]}
                    onChange={(e) =>
                      save(
                        items.map((row, j) =>
                          j === i ? { ...row, [k]: e.target.value } : row,
                        ),
                      )
                    }
                  />
                </Field>
              ))}
              <Actions>
                <Button
                  size="sm"
                  variant="ghost"
                  className="ag-danger"
                  icon={Trash2}
                  onClick={() => save(items.filter((_, j) => i !== j))}
                >
                  {t("Remove definition")}
                </Button>
              </Actions>
            </div>
          </Disclosure>
        ))}
        <Actions>
          <Button
            size="sm"
            icon={Plus}
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
            {t("Add subtask definition")}
          </Button>
        </Actions>
      </div>
    </T>
  );
}
