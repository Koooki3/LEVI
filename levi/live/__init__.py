"""Live annotation service: a long-running background LEVI that labels robot
rollouts as an evaluation writes them.

The pieces (see ``docs/LIVE.md``):

- ``config``      one TOML file, every default in one place
- ``criteria``    when a rollout directory counts as finished
- ``mirror``      hard-links finished rollouts into the service's workspace
- ``sessions``    evaluation-session and FR3-health files written by the robot side
- ``gpumgr``      when the local model may use the GPU, and its vLLM server
- ``worker``      one dataset's batch: plan, approve, run, validate, commit
- ``auto``        the explicit, audited automatic approver
- ``controller``  the small supervisor loop (``levi live start``)
- ``api``         read-only HTTP views for the live page

Everything the idle supervisor needs is standard library only: importing this
package must not load pandas, numpy, OpenCV or pydantic (``resources`` tests
that).
"""
