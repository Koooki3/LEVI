// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Maximize2, Minimize2 } from "lucide-react";
import { Button, Checkbox, Tooltip as Tip } from "@/components/ds";
import { seriesColor, seriesDash } from "@/components/viewer/data-palette";
import { SeriesSwatch } from "@/components/viewer/series-swatch";
import { T } from "@/components/levi-locale";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTime } from "../context/time-context";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  ReferenceLine,
} from "recharts";

type ChartRow = Record<string, number | Record<string, number>>;

type DataGraphProps = {
  data: ChartRow[][];
  onChartsReady?: () => void;
};

const SERIES_NAME_DELIMITER = " | ";

function mergeGroups(data: ChartRow[][]): ChartRow[] {
  if (data.length <= 1) return data[0] ?? [];
  const maxLen = Math.max(...data.map((g) => g.length));
  const merged: ChartRow[] = [];
  for (let i = 0; i < maxLen; i++) {
    const row: ChartRow = {};
    for (const group of data) {
      const src = group[i];
      if (!src) continue;
      for (const [k, v] of Object.entries(src)) {
        if (k === "timestamp") {
          row[k] = v;
          continue;
        }
        row[k] = v;
      }
    }
    merged.push(row);
  }
  return merged;
}

export const DataRecharts = React.memo(
  ({ data, onChartsReady }: DataGraphProps) => {
    const [hoveredTime, setHoveredTime] = useState<number | null>(null);
    const [expanded, setExpanded] = useState(false);

    useEffect(() => {
      if (typeof onChartsReady === "function") onChartsReady();
    }, [onChartsReady]);

    const combinedData = useMemo(
      () => (expanded ? mergeGroups(data) : []),
      [data, expanded],
    );

    if (!Array.isArray(data) || data.length === 0) return null;

    return (
      <T>
        {
          <div>
            {data.length > 1 && (
              <div className="vw-chart-toolbar">
                <Button
                  size="sm"
                  icon={expanded ? Minimize2 : Maximize2}
                  aria-pressed={expanded}
                  onClick={() => setExpanded((v) => !v)}
                >
                  <T>{expanded ? "Split charts" : "Combine all"}</T>
                </Button>
              </div>
            )}

            <T>
              {expanded ? (
                <SingleDataGraph
                  data={combinedData}
                  hoveredTime={hoveredTime}
                  setHoveredTime={setHoveredTime}
                  tall
                />
              ) : (
                <div className="vw-chart-grid">
                  {data.map((group, idx) => (
                    <SingleDataGraph
                      key={idx}
                      data={group}
                      hoveredTime={hoveredTime}
                      setHoveredTime={setHoveredTime}
                    />
                  ))}
                </div>
              )}
            </T>
          </div>
        }
      </T>
    );
  },
);

const SingleDataGraph = React.memo(
  ({
    data,
    hoveredTime,
    setHoveredTime,
    tall,
  }: {
    data: ChartRow[];
    hoveredTime: number | null;
    setHoveredTime: (t: number | null) => void;
    tall?: boolean;
  }) => {
    const { currentTime, seek } = useTime();
    const flattenRow = useCallback(
      (row: Record<string, number | Record<string, number>>, prefix = "") => {
        const result: Record<string, number> = {};
        for (const [key, value] of Object.entries(row)) {
          // Special case: if this is a group value that is a primitive, assign to prefix.key
          if (typeof value === "number") {
            if (prefix) {
              result[`${prefix}${SERIES_NAME_DELIMITER}${key}`] = value;
            } else {
              result[key] = value;
            }
          } else if (
            value !== null &&
            typeof value === "object" &&
            !Array.isArray(value)
          ) {
            // If it's an object, recurse
            Object.assign(
              result,
              flattenRow(
                value,
                prefix ? `${prefix}${SERIES_NAME_DELIMITER}${key}` : key,
              ),
            );
          }
        }
        if ("timestamp" in row && typeof row["timestamp"] === "number") {
          result["timestamp"] = row["timestamp"];
        }
        return result;
      },
      [],
    );

    // Flatten all rows for recharts
    const chartData = useMemo(
      () => data.map((row) => flattenRow(row)),
      [data, flattenRow],
    );

    // dataKeys is purely derived from chartData — compute it during render,
    // not via a useState + useEffect that would force an extra render and
    // briefly leak the previous chart's keys after `data` changes.
    // Vercel rule: rerender-derived-state-no-effect.
    const dataKeys = useMemo(() => {
      const first = chartData[0];
      if (!first) return [];
      return Object.keys(first).filter((k) => k !== "timestamp");
    }, [chartData]);

    // visibleKeys IS user-facing state (column toggles). Reset it whenever
    // the underlying schema changes — keying off the joined dataKeys string
    // catches both episode navigations and combine/split toggles.
    const dataKeysSig = dataKeys.join("|");
    const [visibleKeys, setVisibleKeys] = useState<string[]>(dataKeys);
    const lastSigRef = useRef(dataKeysSig);
    if (lastSigRef.current !== dataKeysSig) {
      lastSigRef.current = dataKeysSig;
      // Setting state during render is fine here — React schedules a
      // re-render and discards the in-progress one. This pattern is
      // documented as "Storing information from previous renders".
      setVisibleKeys(dataKeys);
    }

    const { groups, singles, groupColorMap, groupIndex } = useMemo(() => {
      const grouped: Record<string, string[]> = {};
      const singleList: string[] = [];
      dataKeys.forEach((key) => {
        const parts = key.split(SERIES_NAME_DELIMITER);
        if (parts.length > 1) {
          const group = parts[0];
          if (!grouped[group]) grouped[group] = [];
          grouped[group].push(key);
        } else {
          singleList.push(key);
        }
      });

      const allGroups = [...Object.keys(grouped), ...singleList];
      const colorMap: Record<string, string> = {};
      const indexMap: Record<string, number> = {};
      allGroups.forEach((group, idx) => {
        colorMap[group] = seriesColor(idx);
        indexMap[group] = idx;
      });
      return {
        groups: grouped,
        singles: singleList,
        groupColorMap: colorMap,
        groupIndex: indexMap,
      };
    }, [dataKeys]);

    // Find the closest data point to the current time for highlighting
    const findClosestDataIndex = (time: number) => {
      if (!chartData.length) return 0;
      // Find the index of the first data point whose timestamp is >= time (ceiling)
      const idx = chartData.findIndex((point) => point.timestamp >= time);
      if (idx !== -1) return idx;
      // If all timestamps are less than time, return the last index
      return chartData.length - 1;
    };

    const handleMouseLeave = () => {
      setHoveredTime(null);
    };

    const handleClick = (
      data: { activePayload?: { payload: { timestamp: number } }[] } | null,
    ) => {
      if (data?.activePayload?.length) {
        seek(data.activePayload[0].payload.timestamp);
      }
    };

    // Custom legend to show current value next to each series
    const CustomLegend = () => {
      const closestIndex = findClosestDataIndex(
        hoveredTime != null ? hoveredTime : currentTime,
      );
      const currentData = chartData[closestIndex] || {};

      const isGroupChecked = (group: string) =>
        groups[group].every((k) => visibleKeys.includes(k));
      const isGroupIndeterminate = (group: string) =>
        groups[group].some((k) => visibleKeys.includes(k)) &&
        !isGroupChecked(group);

      const handleGroupCheckboxChange = (group: string) => {
        if (isGroupChecked(group)) {
          // Uncheck all children
          setVisibleKeys((prev) =>
            prev.filter((k) => !groups[group].includes(k)),
          );
        } else {
          // Check all children
          setVisibleKeys((prev) =>
            Array.from(new Set([...prev, ...groups[group]])),
          );
        }
      };

      const handleCheckboxChange = (key: string) => {
        setVisibleKeys((prev) =>
          prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
        );
      };

      const value = (key: string) =>
        typeof currentData[key] === "number"
          ? currentData[key].toFixed(2)
          : "–";

      return (
        <div className="vw-series-legend">
          {Object.entries(groups).map(([group, children]) => (
            <div key={group} className="vw-series-group">
              <Checkbox
                checked={isGroupChecked(group)}
                indeterminate={isGroupIndeterminate(group)}
                onChange={() => handleGroupCheckboxChange(group)}
                label={
                  <span className="vw-series-name vw-series-name--group">
                    <SeriesSwatch index={groupIndex[group] ?? 0} />
                    {group}
                  </span>
                }
              />
              <div className="vw-series-children">
                {children.map((key) => {
                  const label = key.split(SERIES_NAME_DELIMITER).pop() ?? key;
                  return (
                    <div key={key} className="vw-series-row">
                      <Checkbox
                        checked={visibleKeys.includes(key)}
                        onChange={() => handleCheckboxChange(key)}
                        label={<span className="vw-series-name">{label}</span>}
                      />
                      <span
                        className="vw-series-value"
                        data-off={!visibleKeys.includes(key) || undefined}
                      >
                        {value(key)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {singles.map((key) => (
            <div key={key} className="vw-series-row">
              <Checkbox
                checked={visibleKeys.includes(key)}
                onChange={() => handleCheckboxChange(key)}
                label={
                  <span className="vw-series-name">
                    <SeriesSwatch index={groupIndex[key] ?? 0} />
                    {key}
                  </span>
                }
              />
              <span
                className="vw-series-value"
                data-off={!visibleKeys.includes(key) || undefined}
              >
                {value(key)}
              </span>
            </div>
          ))}
        </div>
      );
    };

    // Derive chart title from the grouped feature names
    const chartTitle = useMemo(() => {
      const featureNames = Object.keys(groups);
      if (featureNames.length > 0) {
        const suffixes = featureNames.map((g) => {
          const parts = g.split(SERIES_NAME_DELIMITER);
          return parts[parts.length - 1];
        });
        return suffixes.join(", ");
      }
      return singles.join(", ");
    }, [groups, singles]);

    return (
      <T>
        {
          <div className="vw-chart vw-panel vw-chart-panel">
            {chartTitle && (
              <Tip content={chartTitle}>
                <p className="vw-chart-title" tabIndex={0}>
                  {chartTitle}
                </p>
              </Tip>
            )}
            <div
              className={
                tall ? "vw-chart-plot vw-chart-plot--tall" : "vw-chart-plot"
              }
              onMouseLeave={handleMouseLeave}
            >
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={chartData}
                  syncId="episode-sync"
                  margin={{ top: 12, right: 12, left: -8, bottom: 8 }}
                  onClick={handleClick}
                  onMouseMove={(state) => {
                    const payload = state?.activePayload?.[0]?.payload as
                      | { timestamp?: number }
                      | undefined;
                    setHoveredTime(payload?.timestamp ?? null);
                  }}
                  onMouseLeave={handleMouseLeave}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="var(--ds-separator)"
                  />
                  <XAxis
                    dataKey="timestamp"
                    domain={[
                      chartData.at(0)?.timestamp ?? 0,
                      chartData.at(-1)?.timestamp ?? 0,
                    ]}
                    tickFormatter={(v: number) => `${v.toFixed(1)}s`}
                    stroke="var(--ds-separator-strong)"
                    tick={{ fontSize: 12, fill: "var(--ds-text-secondary)" }}
                    minTickGap={30}
                    allowDataOverflow={true}
                  />
                  <YAxis
                    domain={["auto", "auto"]}
                    stroke="var(--ds-separator-strong)"
                    tick={{ fontSize: 12, fill: "var(--ds-text-secondary)" }}
                    width={55}
                    allowDataOverflow={true}
                    tickFormatter={(v: number) => {
                      if (v === 0) return "0";
                      const abs = Math.abs(v);
                      if (abs < 0.01 || abs >= 10000) return v.toExponential(1);
                      return Number(v.toFixed(2)).toString();
                    }}
                  />

                  <Tooltip
                    content={() => null}
                    active={true}
                    isAnimationActive={false}
                    defaultIndex={
                      !hoveredTime
                        ? findClosestDataIndex(currentTime)
                        : undefined
                    }
                  />

                  <ReferenceLine
                    x={currentTime}
                    stroke="var(--ds-text-primary)"
                    strokeWidth={1.5}
                  />

                  {dataKeys.map((key) => {
                    const group = key.includes(SERIES_NAME_DELIMITER)
                      ? key.split(SERIES_NAME_DELIMITER)[0]
                      : key;
                    const color = groupColorMap[group];
                    // Past eight groups the colours repeat: the pattern tells
                    // them apart (the legend shows it too).
                    let strokeDasharray: string | undefined = seriesDash(
                      groupIndex[group] ?? 0,
                    );
                    if (groups[group] && groups[group].length > 1) {
                      const idxInGroup = groups[group].indexOf(key);
                      if (idxInGroup > 0) strokeDasharray = "5 5";
                    }
                    return (
                      visibleKeys.includes(key) && (
                        <Line
                          key={key}
                          type="monotone"
                          dataKey={key}
                          name={key}
                          stroke={color}
                          strokeDasharray={strokeDasharray}
                          dot={false}
                          activeDot={false}
                          strokeWidth={1.5}
                          isAnimationActive={false}
                        />
                      )
                    );
                  })}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <CustomLegend />
          </div>
        }
      </T>
    );
  },
); // End React.memo

SingleDataGraph.displayName = "SingleDataGraph";
DataRecharts.displayName = "DataGraph";
export default DataRecharts;
