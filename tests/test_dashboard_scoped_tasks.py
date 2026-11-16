"""Run the shipped dashboard card against a dependency-free SDK harness."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_scoped_dashboard_card():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for dashboard bundle behavior")
    subprocess.run([node, str(Path(__file__).with_name("dashboard_scoped_tasks.cjs"))], check=True, timeout=20)
