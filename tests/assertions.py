"""Public error assertions that preserve freedom to improve diagnostic wording."""

from contextlib import contextmanager

import pytest


@contextmanager
def safe_error(kind, *, match=None):
    """Expect a first-party safe error with a real, nonempty textual diagnostic."""
    with pytest.raises(kind, match=match) as captured:
        yield captured
    assert captured.value.args, "safe errors need a user-facing diagnostic"
    assert isinstance(captured.value.args[0], str), "diagnostics must be text"
    assert captured.value.args[0].strip(), "diagnostics must explain the failure"
