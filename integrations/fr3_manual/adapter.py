"""ROS/Flask glue; imported only by an explicitly restarted shared server."""

from .fr3_manual_protocol import AsyncGoal, Protocol, install_flask


class Adapter:
    def __init__(self, robot, gripper):
        self.robot, self.gripper = robot, gripper
        self.goal = AsyncGoal()

    def sample(self):
        with self.robot.manual_sample_lock:
            return {
                "pose7": self.robot.pos.tolist(),
                "joints": self.robot.q.tolist(),
                "frame": self.robot.manual_frame,
                "seq": self.robot.pose_update_count,
                "joint_seq": self.robot.joint_state_update_count,
                "monotonic_time": self.robot.last_pose_monotonic,
                "joint_monotonic_time": self.robot.last_joint_monotonic,
            }

    def publish(self, pose):
        self.robot.move(pose)

    def legacy_idle(self):
        gripper = self.gripper
        if hasattr(gripper, "_goal_lock"):
            with gripper._goal_lock:
                if gripper._goal_in_flight or gripper._pending_goal_request is not None:
                    return False
        # Unsupported gripper backends have no trustworthy idle evidence.
        return self.gripper_supported() and self.goal.terminal

    def gripper_supported(self):
        return hasattr(self.gripper, "manual_sample_lock") and hasattr(
            self.gripper, "manual_send_ratio"
        )

    def gripper_state(self):
        if not self.gripper_supported():
            return {"supported": False, "terminal": False}
        with self.gripper.manual_sample_lock:
            return {
                "supported": True,
                "opening_ratio": self.gripper.gripper_pos,
                "theta": self.gripper.manual_theta,
                "seq": self.gripper.manual_sample_seq,
                "monotonic_time": self.gripper.manual_sample_monotonic,
                "terminal": self.goal.terminal,
                "action_status": self.goal.status,
            }

    def gripper_terminal(self):
        return self.goal.terminal

    def begin_gripper(self, ratio):
        self.goal.begin(lambda: self.gripper.manual_send_ratio(ratio))

    def cancel_gripper(self):
        self.goal.cancel()


def install(app, robot, gripper, request, jsonify):
    adapter = Adapter(robot, gripper)
    protocol = Protocol(adapter)
    app.extensions["fr3_manual_protocol"] = protocol
    app.extensions["fr3_manual_watchdog_stop"] = install_flask(
        app, protocol, request, jsonify
    )
    return protocol
