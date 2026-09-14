"""Realistic-shaped inbound leads for Meridian Facility Services.

Weighted toward what a real funnel actually looks like - mostly cold and
spam, hot leads are rare. That skew matters: it's what makes the eval set
and the ROI dashboard honest instead of flattering. Run standalone to
preview a batch:

    python -m data.synthetic_leads 10
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# This module needs to import from the `agent` package (for LeadIn) whether
# it's run as `python -m data.synthetic_leads` from the project root or
# imported by another script from somewhere else - so it puts the project
# root on sys.path itself instead of assuming the caller's cwd is right.
# The import has to come after this line, which is what `noqa: E402`
# (module-level import not at top of file) is silencing below - it's a
# deliberate ordering, not an oversight.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.schema import LeadIn  # noqa: E402

FIRST_NAMES = ["Jordan", "Casey", "Morgan", "Priya", "Diego", "Aisha", "Sam", "Lena",
               "Marcus", "Yuki", "Elena", "Tomás", "Grace", "Noah", "Fatima", "Owen"]
LAST_NAMES = ["Reyes", "Kim", "Novak", "Sharma", "Okafor", "Bianchi", "Larsen", "Cruz",
              "Bennett", "Haddad", "Kowalski", "Fischer", "Nguyen", "Patel", "Ortiz"]

COMPANIES_ENTERPRISE = [
    "Summit Health Network (7 clinics)", "Brightline Retail Group",
    "Pinnacle Facilities Management", "Cascade School District",
    "Harborview Medical Plaza (multi-site)", "Union Square Office Portfolio",
]
COMPANIES_SMB = [
    "Ridgeline Dental", "Alcove Coworking", "Thornbury Law Offices",
    "Fresh Start Pediatrics", "Kepler Insurance Agency", "Maple & Co. Studio",
]
FREE_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]
BIZ_DOMAINS = ["summithealth.com", "brightlineretail.com", "pinnaclefm.com",
               "ridgelinedental.com", "alcovecoworking.com", "thornburylaw.com"]

HOT_TEMPLATES = [
    "We manage {n} locations and our current janitorial contract expires "
    "at the end of the month - we need a new vendor lined up to start by then.",
    "Opening a new {site} next month across {n} locations and need nightly "
    "cleaning in place before we open. Can someone call this week?",
    "We're putting together an RFP for facilities cleaning across our "
    "portfolio of {n} sites. Deadline to respond is in 10 days.",
    "Our compliance inspection is in two weeks and our current cleaning "
    "vendor isn't meeting standards across our {n} sites - need this sorted urgently.",
]
WARM_TEMPLATES = [
    "Looking into options for regular office cleaning for our {site}. "
    "No rush, just comparing a few providers.",
    "Do you offer recurring cleaning contracts for a single {site}? "
    "Would like a quote when you have a chance.",
    "We've been using another vendor but aren't thrilled - considering a switch "
    "for our {site}, nothing urgent though.",
]
COLD_TEMPLATES = [
    "Hi, I need my house cleaned before guests arrive next weekend, do you do residential?",
    "Looking for a one-time move-out clean for my apartment.",
    "Do you clean Airbnbs between guests? Just one unit.",
    "Just curious what cleaning services generally cost, not sure if I need anything yet.",
]
SPAM_TEMPLATES = [
    "Hi, I noticed your website could use better SEO. We can boost your ranking "
    "- reply for a free audit.",
    "Grow your business with our guest post and link building service, low rates!",
    "test test 123",
    "asdf",
    "Unsubscribe me from this list immediately!!!",
    "lorem ipsum dolor sit amet consectetur",
]

SITE_WORDS = ["office", "clinic", "building", "location", "facility"]


def _email(name: str, domain: str) -> str:
    """"Priya Shah" + "summithealth.com" -> "priya.shah@summithealth.com" -
    just enough realism to look like a real inbound address."""
    handle = name.lower().replace(" ", ".")
    return f"{handle}@{domain}"


def _name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def generate_lead(rng: random.Random | None = None) -> LeadIn:
    """Builds one fake-but-plausible inbound lead. Pass a seeded
    `random.Random(seed)` for reproducible batches (that's what
    scripts/seed_history.py and the eval set generation do); leave it
    unset for genuinely random one-off samples, e.g. the CLI preview below."""
    # Default to a fresh, unseeded Random() instance rather than the `random`
    # module itself - same interface, but it doesn't share global RNG state
    # with anything else in the process (and satisfies the type checker,
    # which correctly treats the module and the class as different types).
    rng = rng or random.Random()

    # These weights are the whole point of this generator: a real inbound
    # funnel is mostly noise. 8% hot / 27% warm / 40% cold / 25% spam is a
    # deliberately unflattering split, chosen so the eval harness and the
    # ROI dashboard are grading against something that looks like a real
    # funnel, not a cherry-picked one.
    bucket = rng.choices(
        ["hot", "warm", "cold", "spam"], weights=[8, 27, 40, 25], k=1
    )[0]
    name = _name()
    source = rng.choices(
        ["web_form", "referral", "cold_email", "content_download"],
        weights=[55, 15, 15, 15], k=1,
    )[0]

    # Each bucket gets its own template pool, company pool, and email-domain
    # pool, so a "hot" lead reads like a real enterprise account (a business
    # email domain, a plausible phone number) and a "cold" one reads like a
    # real person (a free email domain, no phone) - the shape of the data
    # itself carries signal, the same way it would for a real lead.
    if bucket == "hot":
        company = rng.choice(COMPANIES_ENTERPRISE)
        message = rng.choice(HOT_TEMPLATES).format(
            n=rng.choice([3, 4, 5, 7, 9]), site=rng.choice(SITE_WORDS)
        )
        email = _email(name, rng.choice(BIZ_DOMAINS))
        phone = f"({rng.randint(200,999)}) {rng.randint(200,999)}-{rng.randint(1000,9999)}"
    elif bucket == "warm":
        company = rng.choice(COMPANIES_SMB)
        message = rng.choice(WARM_TEMPLATES).format(site=rng.choice(SITE_WORDS))
        email = _email(name, rng.choice(BIZ_DOMAINS + FREE_DOMAINS))
        phone = None
    elif bucket == "cold":
        company = None
        message = rng.choice(COLD_TEMPLATES)
        email = _email(name, rng.choice(FREE_DOMAINS))
        phone = None
    else:  # spam
        company = rng.choice([None, "Growth Marketing Solutions", "SEO Experts Inc"])
        message = rng.choice(SPAM_TEMPLATES)
        email = _email(name, rng.choice(FREE_DOMAINS))
        phone = None

    # Constructing LeadIn here (rather than returning a plain dict) means
    # every generated lead passes through the exact same Pydantic validation
    # a real webhook payload would - if a template above ever produced
    # something invalid, this would fail loudly here instead of silently
    # downstream.
    return LeadIn(
        name=name, email=email, company=company, phone=phone, message=message, source=source
    )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    for _ in range(n):
        print(json.dumps(generate_lead().model_dump(), indent=2))
