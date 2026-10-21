# A stand-in for franka_control/env/fr3_env.sh: what it exports, and the
# colour switch the real one forces on. It records how many positional
# parameters it was sourced with.
export FAKE_ROS_SOURCED=1
export ROS_DOMAIN_ID=17
export RCUTILS_COLORIZED_OUTPUT=1
export FAKE_ENV_ARGC=$#
echo "[fr3_env] sourced (a line the wrapper must not pass on)"
