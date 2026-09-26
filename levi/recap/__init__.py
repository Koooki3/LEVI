"""RECAP value model inside LEVI: per-frame values and advantage labels.

A RECAP value model (RLinf's ``ValueCriticModel``) predicts V(o_t) in
[-1, 0] for every frame; LEVI turns the values into RLinf's N-step advantage
and a positive/negative label per frame, stores them per dataset and serves
them to the annotation timeline. See docs/RECAP.md.

- ``checkpoints``: the workspace checkpoint store and its manifests;
- ``advantage``: RLinf's advantage formula and threshold rules (numpy only);
- ``store``: revisioned per-dataset results;
- ``jobs``: the worker plan, process lifecycle and publication.

The core stays model-free: inference runs in ``integrations/recap_value``
(its own uv environment); the ``fake`` provider runs with LEVI's own Python.
"""
