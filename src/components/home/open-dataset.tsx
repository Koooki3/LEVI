"use client";
/**
 * One field that opens any Hugging Face dataset by ID, with live suggestions
 * from the Hub search. Used by the home page and by Explore. Styles:
 * src/styles/home.css.
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight, Search } from "lucide-react";
import { Button, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

const DATASET_ID = /^[\w.-]+\/[\w.-]+$/;

export function OpenDataset() {
  const { t } = useLocale();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const invalid = query.trim() !== "" && !DATASET_ID.test(query.trim());
  useEffect(() => {
    const controller = new AbortController();
    if (!query.trim()) {
      setSuggestions([]);
      return;
    }
    const timer = setTimeout(
      () =>
        fetch(
          `https://huggingface.co/api/datasets?search=${encodeURIComponent(query)}&limit=5`,
          { signal: controller.signal },
        )
          .then((r) => r.json())
          .then((data) =>
            setSuggestions(
              Array.isArray(data) ? data.map((d: { id: string }) => d.id) : [],
            ),
          )
          .catch(() => {}),
      250,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);
  function open(id: string) {
    if (DATASET_ID.test(id.trim())) router.push(`/${id.trim()}`);
  }
  return (
    <div className="levi-home-open">
      <form
        className="levi-home-search"
        role="search"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          open(query);
        }}
      >
        <Icon icon={Search} className="levi-home-search__icon" />
        <input
          className="levi-home-search__input ds-focus"
          aria-label={t("Dataset ID")}
          aria-invalid={invalid || undefined}
          placeholder={t("Search or enter a Hugging Face dataset ID")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          required
          pattern="[\w.\-]+/[\w.\-]+"
        />
        <Button type="submit" variant="primary" iconEnd={ArrowRight}>
          {t("Open dataset")}
        </Button>
      </form>
      {suggestions.length > 0 && (
        <ul className="levi-home-suggestions" aria-label={t("Suggestions")}>
          {suggestions.map((id) => (
            <li key={id}>
              <button
                type="button"
                className="ds-focus"
                onClick={() => open(id)}
              >
                <span className="levi-home-mono">{id}</span>
                <Icon icon={ArrowRight} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
