"""Test-only environment overrides — must run before `app.config` (and
anything importing it) is loaded, so pytest's automatic conftest discovery
does this for us.
"""
import os
import tempfile

os.environ.setdefault("EXECUTOR_METRICS_PATH", os.path.join(tempfile.mkdtemp(), "metrics.jsonl"))
os.environ.setdefault("EXECUTOR_POLL_INTERVAL_SECONDS", "0.05")
