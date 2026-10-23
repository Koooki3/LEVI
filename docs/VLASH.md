# VLASH inference mode

The workstation offers **Plain / RTC / VLASH** as mutually exclusive modes. The selection follows through policy deployment, client command preview, service matching and evaluation. Existing requests with only the RTC boolean retain their original meaning. Conflicting fields are refused.

VLASH is an experimental openpi adaptation of future-state-aware asynchronous chunk scheduling. It requires a reviewed state-conditioned FR3 absolute pose/gripper configuration; it does not imply that ordinary weights were delay-augmented during training. Only VLASH uses the overlap setting; the default is 2 steps and it must be smaller than the action horizon.

Install the isolated runtime with [the adapter instructions](../integrations/vlash/README.md). The required explicit client hooks preserve the existing safety checks, operator gates and recorder. Each episode owns and closes its scheduler, and diagnostics use an independent `vlash` field. Plain servers cannot silently satisfy VLASH requests. The interface never starts a new mode inside an active episode.

Action quantization, automatic training, simultaneous RTC+VLASH and autonomous robot motion are outside this integration. Existing checkpoints must pass the wire-contract checks; physical performance remains unverified until operator acceptance.

This version supports policy deployment and operator-controlled evaluation in workstation steps 2–4. Step 5 (AERI) does not yet accept VLASH; its closed schema rejects the mode instead of silently falling back to plain inference.
