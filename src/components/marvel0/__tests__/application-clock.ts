import { GlobalRegistrator } from "@happy-dom/global-registrator";

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

/**
 * Advances `ms` in steps of at most `step` (default: the 30 s status refresh of
 * the voice card), awaiting `settle` after each so Promise work answers before
 * the next periodic timer, as it would in a browser. One long jump runs every
 * due timer back to back, before any pending Promise body can settle.
 */
export async function advanceInSteps(
  clock: { advance(ms: number): void },
  ms: number,
  settle: () => Promise<void>,
  step = 30_000,
) {
  if (!Number.isFinite(ms) || ms < 0 || !(step > 0))
    throw new Error(
      "advanceInSteps requires a finite nonnegative duration and a positive step",
    );
  let left = ms;
  do {
    const part = Math.min(left, step);
    clock.advance(part);
    await settle();
    left -= part;
  } while (left > 0);
}

/**
 * installApplicationClock for a DOM test file (one that calls setupDom()); call
 * it in beforeAll and register afterAll(() => clock.restore()) before
 * setupDom(). happy-dom is registered first: when an earlier DOM file in the
 * same process has unregistered it, setupDom's own beforeAll would register it
 * after the clock, take the clock's timers for the originals and put them back
 * at the end, so every later setTimeout in that process would throw
 * "application clock is restored".
 */
export function installDomApplicationClock() {
  if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
  return installApplicationClock();
}
