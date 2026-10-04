"use client";
import { Monitor, Moon, Sun } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import type { ThemePreference } from "@/lib/design/theme";
import { SegmentedControl } from "./Tabs";

/** System / Light / Dark choice, for the settings menu (stage 2). */
export function ThemePicker({
  value,
  onChange,
  size = "sm",
}: {
  value: ThemePreference;
  onChange: (value: ThemePreference) => void;
  size?: "sm" | "md";
}) {
  const { t } = useLocale();
  return (
    <SegmentedControl
      label={t("Theme")}
      size={size}
      value={value}
      onChange={(next) => onChange(next as ThemePreference)}
      options={[
        { value: "system", label: t("System"), icon: Monitor },
        { value: "light", label: t("Light"), icon: Sun },
        { value: "dark", label: t("Dark"), icon: Moon },
      ]}
    />
  );
}
