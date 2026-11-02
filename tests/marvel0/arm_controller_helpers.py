"""Shared values of the arm controller tests (``test_marvel0_arm_controller_*.py``):
the stack control the page would read, the anyspeedup values, and where the
anyspeedup checkout is when it is beside this one."""

from pathlib import Path

from stack_act_machine import POSE

from levi.marvel0 import control_site as cs
from levi.marvel0 import stack

ROOT = Path(stack.__file__).resolve().parents[2]
ANY_ROOT = "/opt/rig/anyspeedup"
LAB_ENV = "/opt/rig/franka_control/env/fr3_env.sh"
ANY_ENV = f"{ANY_ROOT}/controllers/fr3_serl/env.sh"
LAB_A = (
    "ros2",
    "launch",
    "--noninteractive",
    "serl_franka_controllers_ros2",
    "impedance.launch.py",
    "robot_type:=fr3",
    "robot_ip:=10.42.0.157",
    "load_gripper:=false",
)


def control(**kw):
    values = {
        "enabled": True,
        "franka_control": "/opt/rig/franka_control",
        "robot_ip": "10.42.0.157",
        "com_port": "/dev/serial/by-id/usb-FTDI_USB_TO_RS-485_DABBX310-if00-port0",
        "reset_joint_target": tuple(POSE),
        **kw,
    }
    return cs.StackControl(**values)


def anyspeedup(preset="today", **kw):
    values = {"anyspeedup_root": ANY_ROOT, **kw}
    return control(arm_controller="anyspeedup", arm_preset=preset, **values)


def anyspeedup_checkout():
    """The anyspeedup checkout's ``controllers/fr3_serl`` when it is beside the
    LEVI checkout (outside this repository), else None."""
    for parent in ROOT.parents:
        for guess in (parent / "anyspeedup", parent / "wenkai" / "anyspeedup"):
            folder = guess / "controllers" / "fr3_serl"
            if (folder / "presets.py").is_file():
                return folder
    return None
