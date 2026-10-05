"use client";
import { useColon } from "@/components/pages-ui/messages";
import { useCallback, useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { CircleCheck, Eraser, Trash2 } from "lucide-react";
import { Button, Tooltip } from "@/components/ds";
import { EmptyLine, RequestProblem } from "@/components/pages-ui/feedback";
import { ConfirmDialog } from "./confirm-dialog";
import { ago, bytes, duration } from "./pool-progress";
import type { CleanupInventory, CleanupPartial } from "./types";

/** Free space of the export volumes, as a small bar. */
export function DiskUsage({ disk }: { disk: CleanupInventory["disk"] }) {
  const { t } = useLocale();
  if (!disk?.length) return null;
  return (
    <ul className="pg-pool-disk">
      {disk.map((d) => {
        const used = d.total_bytes ? 1 - d.free_bytes / d.total_bytes : 0;
        return (
          <li key={d.path}>
            <code>{d.path}</code> · {t("free")}{" "}
            <strong>{bytes(d.free_bytes)}</strong> / {bytes(d.total_bytes)}
            <div
              className="pg-bar"
              role="progressbar"
              aria-label={t("Disk used")}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(used * 100)}
            >
              <span style={{ width: `${(used * 100).toFixed(1)}%` }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** What jobs left behind (unfinished exports, old job files) with sizes and
 * ages, the free space, and delete buttons. Live jobs' files and finished
 * exports are never offered. */
export function CleanupPanel({
  refreshKey,
  onChanged,
  onNotice,
}: {
  refreshKey: number;
  onChanged: () => void;
  onNotice: (text: string) => void;
}) {
  const { t } = useLocale();
  const colon = useColon();
  const [inventory, setInventory] = useState<CleanupInventory | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<
    CleanupPartial | "expired" | null
  >(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setInventory(await leviRequest<CleanupInventory>("GET", "pool/cleanup"));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function remove() {
    if (!confirming) return;
    setBusy(true);
    try {
      const result = await leviRequest<{
        removed: { bytes: number }[];
        bytes?: number;
      }>(
        "POST",
        "pool/cleanup",
        confirming === "expired"
          ? { sweep: true }
          : { partials: [confirming.path] },
      );
      const freed =
        result.bytes ?? result.removed.reduce((n, r) => n + (r.bytes || 0), 0);
      onNotice(`${t("Freed")} ${bytes(freed)}`);
      setConfirming(null);
      await load();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const empty =
    inventory && !inventory.partials.length && !inventory.jobs.length;
  return (
    <section className="pg-box pg-pool-cleanup" aria-labelledby="pool-cleanup">
      <h2 id="pool-cleanup">{t("Cleanup")}</h2>
      <p className="pg-pool-hint">
        {t(
          "Unfinished exports are kept so they can be resumed, then removed after the time shown. Finished exports are never listed here.",
        )}
      </p>
      {inventory && <DiskUsage disk={inventory.disk} />}
      {error && (
        <RequestProblem
          action="The cleanup did not complete"
          message={error}
          onRetry={() => void load()}
        />
      )}
      {empty && (
        <EmptyLine icon={CircleCheck}>{t("Nothing to clean up.")}</EmptyLine>
      )}
      {inventory && inventory.partials.length > 0 && (
        <div className="ds-table-wrap">
          <table className="ds-table ds-table--compact">
            <thead>
              <tr>
                <th>{t("Unfinished output")}</th>
                <th>{t("State")}</th>
                <th>{t("Size")}</th>
                <th>{t("Age")}</th>
                <th>{t("Removed in")}</th>
                <th>
                  <span className="sr-only">{t("Actions")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {inventory.partials.map((p) => (
                <tr key={p.path}>
                  <td className="pg-pool-ellipsis">
                    <Tooltip content={p.path}>
                      <code tabIndex={0}>{p.name}</code>
                    </Tooltip>
                    {p.job && <small className="pg-pool-muted"> {p.job}</small>}
                  </td>
                  <td>
                    {p.live
                      ? t("running")
                      : p.resumable
                        ? t("can resume")
                        : p.known
                          ? t(p.status || "")
                          : t("unknown job")}
                  </td>
                  <td className="ds-num">{bytes(p.bytes)}</td>
                  <td className="tabular">{ago(p.age_seconds, t)}</td>
                  <td>
                    {p.live
                      ? "—"
                      : p.expires_in_seconds
                        ? duration(p.expires_in_seconds)
                        : t("now")}
                  </td>
                  <td>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="pg-danger-text"
                      icon={Trash2}
                      disabled={p.live}
                      onClick={() => setConfirming(p)}
                    >
                      {t("Delete")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {inventory && inventory.jobs.length > 0 && (
        <p className="pg-pool-hint">
          {inventory.jobs.length} {t("finished job records with logs")} ·{" "}
          {bytes(inventory.jobs.reduce((n, j) => n + j.bytes, 0))}
        </p>
      )}
      {inventory && (
        <div className="pg-row">
          <span className="pg-pool-hint">
            {t("Ready to remove")}
            {colon}
            <strong>{bytes(inventory.reclaimable_bytes)}</strong>
          </span>
          <Button
            size="sm"
            icon={Eraser}
            disabled={!inventory.reclaimable_bytes && !inventory.jobs.length}
            onClick={() => setConfirming("expired")}
          >
            {t("Remove expired")}
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={!!confirming}
        title={t("Delete unfinished output")}
        confirmLabel={t("Delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onCancel={() => setConfirming(null)}
      >
        {confirming === "expired" ? (
          <p>
            {t(
              "Removes unfinished exports older than the keep time, old job files and stale temporaries.",
            )}
          </p>
        ) : confirming ? (
          <>
            <p>
              <code>{confirming.path}</code>
            </p>
            <p>
              {bytes(confirming.bytes)} · {ago(confirming.age_seconds, t)}
              {confirming.resumable &&
                ` · ${t("It could still be resumed; deleting it means a re-run.")}`}
            </p>
          </>
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
