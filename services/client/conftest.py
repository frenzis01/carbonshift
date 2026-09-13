"""Test-only environment overrides — must run before `app.config` is
imported, so pytest's automatic conftest discovery does this for us.
"""
import os
import tempfile

os.environ.setdefault("CLIENT_METRICS_PATH", os.path.join(tempfile.mkdtemp(), "metrics.jsonl"))
os.environ.setdefault("CLIENT_CALLBACK_TIMEOUT_SECONDS", "120")
