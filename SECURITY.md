# Local deployment model

LEVI can read registered dataset files and launch an allowlisted set of local conversion scripts. It is intended for one trusted user on loopback or behind authenticated access control. Hugging Face login does not authenticate access to LEVI itself.

Paths are resolved and confined to the workspace; dataset file routes are additionally confined to registered roots. Source-safe exports use new directories. Browser cross-origin writes are rejected. This does not provide isolation from another process or user that already has write access to the workspace.

HF tokens are held in browser local storage and an HttpOnly cookie for the media proxy; sign out on shared devices. Server-side `HF_TOKEN` is an alternative for downloads. Enable secure cookies only on HTTPS deployments. Never publish `.env`, runtime catalogs, dataset paths, logs or real credentials.

Report a security problem privately to the repository maintainer using the contact mechanism they configure on GitHub. Do not include credentials or private captures in a public issue. This repository does not invent a contact address or guarantee a response SLA.
