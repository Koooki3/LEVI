"""Run the offline shared-server contract regressions in the normal CI suite."""

from integrations.fr3_manual.test_install import InstallerTests
from integrations.fr3_manual.test_protocol import ActionTests, Tests

__all__ = ["ActionTests", "InstallerTests", "Tests"]
