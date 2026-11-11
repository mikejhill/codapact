"""Suite markers come from the directory a test lives in.

``tests/unit`` → ``unit``, ``tests/integration`` → ``integration``, and so on, so
``pytest -m unit`` selects a suite without every file repeating its marker.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

SUITES = ("unit", "integration", "e2e", "perf")
ROOT = Path(__file__).parent

# CI runs more examples; local runs stay fast. ``HYPOTHESIS_PROFILE=ci`` selects it.
settings.register_profile("dev", max_examples=50)
settings.register_profile(
    "ci", max_examples=300, suppress_health_check=[HealthCheck.too_slow], print_blob=True
)
settings.register_profile("deep", max_examples=3000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


LIVE = os.environ.get("CODAPACT_LIVE") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_live = pytest.mark.skip(reason="calls public APIs; set CODAPACT_LIVE=1 to run")
    for item in items:
        parts = item.path.relative_to(ROOT).parts
        if parts and parts[0] in SUITES:
            item.add_marker(getattr(pytest.mark, parts[0]))
        if "live" in item.keywords and not LIVE:
            item.add_marker(skip_live)


PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY")


@pytest.fixture(autouse=True)
def _no_ambient_proxy(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hermetic tests independent of the machine's proxy settings.

    Live tests keep them: on a proxied network they are the only way out.
    """
    if "live" in request.keywords:
        return
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
