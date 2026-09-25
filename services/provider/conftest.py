"""Test-only environment overrides — must run before `app.config` (and
anything importing it) is loaded, so pytest's automatic conftest discovery
does this for us.

Mirrors `client/conftest.py` and `executor/conftest.py`: settings are read at
import time, so the ordering here is deliberate, not incidental.
"""
import os

os.environ.setdefault("PROVIDER_MANUAL_CLOCK", "1")
# Never let the auto-advance thread run during tests: it would move the slot
# underneath a test that is asserting on it.
os.environ.setdefault("PROVIDER_AUTO_ADVANCE_CLOCK", "0")
os.environ.setdefault("PROVIDER_ROLE", "local")
os.environ.setdefault("PROVIDER_SLOT_MINUTES", "30")
# No peer services are running under pytest; tests that exercise the fan-out
# inject a fake `post` instead.
os.environ.setdefault("EXECUTOR_URL", "")
