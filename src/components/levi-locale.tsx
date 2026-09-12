"use client";
import React, { createContext, useContext, useEffect, useState } from "react";
import zh from "@/i18n/zh.json";
import en from "@/i18n/en.json";

const Locale = createContext({
  language: "zh",
  setLanguage: (value: string) => {
    void value;
  },
});
export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguage] = useState("zh");
  useEffect(() => {
    setLanguage(localStorage.getItem("levi-language") || "zh");
  }, []);
  useEffect(() => {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  }, [language]);
  return (
    <Locale.Provider
      value={{
        language,
        setLanguage: (value) => {
          localStorage.setItem("levi-language", value);
          setLanguage(value);
        },
      }}
    >
      {children}
    </Locale.Provider>
  );
}
export function useLocale() {
  const { language, setLanguage } = useContext(Locale);
  const t = (text: string) => {
    if (language !== "zh")
      return (en as Record<string, string>)[text.trim()] || text;
    const key = text.replace(/\s+/g, " ").trim();
    const translated = (zh as Record<string, string>)[key];
    if (translated) return text.replace(text.trim(), translated);
    const patterns: [RegExp, string][] = [
      [/^Episode (\d+)$/, "片段 $1"],
      [/^ep (\d+)$/, "片段 $1"],
      [/^(\d+) episodes sampled$/, "$1 段采样"],
      [/^\((\d+) episodes sampled\)$/, "（$1 段采样）"],
      [/^(\d+) smooth \((.*)\)$/, "$1 个平滑维度（$2）"],
      [/^(\d+) moderate \((.*)\)$/, "$1 个中等维度（$2）"],
      [/^(\d+) jerky \((.*)\)$/, "$1 个突变维度（$2）"],
      [
        /^(\d+) grippers? jerky — expected for binary open\/close$/,
        "$1 个夹爪维度存在突变（二值开合的预期现象）",
      ],
      [
        /^State changes lag behind actions by ~(\d+) frames on average\. Consider aligning action\[t\] with state\[t\+(\d+)\]\.$/,
        "状态变化平均落后动作约 $1 帧，可考虑对齐 action[t] 与 state[t+$2]。",
      ],
      [
        /^Actions lag behind state changes by ~(\d+) frames on average \(predictive actions\)\.$/,
        "动作平均落后状态变化约 $1 帧（预测型动作）。",
      ],
      [
        /^Individual dimension peaks range from (.*) to (.*) steps\.$/,
        "各维度峰值滞后范围为 $1 至 $2 步。",
      ],
      [/^Saved episode to (.*)$/, "片段已保存至 $1"],
      [/^Save failed: (.*)$/, "保存失败：$1"],
      [
        /^Saved dataset to (.*) \(persistent: (\d+), events: (\d+)\)\.$/,
        "数据集已导出至 $1（持续标注行：$2；事件标注行：$3）。",
      ],
      [
        /^Updated existing export at (.*) \(persistent: (\d+), events: (\d+)\)\.$/,
        "已更新现有导出 $1（持续标注行：$2；事件标注行：$3）。",
      ],
      [/^Save dataset failed: (.*)$/, "数据集导出失败：$1"],
      [/^Delete failed: (.*)$/, "删除失败：$1"],
      [/^Task metadata: (.*)$/, "任务元数据：$1"],
      [/^Sampled (\d+) of (\d+) episodes$/, "采样 $1 / $2 个片段"],
      [
        /^Analysed (\d+) of (\d+) episodes in scope$/,
        "已分析范围内 $1 / $2 个片段",
      ],
      [/^Loading (\d+) \/ (\d+) episodes…$/, "正在加载片段 $1 / $2…"],
      [/^Episodes (\d+)–(\d+)$/, "片段 $1–$2"],
      [/^All tasks \((\d+)\)$/, "全部任务（$1）"],
      [/^(.*): decoding not requested$/, "$1：未请求解码检查"],
      [
        /^Constant actuator dimensions \(may be intentional\): (.*)$/,
        "恒定执行器维度（可能是预期行为）：$1",
      ],
      [
        /^(.*): (\d+) values beyond 10 standard deviations$/,
        "$1：存在 $2 个超出 10 倍标准差的值",
      ],
    ];
    for (const [pattern, replacement] of patterns)
      if (pattern.test(key))
        return text.replace(text.trim(), key.replace(pattern, replacement));
    return text;
  };
  return { language, setLanguage, t };
}
export function T({ children }: { children: React.ReactNode }) {
  const { t } = useLocale();
  const translate = (node: React.ReactNode): React.ReactNode => {
    if (typeof node === "string") return t(node);
    if (Array.isArray(node)) return React.Children.map(node, translate);
    if (React.isValidElement<{ children?: React.ReactNode }>(node)) {
      const props = node.props as Record<string, unknown>;
      const translated: Record<string, unknown> = {};
      for (const key of [
        "title",
        "placeholder",
        "aria-label",
        "alt",
        "label",
      ]) {
        if (typeof props[key] === "string")
          translated[key] = t(props[key] as string);
        else if (
          key === "label" &&
          props[key] &&
          typeof props[key] === "object" &&
          "value" in (props[key] as object)
        ) {
          const label = props[key] as { value?: unknown };
          if (typeof label.value === "string")
            translated[key] = { ...label, value: t(label.value) };
        }
      }
      return React.cloneElement(
        node,
        translated,
        translate(node.props.children),
      );
    }
    return node;
  };
  return <>{translate(children)}</>;
}
export function LanguageSwitch() {
  const { language, setLanguage } = useLocale();
  return (
    <button
      className="levi-language"
      aria-label="Switch language / 切换语言"
      onClick={() => setLanguage(language === "zh" ? "en" : "zh")}
    >
      {language === "zh" ? "EN / 中文" : "中文 / EN"}
    </button>
  );
}
