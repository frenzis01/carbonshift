from contextlib import contextmanager
import config


@contextmanager
def config_override(**kwargs):
    """Temporarily override config attributes and restore them on exit."""
    saved = {k: getattr(config, k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
