"""JobHarbor job data collection and storage."""

__version__ = "0.4.1"


def __getattr__(name):
    if name == "BossCli":
        from .client import BossCli
        return BossCli
    if name == "SourceStore":
        from .store import SourceStore
        return SourceStore
    raise AttributeError(name)
