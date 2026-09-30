# Annotation guideline: generic robot manipulation (live service)

Version 1 (2026-10-01). Task-independent: the only task knowledge it gives a
model is the instruction the robot was given, quoted below. Written from the
plates and screws guidelines with every object, colour and location taken out.
NOT YET EVALUATED on any task: treat its output as automatic and unreviewed.

## Task
The robot's instruction for this episode is:

> {task}

A robot arm with a parallel two-finger gripper carries out that instruction.
Decide what the instruction asks for (which object to pick, where it should
end up) before you look at the frames, and judge every step against it. You
are shown the side camera (`view1`): it shows where the arm is and where the
objects are in the workspace. (A run may also attach the wrist camera, `hand`,
which looks down between the fingers and shows what is held and what lies
under it.)

## Output
For every episode, a list of subtask segments that covers the whole episode
from its first frame to its last, in time order, without overlaps:

- `start`, `end` in seconds, `[start, end)`, on the time scale of the
  request (the last segment ends at the episode's last frame);
- `subtask`: one of the ids below;
- `outcome`: `success`, `failure` or `unknown`;
- `description`: one sentence saying what the robot did (which object, where
  it went). No frame numbers, no reasoning.

## Subtasks
| id | starts when | ends when | success when |
|---|---|---|---|
| approach | the empty gripper starts moving toward an object | the fingertips reach the object | the object is one the instruction asks the robot to pick |
| grasp | the fingertips reach the object | the object first moves with the gripper | the object is lifted between the fingertips |
| transport | the object leaves its place | the gripper stops above the place where it will open | that place is the target the instruction names |
| place | the object is lowered or held above the target | the fingers open | the released object rests on or in the target the instruction names |
| retreat | the fingers open | the gripper has moved clear of the target | what was placed is not disturbed |
| other | a human hand acts, the robot is idle, or nothing recognisable happens | - | outcome is always `unknown` |

## How to work through an episode (do this before writing segments)
1. Find every closing and every opening of the fingers. Each closing is a
   grasp attempt, each opening a release.
2. At each closing, look at the wrist camera as the gripper rises: did an
   object leave its place between the fingertips, and which object was it?
3. At each opening, look just after it: where did the object land?
4. Every object move is a cycle `approach -> grasp -> transport -> place ->
   retreat`. A grasp that lifts nothing is `approach -> grasp` followed by
   the next `approach` (or `other`), with no transport or place. Annotate
   only the moves you can see.
5. Only then write the segments, one cycle after another, covering the whole
   episode without gaps.

## Rules
1. The instruction decides success. Picking or placing the wrong object, or
   placing the right object in the wrong place, is `failure` for that subtask
   even if the motion looks clean.
2. If the release is not visible (the recording ends while hovering or
   descending), `place` is `unknown`, not `success`.
3. Judge `place` on the frames just AFTER the fingers open: success only if
   the released object is seen resting on or in the target. Half on it, on
   something else or on the table is `failure`.
4. Anything a human hand does is `other`, never a robot subtask. When the
   robot and a hand both act, annotate the robot.
5. A grasp where the fingers close and reopen, or where the object stays in
   place, is `grasp` with `failure` (clearly never lifted) or `unknown`,
   followed directly by the next `approach`. Do not complete a cycle by
   assumption: write `transport` and `place` only when later frames show the
   object off its place and moving.
6. One episode may contain several attempts; annotate each of them.
7. Short events (the lift, the release) last a fraction of a second; check
   frames densely before deciding something did not happen.
8. Every segment has `end` > `start` and lasts at least 0.2 s; never write a
   zero-length segment to mark a boundary.
9. An episode with too few frames to see anything is one `other` segment.
