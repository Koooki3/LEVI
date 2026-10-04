"use client";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { cx } from "./internal";

type BaseProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & {
  label: ReactNode;
  description?: ReactNode;
};

/** Checkbox with its label; `indeterminate` shows the mixed state. */
export const Checkbox = forwardRef<
  HTMLInputElement,
  BaseProps & { indeterminate?: boolean }
>(function Checkbox(
  { label, description, indeterminate = false, className, ...rest },
  ref,
) {
  const inner = useRef<HTMLInputElement>(null);
  useImperativeHandle(ref, () => inner.current as HTMLInputElement);
  useEffect(() => {
    if (inner.current) inner.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <label className={cx("ds-choice", className)}>
      <input
        ref={inner}
        type="checkbox"
        className="ds-checkbox ds-focus"
        {...rest}
      />
      <span className="ds-choice__text">
        <span className="ds-choice__label">{label}</span>
        {description && (
          <span className="ds-choice__description">{description}</span>
        )}
      </span>
    </label>
  );
});

/** Radio button; group several with the same `name` inside a RadioGroup. */
export const Radio = forwardRef<HTMLInputElement, BaseProps>(function Radio(
  { label, description, className, ...rest },
  ref,
) {
  return (
    <label className={cx("ds-choice", className)}>
      <input ref={ref} type="radio" className="ds-radio ds-focus" {...rest} />
      <span className="ds-choice__text">
        <span className="ds-choice__label">{label}</span>
        {description && (
          <span className="ds-choice__description">{description}</span>
        )}
      </span>
    </label>
  );
});

export function RadioGroup({
  legend,
  children,
  className,
}: {
  legend: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <fieldset className={cx("ds-radio-group", className)}>
      <legend className="ds-field__label">{legend}</legend>
      {children}
    </fieldset>
  );
}

/**
 * On/off switch: a checkbox with `role="switch"`, so Space toggles it and a
 * screen reader says "on"/"off". Use for settings that act at once.
 */
export const Switch = forwardRef<HTMLInputElement, BaseProps>(function Switch(
  { label, description, className, ...rest },
  ref,
) {
  return (
    <label className={cx("ds-choice ds-choice--switch", className)}>
      <span className="ds-choice__text">
        <span className="ds-choice__label">{label}</span>
        {description && (
          <span className="ds-choice__description">{description}</span>
        )}
      </span>
      <input
        ref={ref}
        type="checkbox"
        role="switch"
        className="ds-switch ds-focus"
        {...rest}
      />
    </label>
  );
});
