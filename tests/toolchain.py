"""Compatibility exports for optional external hardware toolchains."""

from zlang.toolchain import clash_subprocess_environment, find_clash_executable


CLASH_EXECUTABLE = find_clash_executable()
CLASH_ENVIRONMENT = clash_subprocess_environment(CLASH_EXECUTABLE)
