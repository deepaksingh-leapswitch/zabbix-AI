"""Root conftest — shared pytest fixtures and hooks."""
from __future__ import annotations

import warnings

import pytest

# #11: Suppress authlib.jose deprecation warning globally in tests.
# Remove once we migrate to joserfc (tracked for v1.4).
warnings.filterwarnings("ignore", message="authlib.jose module is deprecated",
                        category=DeprecationWarning)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi limiter storage between tests to prevent cross-test pollution."""
    from zabbix_ai.admin.rate_limit import limiter
    limiter.reset()
    yield
    limiter.reset()
