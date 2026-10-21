// The answer of POST /api/levi/marvel0/command/preview as the backend writes it
// (levi/marvel0/catalog_api.py command_preview), for the default form of the SFT
// model on a site with placeholder paths.
import { siteAnswer } from "./catalog-fixtures";
import hzSample from "./marvel0-command-hz.sample.json";

type Rec = Record<string, unknown>;

export const COMMAND = [
  "cd /path/to/openpi && \\",
  "  env -u VIRTUAL_ENV no_proxy=localhost,127.0.0.1,::1 \\",
  "  uv run --no-sync --with pyrealsense2==2.55.1.6486 python examples/fr3_local/run_robotiq_client_collect_data.py \\",
  "  --remote-host=127.0.0.1 --remote-port=8000 --view-camera-id=262622274256 --wrist-camera-id=262622275490 --franka-server-url=http://127.0.0.1:5000/ \\",
  "  '--prompt=pick the eggplant in the blue plate' \\",
  "  --max-timesteps=500 --eval-num=5 --min-z=0.025 \\",
  "  --levi-mode=off --new --no-step-by-step --rtc --rtc-execute-horizon=6",
].join("\n");

export function preview(over: Rec = {}): Rec {
  return {
    schema: "levi.marvel0.command.v1",
    ok: true,
    model: {
      id: "pi05_fr3_all_step49999",
      config: "pi05_fr3_all_state",
      restricted_prompts: false,
      verified: true,
    },
    server_requirements: {
      model_id: "pi05_fr3_all_step49999",
      config: "pi05_fr3_all_state",
      rtc: true,
    },
    cwd: "/path/to/openpi",
    env: ["no_proxy=localhost,127.0.0.1,::1"],
    argv: ["env", "-u", "VIRTUAL_ENV"],
    command: COMMAND,
    warnings: [],
    hands:
      "Keep a hand on the physical e-stop. This page never starts the arm stack or this client.",
    ...over,
  };
}

/** The `control` part of the site answer, as the backend writes it
 * (levi/marvel0/command.py `control_public`; marvel0-command-hz.sample.json is
 * written by tests/marvel0/regen_command_hz_sample.py). */
export const RATE_CONTROL = hzSample.site_control;

/** The site answer of a backend that offers control rates. */
export const siteWithRate = (over: Rec = {}): Rec =>
  siteAnswer({ control: RATE_CONTROL, ...over });

/** The cases of the backend's sample: what it works out for each rate. */
export const RATE_CASES = hzSample.cases;
