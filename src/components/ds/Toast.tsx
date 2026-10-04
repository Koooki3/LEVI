"use client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { X } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { TONE_ICON, type Tone } from "./Display";
import { Icon } from "./Icon";
import { IconButton } from "./IconButton";
import { cx } from "./internal";

export type ToastOptions = {
  title: ReactNode;
  description?: ReactNode;
  tone?: Tone;
  /** One optional action: undo, view, retry. */
  action?: { label: string; onClick: () => void };
  /** ms before it hides; default 4000; errors (`danger`) stay until closed. */
  duration?: number | null;
};

type ToastItem = ToastOptions & { id: number };

const MAX_VISIBLE = 3;
export const TOAST_DEFAULT_MS = 4000;

type ToastApi = {
  show: (options: ToastOptions) => number;
  dismiss: (id: number) => void;
};

const ToastContext = createContext<ToastApi | null>(null);

/**
 * Show a toast for a result that happened out of view. When the result is
 * next to the control that caused it, show it there instead.
 * Outside a ToastProvider the calls do nothing.
 */
export function useToast(): ToastApi {
  return (
    useContext(ToastContext) ?? {
      show: () => -1,
      dismiss: () => undefined,
    }
  );
}

function ToastCard({
  item,
  onDismiss,
}: {
  item: ToastItem;
  onDismiss: (id: number) => void;
}) {
  const { t } = useLocale();
  const tone = item.tone ?? "neutral";
  const duration =
    item.duration !== undefined
      ? item.duration
      : tone === "danger"
        ? null
        : TOAST_DEFAULT_MS;
  const [paused, setPaused] = useState(false);
  const remaining = useRef(duration ?? 0);
  const startedAt = useRef(0);

  useEffect(() => {
    if (duration === null || paused) return;
    startedAt.current = Date.now();
    const timer = setTimeout(() => onDismiss(item.id), remaining.current);
    return () => {
      clearTimeout(timer);
      remaining.current = Math.max(
        0,
        remaining.current - (Date.now() - startedAt.current),
      );
    };
  }, [duration, paused, item.id, onDismiss]);

  return (
    <div
      className={cx("ds-toast ds-on-raised", `ds-toast--${tone}`)}
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="ds-toast__icon">
        <Icon icon={TONE_ICON[tone]} />
      </span>
      <div className="ds-toast__text">
        <p className="ds-toast__title">{item.title}</p>
        {item.description && (
          <p className="ds-toast__description">{item.description}</p>
        )}
      </div>
      {item.action && (
        <button
          type="button"
          className="ds-toast__action ds-focus"
          onClick={() => {
            item.action?.onClick();
            onDismiss(item.id);
          }}
        >
          {item.action.label}
        </button>
      )}
      <IconButton
        icon={X}
        size="sm"
        label={t("Dismiss notification")}
        onClick={() => onDismiss(item.id)}
      />
    </div>
  );
}

/**
 * The toast region (bottom right, at most three at a time). Successes and
 * notes are announced politely and hide after 4 s (paused while hovered or
 * focused); errors are announced assertively and stay until closed.
 */
export function ToastProvider({
  children,
  contained = false,
}: {
  children: ReactNode;
  /** Pin the region to the nearest positioned ancestor (previews). */
  contained?: boolean;
}) {
  const { t } = useLocale();
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const dismiss = useCallback(
    (id: number) => setItems((list) => list.filter((item) => item.id !== id)),
    [],
  );
  const show = useCallback((options: ToastOptions) => {
    const id = next.current++;
    setItems((list) => [...list, { ...options, id }]);
    return id;
  }, []);
  const api = useMemo(() => ({ show, dismiss }), [show, dismiss]);
  const visible = items.slice(-MAX_VISIBLE);
  const polite = visible.filter((item) => item.tone !== "danger");
  const urgent = visible.filter((item) => item.tone === "danger");
  return (
    <ToastContext.Provider value={api}>
      {children}
      <section
        className={cx(
          "ds-toast-region",
          contained && "ds-toast-region--contained",
        )}
        aria-label={t("Notifications")}
      >
        <div role="status" aria-live="polite" className="ds-toast-stack">
          {polite.map((item) => (
            <ToastCard key={item.id} item={item} onDismiss={dismiss} />
          ))}
        </div>
        <div role="alert" aria-live="assertive" className="ds-toast-stack">
          {urgent.map((item) => (
            <ToastCard key={item.id} item={item} onDismiss={dismiss} />
          ))}
        </div>
      </section>
    </ToastContext.Provider>
  );
}
