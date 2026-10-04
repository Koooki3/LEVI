"use client";
import {
  createContext,
  forwardRef,
  useContext,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { ChevronDown } from "lucide-react";
import { Icon } from "./Icon";
import { cx } from "./internal";

type FieldIds = {
  id: string;
  describedBy?: string;
  invalid: boolean;
  required?: boolean;
};

const FieldContext = createContext<FieldIds | null>(null);

/**
 * Label, control, hint and error in one block. The control inside gets the
 * id, `aria-describedby` (hint and error) and `aria-invalid` from here.
 * Validate when focus leaves the field and put the message in `error`.
 */
export function Field({
  label,
  hint,
  error,
  required,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = cx(hintId, errorId) || undefined;
  return (
    <div className={cx("ds-field", className)}>
      <label className="ds-field__label" htmlFor={id}>
        {label}
        {required && (
          <span className="ds-field__required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>
      <FieldContext.Provider
        value={{ id, describedBy, invalid: Boolean(error), required }}
      >
        {children}
      </FieldContext.Provider>
      {hint && (
        <p id={hintId} className="ds-field__hint">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="ds-field__error">
          {error}
        </p>
      )}
    </div>
  );
}

function useFieldProps(props: {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: InputHTMLAttributes<HTMLInputElement>["aria-invalid"];
  required?: boolean;
}) {
  const field = useContext(FieldContext);
  return {
    id: props.id ?? field?.id,
    "aria-describedby":
      cx(props["aria-describedby"], field?.describedBy) || undefined,
    "aria-invalid": props["aria-invalid"] ?? (field?.invalid || undefined),
    required: props.required ?? field?.required,
  };
}

export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement> & { size?: never }
>(function Input({ className, ...rest }, ref) {
  const field = useFieldProps(rest);
  return (
    <input
      ref={ref}
      className={cx("ds-input ds-focus", className)}
      {...rest}
      {...field}
    />
  );
});

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, rows = 3, ...rest }, ref) {
  const field = useFieldProps(rest);
  return (
    <textarea
      ref={ref}
      rows={rows}
      className={cx("ds-input ds-textarea ds-focus", className)}
      {...rest}
      {...field}
    />
  );
});

/** A native select (keeps the platform's keyboard and screen-reader support). */
export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, children, ...rest }, ref) {
  const field = useFieldProps(rest);
  return (
    <span className={cx("ds-select", className)}>
      <select
        ref={ref}
        className="ds-input ds-select__control ds-focus"
        {...rest}
        {...field}
      >
        {children}
      </select>
      <Icon icon={ChevronDown} className="ds-select__chevron" />
    </span>
  );
});
