"""Reset exports: derive "put it back" episodes from forward demonstrations.

See docs/RESET_EXPORT.md. The modules, in the order data flows through them:

- ``schema``: the options, and the temporal map every derived episode keeps;
- ``contract``: what an action means, and how to rebuild it for reversed time;
- ``events`` / ``vision`` / ``analysis``: where the gripper grasps and
  releases, and whether the object is still where the gripper can take it
  again after a release (the part that decides what may be reversed);
- ``bridge``: a recorded stretch of real motion that replaces what cannot be
  reversed, and the checks that it joins;
- ``build``: the reversed episode itself, with every camera.
"""
