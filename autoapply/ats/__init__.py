from .ashby import AshbyClient
from .base import AtsClient, FetchError, JobDetails
from .detect import AtsRef, application_url, detect, normalize_url, parse_key, posting_key
from .greenhouse import GreenhouseClient
from .lever import LeverClient

CLIENTS = {"greenhouse": GreenhouseClient, "lever": LeverClient, "ashby": AshbyClient}


def clients(http=None) -> dict[str, AtsClient]:
    return {k: v(http) for k, v in CLIENTS.items()}


def form_hints(ats: str) -> dict:
    return CLIENTS[ats].form_hints if ats in CLIENTS else {}


__all__ = ["AtsRef", "AtsClient", "FetchError", "JobDetails", "application_url", "clients",
           "detect", "form_hints", "normalize_url", "parse_key", "posting_key"]
