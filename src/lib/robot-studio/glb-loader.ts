import type { LoadingManager } from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";

/** A GLTFLoader for the vendored robot meshes in `public/robots/`. They are
 * stored meshopt-compressed with quantized positions
 * (`scripts/robot-studio/compress-glb.py`), so every loader needs the meshopt
 * decoder; quantization is undone by the node transforms gltfpack writes. */
export function robotGlbLoader(manager?: LoadingManager): GLTFLoader {
  return new GLTFLoader(manager).setMeshoptDecoder(MeshoptDecoder);
}
