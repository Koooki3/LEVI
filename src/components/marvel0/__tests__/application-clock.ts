/** Application-only test clock. Load React first; never replace its captured host objects. */
export function installApplicationClock() {
  type Timer = { due: number; period: number | null; run: () => void };
  const timers = new Map<number, Timer>();
  const saved: Array<[object, PropertyKey, PropertyDescriptor | undefined]> =
    [];
  const host = globalThis.performance;
  const epoch = Date.now();
  const origin = host.now();
  let elapsed = 0,
    nextId = 1,
    active = true,
    advancing = false;
  function replace(object: object, key: PropertyKey, value: unknown) {
    saved.push([object, key, Object.getOwnPropertyDescriptor(object, key)]);
    Object.defineProperty(object, key, {
      configurable: true,
      writable: true,
      value,
    });
  }
  function restore() {
    if (!active) return;
    for (const [object, key, descriptor] of saved.reverse()) {
      if (descriptor) Object.defineProperty(object, key, descriptor);
      else Reflect.deleteProperty(object, key);
    }
    timers.clear();
    active = false;
  }
  function schedule(
    fn: (...args: unknown[]) => void,
    delay = 0,
    repeat = false,
    args: unknown[] = [],
  ) {
    if (!active) throw new Error("application clock is restored");
    if (typeof fn !== "function" || !Number.isFinite(delay) || delay < 0)
      throw new Error(
        "application clock requires a callback and finite nonnegative delay",
      );
    const ms = repeat ? Math.max(1, delay) : delay;
    const id = nextId++;
    timers.set(id, {
      due: elapsed + ms,
      period: repeat ? ms : null,
      run: () => fn(...args),
    });
    return id;
  }
  try {
    replace(
      globalThis,
      "setTimeout",
      (fn: (...args: unknown[]) => void, ms?: number, ...args: unknown[]) =>
        schedule(fn, ms, false, args),
    );
    replace(
      globalThis,
      "setInterval",
      (fn: (...args: unknown[]) => void, ms?: number, ...args: unknown[]) =>
        schedule(fn, ms, true, args),
    );
    const cancel = (id?: number) => {
      if (id !== undefined) timers.delete(id);
    };
    replace(globalThis, "clearTimeout", cancel);
    replace(globalThis, "clearInterval", cancel);
    replace(Date, "now", () => epoch + elapsed);
    const now = () => origin + elapsed;
    replace(
      globalThis,
      "performance",
      new Proxy(host, {
        get(target, key) {
          if (key === "now") return now;
          const value = Reflect.get(target, key, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      }),
    );
  } catch (error) {
    restore();
    throw error;
  }
  return {
    restore,
    advance(ms: number) {
      if (
        !active ||
        advancing ||
        !Number.isFinite(ms) ||
        ms < 0 ||
        !Number.isFinite(elapsed + ms)
      )
        throw new Error(
          "application clock advance requires active clock, finite nonnegative duration, and no recursion",
        );
      const target = elapsed + ms;
      let calls = 0;
      advancing = true;
      try {
        for (;;) {
          let selected: [number, Timer] | undefined;
          for (const entry of timers) {
            if (
              entry[1].due <= target &&
              (!selected ||
                entry[1].due < selected[1].due ||
                (entry[1].due === selected[1].due && entry[0] < selected[0]))
            )
              selected = entry;
          }
          if (!selected) break;
          if (++calls > 10000)
            throw new Error("application clock callback limit exceeded");
          const [id, timer] = selected;
          elapsed = timer.due;
          if (timer.period === null) timers.delete(id);
          else timer.due += timer.period;
          timer.run();
        }
        elapsed = target;
      } finally {
        advancing = false;
      }
    },
  };
}
