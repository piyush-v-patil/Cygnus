"""Employers where essentially every role needs US citizenship, US-person status or a
clearance (defense, space, national labs). Postings there are skipped before any
fetch or Claude call when visa.skip_us_person_roles is on.

Deliberately short: a company that hires non-citizens for some roles (Boeing,
Palantir, Textron...) is left to the per-posting checks. visa.us_person_companies_add
and visa.us_person_companies_remove in profile.yaml adjust the list.
"""
from __future__ import annotations

import re

US_PERSON_COMPANIES = {
    "anduril", "spacex", "blue origin", "lockheed martin", "northrop grumman",
    "rtx", "raytheon", "collins aerospace", "pratt whitney",
    "general dynamics", "general dynamics information technology", "general dynamics mission systems",
    "general dynamics electric boat", "l3harris", "bae systems", "booz allen hamilton", "booz allen",
    "leidos", "saic", "caci", "peraton", "peraton labs", "mantech", "huntington ingalls",
    "leonardo drs", "sierra nevada", "kratos defense and security solutions", "mercury systems",
    "shield ai", "saronic", "johns hopkins applied physics laboratory", "the aerospace corporation",
    "mitre", "sandia national laboratories", "los alamos national laboratory",
    "mit lincoln laboratory", "riverside research", "draper",
}
_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|llc|ltd|technologies|industries"
                     r"|international|holdings|company)\b")


def normalize_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " "))
    n = _SUFFIX.sub(" ", n)
    return " ".join(n.split())


def is_us_person_company(company: str, add: list[str] = (), remove: list[str] = ()) -> bool:
    n = normalize_company(company)
    if not n or n in {normalize_company(c) for c in remove}:
        return False
    return n in {normalize_company(c) for c in (*US_PERSON_COMPANIES, *add)}
