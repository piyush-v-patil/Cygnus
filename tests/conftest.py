import json
from pathlib import Path

import pytest
import yaml

from autoapply.config import Paths, Profile
from autoapply.context import Context
from autoapply.llm import FakeTransport

FIXTURES = Path(__file__).parent / "fixtures"
CHROMIUM = "/opt/pw-browsers/chromium"

RESUME = """Jane Doe
jane@example.com | github.com/janedoe
EXPERIENCE
Software Engineer, Acme Corp (2023 - present)
- Built Python and Go microservices handling 2M requests/day
- 3 years of Python experience building backend APIs
EDUCATION
B.S. Computer Science, State University, 2023
"""


def base_profile(**over) -> dict:
    d = {
        "sources": [{"type": "jobmonitor", "path": str(FIXTURES / "jobs.json"), "repo": "me/job-monitor",
                     "writeback": True, "companies_path": str(FIXTURES / "companies.yaml")}],
        "filters": {"tiers": ["newgrad", "experienced"],
                    "roles": ["software", "backend", "fullstack", "frontend", "ml-ai", "data"]},
        "visa": {"status": "opt_stem_opt", "needs_sponsorship_future": True, "confirmed": True},
        "resumes": {"default": "general.txt", "map": {"backend": "backend.txt"}},
        "browser": {"parallel_tabs": 2, "executable_path": CHROMIUM if Path(CHROMIUM).exists() else None,
                    "autofill_timeout_s": 5, "page_timeout_s": 15},
        "llm": {"monthly_budget_usd": 5},
        # tests seed several postings per company; the per-run company limit has its own tests
        "limits": {"per_company_per_run": None},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def default_responder(params: dict):
    """Recorded-style answers keyed by purpose; tests override as needed."""
    system = params["system"][0]["text"]
    if "rate how good a fit" in system:
        return {"score": 80, "role_family": "backend", "reasons": ["Python backend match"],
                "blockers": [], "yoe_required": 2}
    if "complete a job application form" in system:
        listing = params["messages"][0]["content"]
        fields = json.loads(listing.split("Blank fields:\n", 1)[1])
        out = []
        for f in fields:
            lab = f["label"].lower()
            if "why" in lab or "project" in lab:
                out.append({"id": f["id"], "kind": "essay", "answer": "I built Python services at Acme.",
                            "evidence": None, "facts_used": ["Python microservices at Acme"],
                            "needs_user_reason": None})
            elif "years of python" in lab:
                out.append({"id": f["id"], "kind": "factual_from_resume", "answer": "3",
                            "evidence": "3 years of Python experience", "facts_used": [],
                            "needs_user_reason": None})
            else:
                out.append({"id": f["id"], "kind": "needs_user", "answer": None, "evidence": None,
                            "facts_used": [], "needs_user_reason": "not in resume"})
        return {"fields": out}
    if "cover letter" in system:
        return "Dear team, I built Python services at Acme. Jane Doe"
    raise AssertionError("unexpected prompt")


@pytest.fixture
def profile_dir(tmp_path) -> Paths:
    paths = Paths(tmp_path / "profile")
    paths.ensure()
    (paths.resumes / "general.txt").write_text(RESUME, encoding="utf-8")
    (paths.resumes / "backend.txt").write_text(RESUME + "\nBackend focus.", encoding="utf-8")
    paths.profile.write_text(yaml.safe_dump(base_profile()), encoding="utf-8")
    return paths


@pytest.fixture
def transport():
    return FakeTransport(default_responder)


@pytest.fixture
def make_ctx(profile_dir, transport):
    def _make(profile: dict | None = None, **kw) -> Context:
        if profile is not None:
            profile_dir.profile.write_text(yaml.safe_dump(profile), encoding="utf-8")
        prof = Profile.model_validate(yaml.safe_load(profile_dir.profile.read_text()))
        ctx = Context(profile_dir, profile=prof, secrets=kw.pop("secrets", {}), llm_transport=transport, **kw)
        return ctx
    return _make
