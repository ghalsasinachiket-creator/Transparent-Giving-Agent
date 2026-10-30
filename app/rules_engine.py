"""rules_engine.py — deterministic giving-policy engine. NO LLM in this file."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum
import requests

API = "https://projects.propublica.org/nonprofits/api/v2"


class Verdict(IntEnum):
    # IntEnum so we can take max() to get the "worst" outcome across all rules
    APPROVE = 0
    FLAG = 1     # soft problem: needs human confirmation
    BLOCK = 2    # hard problem: PayPal is never called


@dataclass
class DonorRules:
    """The donor's policy. This same object is rendered on the 'giving rules' page."""
    paused: bool = False             # kill switch: when True nothing reaches PayPal
    per_gift_cap: float = 100.0      # max single donation (USD)
    monthly_cap: float = 300.0       # max total per calendar month
    max_filing_age_years: int = 3    # newest 990 must be at most this old
    min_revenue: float = 50_000.0    # below this, flag (too small to verify)


@dataclass
class RuleResult:
    rule: str                        # human-readable rule name
    verdict: Verdict                 # what this rule alone concluded
    detail: str                      # plain-English explanation shown in the UI
    evidence_url: str | None = None  # one-click proof for the judge


@dataclass
class Decision:
    verdict: Verdict
    results: list[RuleResult] = field(default_factory=list)


def fetch_org(ein: str) -> dict | None:
    """Pull the live org record + filing history. Returns None on any failure."""
    try:
        r = requests.get(f"{API}/organizations/{ein}.json", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def evaluate(org: dict | None, ein: str, amount: float,
             month_total: float, rules: DonorRules) -> Decision:
    """Run EVERY rule (no early exit) so the UI can show the full checklist.
    Final verdict = the worst verdict of any single rule."""
    results: list[RuleResult] = []
    url = f"https://projects.propublica.org/nonprofits/organizations/{ein}"

    # --- Donor-side rules (need no network) ---
    results.append(RuleResult(
        "Agent not paused",
        Verdict.BLOCK if rules.paused else Verdict.APPROVE,
        "Agent is paused by the donor." if rules.paused else "Agent is active."))

    results.append(RuleResult(
        "Per-gift cap",
        Verdict.BLOCK if amount > rules.per_gift_cap else Verdict.APPROVE,
        f"${amount:.0f} vs cap ${rules.per_gift_cap:.0f}."))

    results.append(RuleResult(
        "Monthly cap",
        Verdict.BLOCK if month_total + amount > rules.monthly_cap else Verdict.APPROVE,
        f"${month_total + amount:.0f} this month vs cap ${rules.monthly_cap:.0f}."))

    # --- Charity-side rules (need live data) ---
    if org is None:
        # Fail safe: if we can't verify, we don't approve.
        results.append(RuleResult("Charity data available", Verdict.FLAG,
                                  "Could not fetch filing data; cannot verify.", url))
    else:
        o = org.get("organization", {})

        # Rule: must be a 501(c)(3) (subsection_code 3 = charitable org)
        is_501c3 = str(o.get("subsection_code")) == "3"
        results.append(RuleResult(
            "Registered 501(c)(3)",
            Verdict.APPROVE if is_501c3 else Verdict.BLOCK,
            f"IRS subsection code: {o.get('subsection_code')}.", url))

        # Newest filing that actually contains parsed financial data
        filings = sorted(org.get("filings_with_data", []),
                         key=lambda f: f.get("tax_prd_yr", 0), reverse=True)
        latest = filings[0] if filings else None

        if latest is None:
            results.append(RuleResult("Recent filing", Verdict.FLAG,
                                      "No Form 990 with financial data found.", url))
        else:
            age = date.today().year - latest["tax_prd_yr"]
            fresh = age <= rules.max_filing_age_years
            results.append(RuleResult(
                "Recent filing",
                Verdict.APPROVE if fresh else Verdict.BLOCK,
                f"Latest 990 is for {latest['tax_prd_yr']} ({age} yrs old; "
                f"limit {rules.max_filing_age_years}).",
                latest.get("pdf_url") or url))

            revenue = latest.get("totrevenue") or 0
            results.append(RuleResult(
                "Minimum scale",
                Verdict.APPROVE if revenue >= rules.min_revenue else Verdict.FLAG,
                f"Reported revenue ${revenue:,} vs minimum ${rules.min_revenue:,.0f}.",
                latest.get("pdf_url") or url))

    return Decision(max(r.verdict for r in results), results)


if __name__ == "__main__":
    # Quick manual test: swap in real EINs. 53-0196605 is American Red Cross.
    ein = "530196605"
    d = evaluate(fetch_org(ein), ein, amount=25, month_total=0, rules=DonorRules())
    print(d.verdict.name)
    for r in d.results:
        print(f"  [{r.verdict.name:7}] {r.rule}: {r.detail}")