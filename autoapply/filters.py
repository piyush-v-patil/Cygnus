"""Rule-based prefilter (§6.4). No network, no LLM: runs before anything that costs.

The US-location rules are ported from job-monitor's monitor/filters.py so the
two tools agree about what "US" means; keep them in sync when that file changes.
"""
from __future__ import annotations

import re

from .ats.detect import AtsRef
from .config import Profile
from .h1b import H1BIndex
from .sources.base import Posting

# ---- US location (from job-monitor monitor/filters.py) ----------------------
US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS"
    "|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_HINT = re.compile(
    rf"united states|\busa?\b|,\s*({US_STATES})\b|\b({US_STATES})\s*,?\s*(us|usa)?$"
    r"|new york|san francisco|seattle|austin|boston|chicago|los angeles|denver"
    r"|atlanta|dallas|houston|miami|phoenix|portland|philadelphia|washington"
    r"|bellevue|redmond|mountain view|sunnyvale|palo alto|menlo park|cupertino"
    r"|san jose|santa clara|irvine|san diego|cambridge|pittsburgh|raleigh"
    r"|nashville|salt lake|remote.*(us|america)|us.*remote"
    r"|\bu\.s\.(a\.?)?"
    r"|\bsf\b|\bnyc\b|\bd\.?c\.?\b|bay area|silicon valley"
    r"|brooklyn|manhattan|oakland|berkeley|san mateo|foster city|burlingame"
    r"|arlington|reston|herndon|mclean|bethesda|boulder|fort collins|ann arbor"
    r"|madison|minneapolis|st\.? louis|kansas city|columbus|cincinnati|cleveland"
    r"|detroit|indianapolis|charlotte|durham|chapel hill|orlando|tampa|jacksonville"
    r"|san antonio|el segundo|hawthorne|torrance|pasadena|culver city|santa monica"
    r"|long beach|sacramento|san bruno|redwood city|milpitas|fremont|plano|richardson"
    r"|round rock|provo|lehi|boise|omaha|tucson|albuquerque|las vegas|reno|spokane"
    r"|tacoma|kirkland|renton|everett|hillsboro|beaverton",
    re.I,
)
AMBIGUOUS_LOC = re.compile(
    r"^\s*(n/?a|none|null|-|remote|remote friendly|anywhere|flexible|multiple"
    r"|multiple locations?|various( locations?)?|\d+\s*locations?|other)\s*$", re.I)
NON_US = re.compile(
    r"canada|toronto|vancouver|london|dublin|india|bangalore|bengaluru|hyderabad"
    r"|singapore|tokyo|japan|china|shanghai|beijing|germany|berlin|munich|paris"
    r"|france|israel|tel aviv|australia|sydney|mexico|brazil|poland|warsaw"
    r"|netherlands|amsterdam|spain|madrid|zurich|switzerland|korea|seoul|taiwan"
    r"|ireland|uk\b|united kingdom|england|scotland|wales|remote.*(emea|apac|latam)"
    r"|dhaka|bangladesh|karachi|lahore|colombo|kathmandu|hanoi|ho chi minh"
    r"|jakarta|manila|bangkok|kuala lumpur|vietnam|thailand|philippines"
    r"|istanbul|cairo|nairobi|lagos|casablanca|johannesburg|riyadh|dubai"
    r"|athens|lisbon|oslo|copenhagen|stockholm|helsinki|vienna|brussels"
    r"|budapest|prague|bucharest|sofia|santiago|bogota|bogotá|lima\b"
    r"|buenos aires|montevideo|san jose, costa rica|panama city, panama", re.I)
TRAILING_NON_US = re.compile(
    r",\s*(canada|can|gbr|uk|united kingdom|england|scotland|wales|ireland|irl"
    r"|india|ind|israel|isr|germany|deu|france|fra|spain|esp|italy|ita|poland|pol"
    r"|netherlands|nld|sweden|swe|switzerland|che|australia|aus|new zealand|nzl"
    r"|japan|jpn|china|chn|singapore|sgp|korea|kor|taiwan|twn|brazil|bra|mexico"
    r"|mex|argentina|arg|colombia|col|romania|rou|portugal|prt|denmark|dnk"
    r"|norway|nor|finland|fin|austria|aut|belgium|bel|czechia|cze|hungary|hun"
    r"|greece|grc|turkey|tur|ukraine|ukr|philippines|phl|indonesia|idn|malaysia"
    r"|mys|vietnam|vnm|thailand|tha|south africa|zaf|egypt|nigeria|kenya)\s*$", re.I)
CA_PROVINCE = re.compile(r"\b(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\s*,\s*CA\b")
LOC_SPLIT = re.compile(r"\s*[;|]\s*|\s+/\s+")
BARE_TRAILING_CODE = re.compile(rf"(?<!,)\s+({US_STATES})$")
REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere", re.I)


def _site_is_us(loc: str) -> bool:
    if not loc or AMBIGUOUS_LOC.match(loc):
        return True
    loc = CA_PROVINCE.sub("Canada", loc)
    if TRAILING_NON_US.search(loc):
        return False
    if NON_US.search(loc) and not US_HINT.search(BARE_TRAILING_CODE.sub("", loc)):
        return False
    return bool(US_HINT.search(loc))


def is_us(location: str) -> bool:
    """US, or plausibly US (an unknown location is kept, not dropped)."""
    loc = (location or "").strip()
    if not loc or AMBIGUOUS_LOC.match(loc):
        return True
    return any(_site_is_us(p.strip()) for p in LOC_SPLIT.split(loc) if p.strip())


def location_ok(location: str, mode: str, metros: list[str]) -> bool:
    loc = location or ""
    if mode == "us_or_remote":
        return is_us(loc)
    if mode == "remote_only":
        return bool(REMOTE.search(loc)) and is_us(loc)
    if mode == "metros":
        low = loc.lower()
        if REMOTE.search(loc) and is_us(loc):
            return True
        return any(m.strip().lower() in low for m in metros if m.strip())
    return True


# ---- the prefilter ---------------------------------------------------------
def prefilter(p: Posting, ref: AtsRef, profile: Profile, h1b: H1BIndex | None) -> tuple[str, str] | None:
    """Return (state, reason) to stop the posting here, or None to let it through.

    The ledger-state check (§6.4.1) is done by the caller, which owns the ledger.
    """
    f = profile.filters
    if ref.ats == "linkedin":
        return ("unsupported", "linkedin")
    if not ref.supported:
        return ("unsupported", "ats_unsupported")
    if p.monitor_status is not None and p.monitor_status != "new":
        return ("filtered_out", f"monitor_{p.monitor_status}")
    if p.closed_at:
        return ("filtered_out", "monitor_closed")
    if ref.ats not in f.ats_allowed:
        return ("filtered_out", "ats_not_allowed")
    if p.tier and p.tier not in f.tiers:
        return ("filtered_out", "tier")
    if p.role and p.role not in f.roles:
        return ("filtered_out", "role")
    if not location_ok(p.location, f.location, f.metros):
        return ("filtered_out", "location")
    if h1b is not None and not h1b.qualifies(p.company, profile.visa.sponsor_filter):
        return ("skipped", "no_h1b_record")
    return None
