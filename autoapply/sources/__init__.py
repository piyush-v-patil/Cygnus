from ..config import Profile
from .base import Posting, Source, SourceError
from .csv_links import CsvSource
from .jobmonitor import JobMonitorSource


def build_sources(profile: Profile, secrets: dict[str, str]) -> list:
    out = []
    for cfg in profile.sources:
        if cfg.type in ("jobmonitor", "jobmonitor_public"):
            out.append(JobMonitorSource(cfg, secrets.get("GITHUB_TOKEN")))
        else:
            out.append(CsvSource(cfg))
    return out


__all__ = ["Posting", "Source", "SourceError", "CsvSource", "JobMonitorSource", "build_sources"]
