// The answers of GET /api/levi/marvel0/models and /site as the backend writes them
// (levi/marvel0/models.py view(), levi/marvel0/site.py public_view()), with the real
// marvel0's values. A test replaces whole parts.
type Rec = Record<string, unknown>;

export function model(over: Rec = {}): Rec {
  return {
    id: "pi05_fr3_all_step49999",
    config: "pi05_fr3_all_state",
    cfg_capable: false,
    restricted_prompts: false,
    verified: true,
    labels: [],
    step: 49999,
    version: null,
    variant: null,
    size_bytes: 12436487155,
    warnings: ["training_config_name_differs_from_serving_config"],
    rollout_group_default: {
      rtc: "pi05_fr3_all_step49999_rtc_demo",
      plain: "pi05_fr3_all_step49999_demo",
    },
    ...over,
  };
}

export function cfgModel(over: Rec = {}): Rec {
  return model({
    id: "recap_cfg_r2_best_step14300_jax",
    config: "pi05_fr3_all_state_cfg",
    cfg_capable: true,
    verified: false,
    labels: ["unverified"],
    step: 14300,
    warnings: [],
    rollout_group_default: {
      rtc: "recap_cfg_r2_best_step14300_jax_rtc_demo",
      plain: "recap_cfg_r2_best_step14300_jax_demo",
    },
    ...over,
  });
}

export function modelsAnswer(over: Rec = {}): Rec {
  return {
    schema: "levi.marvel0.models.v1",
    models: [model(), cfgModel()],
    skipped: [
      { id: "recap_global_step_10000", reason: "not_deployable" },
      { id: "reset_r1_step20000", reason: "excluded_by_site" },
    ],
    problems: [],
    defaults: { rtc: true, fraction: 0.22, cfg_guidance_scale: 1.0 },
    ...over,
  };
}

export function siteAnswer(over: Rec = {}): Rec {
  return {
    schema: "levi.marvel0.site_view.v1",
    source: "main_checkout",
    server: {
      port: 8000,
      remote_host: "127.0.0.1",
      robot_server_url: "http://127.0.0.1:5000/",
    },
    cameras: { view: "262622274256", wrist: "262622275490" },
    home: {
      pose: [0.481902, -0.011145, 0.235951, 3.119139, 0.008288, -0.883962],
      duration_s: 3.0,
    },
    min_z: { floor: 0.025, default: 0.025 },
    caps: {
      max_steps: { default: 500, cap: 3000 },
      eval_num: { default: 5, cap: 50 },
      rtc_horizon: { default: 6, max: 8 },
      open_loop_horizon: { default: 6, max: 10 },
      prompt_max_chars: 300,
      note_max_chars: 200,
      cfg_scale: { default: 1.0, max: 1.5 },
    },
    fixed_flags: [
      "--max-xy=0.85",
      "--max-step-m=0.1",
      "--max-step-rad=0.35",
      "--rx-range 1.5 4.03",
      "--binarize-gripper",
      "--require-matching-server",
      "--no-allow-unknown-prompt",
      "--home-duration-s=3.0",
    ],
    gpu: { fraction_presets: [0.22, 0.3, 0.45], margin_mib: 1024 },
    unit: {
      nice: 10,
      cpu_affinity: "8-15",
      stop_timeout_s: 90,
      start_timeout_s: 600,
    },
    names: {
      openpi_checkout: "openpi",
      rollout_root: "online_rollout_data_test",
    },
    prompts: {
      default: "pick the eggplant in the blue plate",
      known: [
        "pick screws of same size into the empty slot of screw box",
        "stack the plates of same color together",
        "pick the eggplant in the blue plate",
      ],
      restricted: [
        "pick screws of same size into the empty slot of screw box",
        "stack the plates of same color together",
      ],
      restricted_prefixes: ["pi05_fr3_sp"],
    },
    source_hash: { manifest: false, files: 5 },
    ...over,
  };
}
