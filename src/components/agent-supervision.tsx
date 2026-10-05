"use client";

import { useEffect, useState } from "react";
import { T, useLocale } from "./levi-locale";
import { Badge, Card, Field, Radio, RadioGroup, Select } from "@/components/ds";
import { Note } from "@/components/pages-ui/feedback";
import { Hint } from "./agent-ui";

export function TeacherChoice({
  dataset,
  mode,
  teacher,
  change,
}: {
  dataset: string;
  mode: string;
  teacher: string;
  change: (mode: "none" | "shadow" | "supervised", teacher: string) => void;
}) {
  const { t } = useLocale();
  const [grants, setGrants] = useState<
    Array<{
      id: string;
      client: string;
      enabled: boolean;
      datasets: string[];
      run_id: string | null;
      expires: number | null;
    }>
  >([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    fetch("/api/levi/agent/v1/grants")
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load teacher connections");
        const items = await response.json();
        if (!cancelled) setGrants(items);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load teacher connections");
      });
    return () => {
      cancelled = true;
    };
  }, [dataset]);
  const modes: Array<["none" | "shadow" | "supervised", string]> = [
    ["none", "No teacher gate"],
    ["shadow", "Shadow comparison"],
    ["supervised", "Supervised annotation"],
  ];
  return (
    <T>
      <div className="ag-stack">
        <RadioGroup legend={t("External teacher supervision")}>
          {modes.map(([value, label]) => (
            <Radio
              key={value}
              name="agent-supervision-mode"
              label={t(label)}
              checked={mode === value}
              onChange={() => change(value, teacher)}
            />
          ))}
        </RadioGroup>
        {mode !== "none" && (
          <>
            <Field label={t("Teacher connection")} required>
              <Select
                value={teacher}
                required
                onChange={(e) =>
                  change(mode as "shadow" | "supervised", e.target.value)
                }
              >
                <option value="">{t("Choose a teacher connection")}</option>
                {grants
                  .filter(
                    (g) =>
                      g.enabled &&
                      !g.run_id &&
                      g.datasets.includes(dataset) &&
                      (!g.expires || g.expires * 1000 > Date.now()),
                  )
                  .map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.client} · {g.id}
                    </option>
                  ))}
              </Select>
            </Field>
            {error && (
              <Note tone="warning" role="alert">
                {t(error)}
              </Note>
            )}
            <Hint>
              Create a scoped Codex or Claude connection first. Both modes pause
              each annotation phase for teacher feedback; final human review is
              still required. No automatic promotion or weight training.
            </Hint>
            <Hint>
              Evidence sharing consent also covers the teacher. The teacher must
              use supervision.pending and supervision.feedback through its LEVI
              tools.
            </Hint>
          </>
        )}
      </div>
    </T>
  );
}

type Teaching = {
  id: string;
  revision: number;
  status: string;
  phase: string;
  episode: number;
  learner_output: { summary: string };
  feedback?: { note: string; decision: string };
};
export function TeachingStatus({
  runId,
  status,
}: {
  runId: string;
  status: string;
}) {
  const [items, setItems] = useState<Teaching[]>([]);
  const [error, setError] = useState("");
  const { t } = useLocale();
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      fetch("/api/levi/agent/v1/tools", {
        signal: controller.signal,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: "supervision.pending",
          arguments: { run_id: runId, include_decided: true },
        }),
      })
        .then(async (response) => {
          const data = await response.json();
          if (!response.ok)
            throw new Error(data.detail || "Could not load teacher feedback");
          if (!cancelled) {
            setItems(data.items);
            setError("");
          }
        })
        .catch((err) => {
          if (!cancelled) setError(String(err));
        });
    };
    const stream = new EventSource(
      `/api/levi/agent/v1/runs/${encodeURIComponent(runId)}/stream`,
    );
    stream.onopen = load;
    stream.onmessage = (message) => {
      try {
        if (JSON.parse(message.data).type?.startsWith("supervision.")) {
          clearTimeout(timer);
          timer = setTimeout(load, 100);
        }
      } catch {
        setError("Could not load teacher feedback");
      }
    };
    load();
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
      stream.close();
    };
  }, [runId, status]);
  return (
    <T>
      <Card padding="compact" title={t("Teacher feedback")}>
        {error && (
          <Note tone="warning" role="alert">
            {t(error)}
          </Note>
        )}
        {items.map((item) => (
          <div key={item.id} className="ag-stack">
            <p>
              <strong>
                {t("Episode")} {item.episode} · {item.phase}
              </strong>{" "}
              <Badge tone="neutral">{t(item.status)}</Badge>
            </p>
            <p>{item.learner_output.summary}</p>
            {item.feedback && (
              <p>
                {t(item.feedback.decision)} · {item.feedback.note}
              </p>
            )}
          </div>
        ))}
        <Hint>
          After feedback is accepted, resume the pilot or execution. Completed
          model phases use cached results; teacher feedback does not approve
          publication.
        </Hint>
      </Card>
    </T>
  );
}
