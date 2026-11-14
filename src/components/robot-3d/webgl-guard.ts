// A WebGL context that cannot be created must reach the panel as a failure.
//
// React Three Fiber's <Canvas> makes the renderer inside an async function
// that nothing awaits (`run()` in its layout effect, which has no dependency
// list and so runs after every render). When `new THREE.WebGLRenderer` throws
// ("Error creating WebGL context.": WebGL is disabled, `getContext` returns
// null, the GPU process is gone) the rejection is unhandled, the Canvas's
// ErrorBoundary never sees it (it only wraps the children, which are never
// rendered), and every re-render tries again: an endless loading label, a
// blank canvas and a stream of uncaught errors. Passing `gl` as a function
// moves the construction into our hands: one attempt per mount, the failure
// reported once.
import * as THREE from "three";

type RendererParameters = THREE.WebGLRendererParameters;

/**
 * A `gl` function for <Canvas>. It builds the renderer from the Canvas's
 * defaults plus `extra`. If that throws, `onFailure` is called once with the
 * error and the returned promise never settles, so the Canvas goes no further
 * (it is unmounted by the panel's error state) and does not retry on its next
 * render. Make one per mount: Retry mounts a new scene, hence a new attempt.
 */
export function createGuardedRenderer(
  extra: Partial<RendererParameters>,
  onFailure: (error: unknown) => void,
  create: (parameters: RendererParameters) => THREE.WebGLRenderer = (
    parameters,
  ) => new THREE.WebGLRenderer(parameters),
) {
  let failed = false;
  return (defaults: RendererParameters): Promise<THREE.WebGLRenderer> => {
    if (!failed) {
      try {
        return Promise.resolve(create({ ...defaults, ...extra }));
      } catch (error) {
        failed = true;
        onFailure(error);
      }
    }
    return new Promise<THREE.WebGLRenderer>(() => {});
  };
}
