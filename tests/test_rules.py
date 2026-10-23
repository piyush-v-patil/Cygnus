"""Prefilter, location, H-1B three-state logic, blocker regexes (§6.4, §6.5, §7.1)."""
import pytest

from autoapply.ats.detect import detect
from autoapply.blockers import confident_block, scan
from autoapply.config import Profile
from autoapply.filters import is_us, location_ok, prefilter
from autoapply.h1b import H1BIndex
from autoapply.sources.base import Posting

from .conftest import base_profile


def P(**kw):
    d = dict(company="Acme", title="SWE", apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
             location="Austin, TX", tier="newgrad", role="software", monitor_status="new")
    d.update(kw)
    return Posting(**d)


def run(p, profile=None, h1b=None):
    prof = Profile.model_validate(profile or base_profile())
    return prefilter(p, detect(p.apply_url, p.company), prof, h1b)


def test_prefilter_passes_a_good_posting():
    assert run(P()) is None


@pytest.mark.parametrize("kw,expected", [
    (dict(apply_url="https://www.linkedin.com/jobs/view/1"), ("unsupported", "linkedin")),
    (dict(apply_url="https://x.wd5.myworkdayjobs.com/a"), ("unsupported", "ats_unsupported")),
    (dict(monitor_status="applied"), ("filtered_out", "monitor_applied")),
    (dict(closed_at="2026-10-19"), ("filtered_out", "monitor_closed")),
    (dict(tier="intern"), ("filtered_out", "tier")),
    (dict(role="security"), ("filtered_out", "role")),
    (dict(location="Toronto, ON, CA"), ("filtered_out", "location")),
])
def test_prefilter_reasons(kw, expected):
    assert run(P(**kw)) == expected


def test_ats_allowed():
    prof = base_profile(filters={"ats_allowed": ["lever"]})
    assert run(P(), prof) == ("filtered_out", "ats_not_allowed")


def test_csv_rows_without_tier_or_role_pass():
    assert run(P(tier=None, role=None, monitor_status=None)) is None


@pytest.mark.parametrize("loc,us", [
    ("San Francisco, California, United States", True), ("Remote US", True), ("Remote", True),
    ("", True), ("New York, NY; London, UK", True), ("London, UK", False), ("Bengaluru, India", False),
    ("Vienna, VA", True), ("Cambridge, England, GBR", False), ("Toronto, ON, CA", False),
    ("Remote - EMEA", False), ("SF Office", True),
])
def test_is_us(loc, us):
    assert is_us(loc) is us


def test_location_modes():
    assert location_ok("Remote US", "remote_only", [])
    assert not location_ok("Austin, TX", "remote_only", [])
    assert location_ok("Seattle, WA", "metros", ["Seattle", "Austin"])
    assert not location_ok("Denver, CO", "metros", ["Seattle"])
    assert location_ok("Remote - US", "metros", ["Seattle"])


# --- H-1B: key missing / null / filed -------------------------------------
H1B = {"companies": {"Known": {"filed": 5}, "Zero": {"filed": 0}, "NoRecord": None, "Acme Inc.": {"filed": 3}}}


@pytest.mark.parametrize("company,mode,ok", [
    ("Unknown Co", "skip_no_record", True),     # never looked up -> qualifies
    ("NoRecord", "skip_no_record", False),      # looked up, nothing found
    ("Zero", "skip_no_record", False),
    ("Known", "skip_no_record", True),
    ("Unknown Co", "known_filers_only", False),
    ("Known", "known_filers_only", True),
    ("NoRecord", "off", True),
    ("acme", "skip_no_record", True),           # suffix / case tolerant
])
def test_h1b_states(company, mode, ok):
    assert H1BIndex(H1B).qualifies(company, mode) is ok


def test_h1b_state_names():
    idx = H1BIndex(H1B)
    assert [idx.state(c) for c in ("x", "NoRecord", "Zero", "Known")] == ["unknown", "no_record", "zero_filed", "filer"]


def test_prefilter_sponsor_filter():
    p = P(company="NoRecord")
    assert run(p, h1b=H1BIndex(H1B)) == ("skipped", "no_h1b_record")
    prof = base_profile(visa={"sponsor_filter": "off"})
    assert run(p, prof, H1BIndex(H1B)) is None


@pytest.mark.parametrize("company,skipped", [
    ("Anduril Industries", True), ("SpaceX", True), ("CACI International Inc", True),
    ("BAE Systems, Inc.", True), ("Pratt & Whitney", True), ("The Aerospace Corporation", True),
    ("Boeing", False), ("Palantir", False), ("SpaceXAI", False), ("Acme", False),
])
def test_us_person_companies_skipped_before_anything_costs(company, skipped):
    assert (run(P(company=company)) == ("skipped", "us_person_company")) is skipped


def test_us_person_company_list_is_adjustable_and_off_for_citizens():
    prof = base_profile(visa={"us_person_companies_add": ["Acme Defense"], "us_person_companies_remove": ["SpaceX"]})
    assert run(P(company="Acme Defense, Inc."), prof) == ("skipped", "us_person_company")
    assert run(P(company="SpaceX"), prof) is None
    assert run(P(company="Anduril"), base_profile(visa={"skip_us_person_roles": False})) is None


# --- blocker regexes -------------------------------------------------------
@pytest.mark.parametrize("text,reason", [
    ("Applicants must be a U.S. citizen due to contract requirements.", "us_person_required"),
    ("U.S. citizenship is required for this position.", "us_person_required"),
    ("Must have an active Secret clearance.", "us_person_required"),
    ("Ability to obtain and maintain a TS/SCI clearance.", "us_person_required"),
    ("We are unable to sponsor visas for this role.", "no_sponsorship"),
    ("Visa sponsorship is not available for this position.", "no_sponsorship"),
    ("Candidates must not require sponsorship now or in the future.", "no_sponsorship"),
    ("This company does not provide visa sponsorship.", "no_sponsorship"),
])
def test_confident_blockers(text, reason):
    hit = confident_block(scan(text))
    assert hit is not None and hit.reason == reason


@pytest.mark.parametrize("text", [
    "We build ITAR-adjacent tooling for aerospace customers.",
    "Visa sponsorship available for exceptional candidates.",
    "Security clearance is a plus.",
    "EEO statement: all U.S. persons are encouraged to apply.",
])
def test_ambiguous_blockers_go_to_claude(text):
    hits = scan(text)
    assert hits and confident_block(hits) is None


def test_no_blockers_in_plain_posting():
    assert scan("We are hiring backend engineers to build Python services.") == []


def test_sponsorship_patterns_skipped_for_citizens():
    assert scan("We are unable to sponsor visas.", check_sponsorship=False) == []


@pytest.mark.parametrize("q,citizen,opt", [
    ("Are you legally authorized to work in the United States without the need for employer sponsorship?",
     ("yes", True), ("no", False)),               # OPT: unclear (yes today, no later) - flagged, not changed
    ("Will you be able to work in the US now and in the future without visa sponsorship?", ("yes", True), ("no", True)),
    ("Will you now or in the future require sponsorship for employment visa status?", ("no", True), ("yes", True)),
    ("Do you currently require visa sponsorship?", ("no", True), ("no", True)),
    ("Do you require visa sponsorship?", ("no", True), ("yes", False)),
    ("Are you legally authorized to work in the United States?", ("yes", True), ("yes", True)),
    ("What is your favourite language?", (None, False), (None, False)),
])
def test_sponsorship_questions_read_for_time_frame_and_negation(q, citizen, opt):
    from autoapply.browser.fill import legal_answer
    cit = Profile.model_validate(base_profile(visa={"status": "citizen_gc", "needs_sponsorship_future": False,
                                                    "confirmed": True}))
    o = Profile.model_validate(base_profile(visa={"status": "opt_stem_opt", "needs_sponsorship_future": True,
                                                  "confirmed": True}))
    assert legal_answer(q, cit) == citizen
    assert legal_answer(q, o) == opt


def test_unclear_sponsorship_answer_is_flagged_not_changed():
    from autoapply.browser.fields import Field
    from autoapply.browser.fill import legal_mismatches, legal_note
    o = Profile.model_validate(base_profile(visa={"status": "opt_stem_opt", "needs_sponsorship_future": True,
                                                  "confirmed": True}))
    f = Field(key="id:q", label="Do you require visa sponsorship?", kind="select", options=["Yes", "No"],
              value="No", filled_by_simplify=True)
    assert legal_mismatches([f], o) == []                     # not "corrected"
    assert "does not say whether it means now or in the future" in legal_note(f, o)
    clear = Field(key="id:r", label="Will you now or in the future require sponsorship?", kind="select",
                  options=["Yes", "No"], value="No", filled_by_simplify=True)
    assert legal_note(clear, o) is None and legal_mismatches([clear], o) == [(clear, "yes")]


def test_spelled_out_us_states_are_us():
    from autoapply.filters import is_us
    for loc in ("Dublin, Ohio", "Bloomington, Indiana", "Santa Fe, New Mexico", "Remote - Indiana"):
        assert is_us(loc), loc
    for loc in ("Dublin, Ireland", "Bangalore, India", "Mexico City, Mexico"):
        assert not is_us(loc), loc


def test_h1b_list_missing_vs_unreadable(tmp_path):
    from autoapply.config import SourceCfg
    from autoapply.github import GitHubError
    from autoapply.sources import JobMonitorSource, SourceError

    class GH:
        def __init__(self, status):
            self.status = status

        def read_raw(self, repo, path, branch):
            raise GitHubError(f"HTTP {self.status}", self.status)

    cfg = SourceCfg(type="jobmonitor", repo="me/job-monitor")
    assert JobMonitorSource(cfg, None, github=GH(404)).h1b() is None        # the repo has none
    with pytest.raises(SourceError, match="H-1B list"):
        JobMonitorSource(cfg, None, github=GH(503)).h1b()
    bad = tmp_path / "h1b.json"
    bad.write_text("{truncated")
    with pytest.raises(SourceError, match="H-1B list"):
        JobMonitorSource(SourceCfg(type="jobmonitor", repo="me/jm", h1b_path=str(bad)), None).h1b()
