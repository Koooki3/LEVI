// Puts a `Say` of the marvel0's view model into words in the reader's
// language. `t` is the catalogue lookup (`useLocale().t`). A placeholder may
// hold another `Say`, so an age inside a sentence is worded like any other.
import { ago } from "@/components/pool/pool-progress";
import type { Say } from "./marvel0-logic";

type T = (text: string) => string;

export function sayText(say: Say, t: T): string {
  if ("raw" in say) return say.raw;
  if ("ago" in say) return ago(say.ago, t);
  if ("join" in say)
    return say.join.map((part) => sayText(part, t)).join(" · ");
  const text = t(say.key);
  const params = say.params;
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (match, name: string) => {
    if (!(name in params)) return match;
    const value = params[name];
    return typeof value === "object" ? sayText(value, t) : String(value);
  });
}
