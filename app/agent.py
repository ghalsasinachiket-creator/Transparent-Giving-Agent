"""agent.py — the LLM layer. It interprets intent and PROPOSES charities only.
It never imports rules_engine, so it cannot influence any approve/flag/block."""
from __future__ import annotations
import difflib
import json
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


def _resolve(name: str) -> dict | None:
    """Look the proposed name up in the real nonprofit registry.
    A hallucinated charity finds no match and is dropped here, so it never
    reaches the rules engine. We keep the registry's name so any mismatch
    between what the LLM said and what the registry holds stays visible."""
    try:
        r = requests.get(SEARCH, params={"q": name}, timeout=10)
        r.raise_for_status()
        orgs = r.json().get("organizations", [])[:5]
    except requests.RequestException:
        return None

    def score(o: dict) -> float:                          # fuzzy name similarity, 0..1
        return difflib.SequenceMatcher(None, name.lower(), o["name"].lower()).ratio()

    best = max(orgs, key=score, default=None)
    if best is None or score(best) < 0.6:                 # too different -> treat as not found
        return None
    return {"ein": str(best["ein"]).zfill(9),             # EINs are 9 digits, keep leading zeros
            "matched_name": best["name"]}


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