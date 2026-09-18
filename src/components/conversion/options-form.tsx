"use client";
import { T, useLocale } from "@/components/levi-locale";
import type { ConversionOptions, InputReport } from "./types";

/** Pipeline and target options for the chosen export. */
export function OptionsForm({
  report,
  options,
  onChange,
  output,
  onOutputChange,
}: {
  report: InputReport;
  options: ConversionOptions;
  onChange: (next: ConversionOptions) => void;
  output: string;
  onOutputChange: (value: string) => void;
}) {
  const { t } = useLocale();
  const set = (patch: Partial<ConversionOptions>) =>
    onChange({ ...options, ...patch });
  const setTarget = (patch: Record<string, unknown>) =>
    set({ target_options: { ...options.target_options, ...patch } });
  const fromDataset = report.format === "lerobot";
  const s = report.summary;
  const measured =
    s.fps_min != null
      ? `${s.fps_min.toFixed(2)}${
          s.fps_max != null && s.fps_max !== s.fps_min
            ? `–${s.fps_max.toFixed(2)}`
            : ""
        }`
      : null;
  const recap = options.target === "recap_value";
  const to = options.target_options;
  return (
    <div className="levi-options">
      {!fromDataset && (
        <>
          <label>
            <T>Frame timing</T>
            <select
              className="levi-input"
              value={options.timing}
              onChange={(e) =>
                set({ timing: e.target.value as ConversionOptions["timing"] })
              }
            >
              <option value="retime">
                {t("Keep every frame (retime, lossless)")}
              </option>
              <option value="resample">
                {t("Resample to the output FPS (drops frames)")}
              </option>
            </select>
          </label>
          <label>
            <T>Output FPS</T>
            <input
              className="levi-input"
              type="number"
              min={1}
              max={240}
              step="any"
              value={options.fps}
              onChange={(e) => set({ fps: Number(e.target.value) })}
            />
            {measured && (
              <span className="text-xs">
                <T>Measured capture FPS</T>: {measured}
                {options.timing === "retime" &&
                  ` · ${t("every captured frame is declared at this rate")}`}
              </span>
            )}
          </label>
          {report.format === "image_sequence" && (
            <label>
              <T>Source FPS</T>
              <input
                className="levi-input"
                type="number"
                min={1}
                max={240}
                step="any"
                value={options.source_fps}
                onChange={(e) => set({ source_fps: Number(e.target.value) })}
              />
            </label>
          )}
          <label className="levi-check">
            <input
              type="checkbox"
              checked={options.filter_static}
              onChange={(e) => set({ filter_static: e.target.checked })}
            />
            <T>Drop static frames (robot not moving)</T>
          </label>
          <label>
            <T>Parallel workers</T>
            <input
              className="levi-input"
              type="number"
              min={1}
              max={64}
              placeholder={t("auto")}
              value={options.workers ?? ""}
              onChange={(e) =>
                set({
                  workers: e.target.value ? Number(e.target.value) : null,
                })
              }
            />
          </label>
          <label className="levi-check">
            <input
              type="checkbox"
              checked={options.keep_intermediates}
              onChange={(e) => set({ keep_intermediates: e.target.checked })}
            />
            <T>Keep intermediate captures for auditing (slower)</T>
          </label>
        </>
      )}
      {recap && (
        <>
          <label>
            <T>Dataset type</T>
            <select
              className="levi-input"
              value={String(to.dataset_type ?? "rollout")}
              onChange={(e) => setTarget({ dataset_type: e.target.value })}
            >
              <option value="rollout">
                {t("Rollouts (success / failure labels)")}
              </option>
              <option value="sft">
                {t("Demonstrations (all successful)")}
              </option>
            </select>
          </label>
          <label>
            <T>Failure reward</T>
            <input
              className="levi-input"
              type="number"
              max={0}
              step="any"
              value={Number(to.failure_reward ?? -300)}
              onChange={(e) =>
                setTarget({ failure_reward: Number(e.target.value) })
              }
            />
          </label>
          <label>
            <T>Discount (gamma)</T>
            <input
              className="levi-input"
              type="number"
              min={0.01}
              max={1}
              step="any"
              value={Number(to.gamma ?? 1)}
              onChange={(e) => setTarget({ gamma: Number(e.target.value) })}
            />
          </label>
          <label>
            <T>Returns tag (optional)</T>
            <input
              className="levi-input"
              value={String(to.tag ?? "")}
              placeholder="returns_<tag>.parquet"
              onChange={(e) => setTarget({ tag: e.target.value || null })}
            />
          </label>
        </>
      )}
      <label className="levi-wide">
        <T>Output directory (optional)</T>
        <input
          className="levi-input"
          value={output}
          onChange={(e) => onOutputChange(e.target.value)}
          placeholder={t(
            "Leave blank for <source>_<target>_<timestamp> under LEVI_WORKSPACE",
          )}
        />
      </label>
      {options.exclude_demos.length > 0 && (
        <p className="levi-wide text-xs">
          <T>Excluded episodes</T>: {options.exclude_demos.length}{" "}
          <button
            type="button"
            className="underline"
            onClick={() => set({ exclude_demos: [] })}
          >
            <T>clear</T>
          </button>
        </p>
      )}
    </div>
  );
}
