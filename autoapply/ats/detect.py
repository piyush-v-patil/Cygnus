"""ATS detection and posting identity (§6.2, §6.3)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SUPPORTED = ("greenhouse", "ashby", "lever", "workday")

_GH = re.compile(r"^https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?"
                 r"([^/?#]+)/jobs/(\d+)", re.I)
_GH_EMBED = re.compile(r"greenhouse\.io/embed/job_app", re.I)
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)", re.I)
_LEVER = re.compile(r"^https?://jobs(?:\.eu)?\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", re.I)
_ASHBY = re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", re.I)
_LINKEDIN = re.compile(r"^https?://([a-z]+\.)?linkedin\.com/", re.I)
_WD_HOST = re.compile(r"^(?:([a-z0-9-]+)\.)?(wd\d+)\.(myworkdayjobs|myworkdaysite)\.com$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")

# query params that carry a posting's identity and therefore survive normalisation
ID_PARAMS = {"gh_jid", "jobid", "job_id", "id", "jid", "for", "token"}


@dataclass(frozen=True)
class AtsRef:
    ats: str                 # greenhouse | lever | ashby | workday | unsupported | linkedin
    board_token: str | None
    job_id: str | None

    @property
    def supported(self) -> bool:
        return self.ats in SUPPORTED and bool(self.board_token and self.job_id)


def detect(url: str, company: str = "", gh_tokens: dict[str, str] | None = None) -> AtsRef:
    """Work out which ATS hosts `url`.

    `gh_tokens` maps lower-cased company name -> Greenhouse board token (from
    job-monitor's config/companies.yaml) and resolves `?gh_jid=` links that sit
    on a company's own careers site.
    """
    url = (url or "").strip()
    if _LINKEDIN.match(url):
        return AtsRef("linkedin", None, None)
    m = _GH.match(url)
    if m:
        return AtsRef("greenhouse", m.group(1).lower(), m.group(2))
    if _GH_EMBED.search(url):
        q = dict(parse_qsl(urlsplit(url).query))
        if q.get("for") and q.get("token"):
            return AtsRef("greenhouse", q["for"].lower(), q["token"])
    m = _GH_JID.search(url)
    if m:
        token = (gh_tokens or {}).get(company.strip().lower())
        if token:
            return AtsRef("greenhouse", token.lower(), m.group(1))
        return AtsRef("unsupported", None, m.group(1))
    m = _LEVER.match(url)
    if m:
        return AtsRef("lever", m.group(1).lower(), m.group(2).lower())
    m = _ASHBY.match(url)
    if m:
        return AtsRef("ashby", m.group(1).lower(), m.group(2).lower())
    wd = _workday(url)
    if wd:
        return wd
    return AtsRef("unsupported", None, None)


def _workday(url: str) -> AtsRef | None:
    """Workday: board_token = "<tenant>@<host>", job_id = "<site>/<job slug>".

    The location segment of /job/<location>/<slug> is not part of the identity:
    Workday answers the same posting for any value (VERIFIED 2026-10-23).
    myworkdaysite.com puts the tenant in the path: /recruiting/<tenant>/<site>/...
    """
    parts = urlsplit(url)
    m = _WD_HOST.match(parts.netloc.lower())
    if not m:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if segs and _LOCALE.match(segs[0]):
        segs = segs[1:]
    tenant = m.group(1)
    if m.group(3).lower() == "myworkdaysite":
        if len(segs) < 2 or segs[0].lower() != "recruiting":
            return None
        tenant, segs = segs[1], segs[2:]
    if not tenant or len(segs) < 3 or segs[1] not in ("job", "details"):
        return None
    rest = segs[2:]
    if "apply" in rest:
        rest = rest[:rest.index("apply")]
    if not rest:
        return None
    return AtsRef("workday", f"{tenant.lower()}@{parts.netloc.lower()}", f"{segs[0]}/{rest[-1]}")


def workday_parts(ref: AtsRef) -> tuple[str, str, str, str]:
    """(tenant, host, site, job slug) of a Workday ref."""
    tenant, _, host = (ref.board_token or "").partition("@")
    site, _, slug = (ref.job_id or "").partition("/")
    return tenant, host, site, slug


def workday_board_url(ref: AtsRef) -> str:
    """https://<host>/<site> (or /recruiting/<tenant>/<site> on myworkdaysite.com)."""
    tenant, host, site, _ = workday_parts(ref)
    if "myworkdaysite" in host:
        return f"https://{host}/recruiting/{tenant}/{site}"
    return f"https://{host}/{site}"


def normalize_url(url: str) -> str:
    """Lowercase host, drop non-identity query params, fragment and trailing slash."""
    parts = urlsplit((url or "").strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() in ID_PARAMS))
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, query, ""))


def posting_key(ref: AtsRef, url: str) -> str:
    if ref.supported:
        return f"{ref.ats}:{ref.board_token}:{ref.job_id}"
    return "url:" + normalize_url(url)


def application_url(ref: AtsRef) -> str:
    """The page that holds the application form. VERIFIED 2026-10-20 against live boards."""
    if ref.ats == "greenhouse":
        return f"https://job-boards.greenhouse.io/{ref.board_token}/jobs/{ref.job_id}"
    if ref.ats == "lever":
        return f"https://jobs.lever.co/{ref.board_token}/{ref.job_id}/apply"
    if ref.ats == "ashby":
        return f"https://jobs.ashbyhq.com/{ref.board_token}/{ref.job_id}/application"
    if ref.ats == "workday":
        # straight to "Apply Manually" (skips the Start Your Application popup)
        return f"{workday_board_url(ref)}/job/{workday_parts(ref)[3]}/apply/applyManually"
    raise ValueError(f"no application page for {ref.ats}")


def parse_key(key: str) -> AtsRef:
    ats, _, rest = key.partition(":")
    if ats in SUPPORTED:
        token, _, job_id = rest.partition(":")
        return AtsRef(ats, token, job_id)
    return AtsRef("unsupported", None, None)
