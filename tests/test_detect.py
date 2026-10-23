import pytest

from autoapply.ats.detect import AtsRef, application_url, detect, normalize_url, parse_key, posting_key


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/affirm/jobs/8011590003", ("greenhouse", "affirm", "8011590003")),
    ("https://boards.greenhouse.io/Stripe/jobs/123456?gh_src=x", ("greenhouse", "stripe", "123456")),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/42", ("greenhouse", "acme", "42")),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=999", ("greenhouse", "acme", "999")),
    ("https://jobs.lever.co/matchgroup/1FEEC16E-f391-45dc-b693-9fb3100ba93e/apply",
     ("lever", "matchgroup", "1feec16e-f391-45dc-b693-9fb3100ba93e")),
    ("https://jobs.ashbyhq.com/abridge/7d6ae2be-cd53-466c-8151-2dae2e87aace",
     ("ashby", "abridge", "7d6ae2be-cd53-466c-8151-2dae2e87aace")),
])
def test_detect_supported(url, expected):
    ref = detect(url)
    assert (ref.ats, ref.board_token, ref.job_id) == expected
    assert ref.supported


def test_gh_jid_resolved_from_companies_yaml():
    url = "https://careers.airbnb.com/positions/7826337?gh_jid=7826337"
    assert detect(url, "Airbnb", {"airbnb": "airbnb"}) == AtsRef("greenhouse", "airbnb", "7826337")
    unresolved = detect(url, "Airbnb", {})
    assert unresolved.ats == "unsupported" and not unresolved.supported


@pytest.mark.parametrize("url,ats", [
    ("https://www.linkedin.com/jobs/view/4473951967", "linkedin"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", "unsupported"),   # a board, not a job
    ("https://careers-amd.icims.com/jobs/1/job", "unsupported"),
])
def test_detect_unsupported(url, ats):
    assert detect(url).ats == ats


def test_posting_key_dedupes_two_links_to_one_posting():
    a = "https://job-boards.greenhouse.io/acme/jobs/1001"
    b = "https://boards.greenhouse.io/acme/jobs/1001?gh_src=simplify"
    assert posting_key(detect(a), a) == posting_key(detect(b), b) == "greenhouse:acme:1001"


def test_url_key_normalisation():
    u1 = "https://Careers.Example.com/jobs/77/?utm_source=x&jobId=77#apply"
    u2 = "https://careers.example.com/jobs/77?jobId=77"
    assert normalize_url(u1) == normalize_url(u2)
    assert posting_key(detect(u1), u1) == "url:" + normalize_url(u2)


def test_application_urls_and_parse_key():
    assert application_url(parse_key("lever:beta:abc")) == "https://jobs.lever.co/beta/abc/apply"
    assert application_url(parse_key("ashby:g:x")) == "https://jobs.ashbyhq.com/g/x/application"
    assert application_url(parse_key("greenhouse:a:1")) == "https://job-boards.greenhouse.io/a/jobs/1"
