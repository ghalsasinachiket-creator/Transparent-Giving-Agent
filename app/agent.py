"""agent.py — the LLM layer. It interprets intent and PROPOSES charities only.
It never imports rules_engine, so it cannot influence any approve/flag/block."""
from __future__ import annotations
import difflib
import json
import re
import os
import requests
from groq import Groq

# Model name is configurable: Groq retires models over time, so check
# console.groq.com/docs/models if this one stops working.
#MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
DEFAULT_AMOUNT = 25.0

SYSTEM_PROMPT = """You help a donor choose charities. Read their request and reply
with ONLY a JSON object of this shape:
{
  "amount": <number in USD, or null if the donor gave none>,
  "cause": "<short phrase, e.g. clean drinking water>",
  "charities": [
    {"name": "<exact legal name of a real US 501(c)(3), as registered with the IRS>", "why": "<one sentence>"}
  ]
}
Rules: propose exactly 3 charities. Use real, established US charities only.
Do NOT include EINs, URLs, ratings or claims about financial health."""


def _ask_llm(intent: str) -> dict:
    """The only provider-specific function: swap this to change LLM vendors."""
    client = Groq(api_key=os.environ["GROQ_API_KEY"])   # read lazily, so importing never crashes
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.2, # low: we want stable, boring output
        reasoning_effort="low",
        response_format={"type": "json_object"},          # forces valid JSON
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": intent}],
    )
    return json.loads(resp.choices[0].message.content)


_NOISE = {"inc", "the", "of", "and", "foundation", "fund",
          "international", "usa", "incorporated"}

def _norm(s: str) -> str:
    """Normalise a charity name so brand and legal forms compare fairly."""
    s = s.lower()
    s = re.sub(r"\.(org|com|net)\b", "", s)     # "water.org" -> "water"
    s = re.sub(r"[^a-z0-9 ]", " ", s)           # punctuation -> spaces
    return " ".join(w for w in s.split() if w not in _NOISE)


def _resolve(name: str) -> dict | None:
    """Find the charity in the real registry, or return None.
    Strict on purpose: a wrong match would pay the wrong charity,
    so 'not found' is always better than a guess."""
    wanted = _norm(name)
    orgs, seen = [], set()
    for q in (name, wanted):                     # try the raw name, then the cleaned one
        if not q:
            continue
        try:
            r = requests.get(SEARCH, params={"q": q}, timeout=10)
            r.raise_for_status()
        except requests.RequestException:
            continue
        for o in r.json().get("organizations", []):   # use ALL results, not just the top 5
            if o["ein"] not in seen:
                seen.add(o["ein"])
                orgs.append(o)

    def score(o: dict) -> float:
        return difflib.SequenceMatcher(None, wanted, _norm(o["name"])).ratio()

    best = max(orgs, key=score, default=None)
    if best is None or score(best) < 0.8:        # was 0.6, which let "Waterford Pto" through
        return None
    return {"ein": str(best["ein"]).zfill(9), "matched_name": best["name"]}

def propose_charities(intent: str) -> dict:
    """intent text -> {amount, cause, charities: [{proposed_name, why, ein, matched_name}], unresolved}"""
    raw = _ask_llm(intent)

    # Validate everything the LLM said; never trust its types.
    try:
        amount = float(raw.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        amount = DEFAULT_AMOUNT

    charities, unresolved, seen = [], [], set()
    for item in (raw.get("charities") or [])[:3]:
        name = str(item.get("name", "")).strip()
        hit = _resolve(name) if name else None
        if hit is None:
            unresolved.append(name)                       # shown to the donor, never paid
        elif hit["ein"] not in seen:                      # skip duplicates
            seen.add(hit["ein"])
            charities.append({"proposed_name": name, "why": str(item.get("why", "")), **hit})

    return {"amount": amount, "cause": str(raw.get("cause", "")),
            "charities": charities, "unresolved": unresolved}