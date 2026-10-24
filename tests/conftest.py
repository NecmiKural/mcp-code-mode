"""Tests that run code need Docker and the sandbox image (`mcp-code-mode build-image`); they
skip locally without them and fail under CI."""

import os
import subprocess

import pytest

from mcp_code_mode import sandbox


def image_ready() -> bool:
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", sandbox.IMAGE],
            env=sandbox.docker_env(),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return found.returncode == 0


@pytest.fixture(scope="session")
def image():
    if not image_ready():
        if os.environ.get("CI"):
            pytest.fail(f"Docker or {sandbox.IMAGE} missing in CI")
        pytest.skip(f"Docker or {sandbox.IMAGE} not available; run `mcp-code-mode build-image`")
    yield sandbox.IMAGE
    sandbox.cleanup()
    left = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={sandbox.NAME_PREFIX}"],
        env=sandbox.docker_env(),
        capture_output=True,
        text=True,
    )
    assert left.stdout.strip() == "", "a sandbox container outlived the tests"


@pytest.fixture
def anyio_backend():
    return "asyncio"
