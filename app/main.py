"""main.py — FastAPI app wiring rules engine + ledger + PayPal together."""
import os
from dotenv import load_dotenv
load_dotenv()                                  # must run before paypal_client is imported

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import agent
import ledger
from rules_engine import DonorRules, evaluate, fetch_org
from paypal_client import create_donation_order, capture_order

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")   # becomes your Render URL later

app = FastAPI(title="Transparent Giving Agent")
ledger.init_db()


class EvalReq(BaseModel):
    ein: str                  # digits only, e.g. "530196605"
    name: str
    amount: float

class DonateReq(BaseModel):
    decision_id: int
    confirmed: bool = False   # donor's explicit OK for FLAG decisions

class PauseReq(BaseModel):
    paused: bool


@app.get("/rules")
def read_rules():
    return ledger.get_rules().__dict__

@app.put("/rules")
def update_rules(rules: DonorRules):          # dataclass works as a request body
    ledger.save_rules(rules)
    return rules.__dict__

@app.post("/pause")
def pause(req: PauseReq):
    """The kill switch: flips paused in the stored rules. The engine reads it
    on every evaluation, so a paused agent can never reach PayPal."""
    rules = ledger.get_rules()
    rules.paused = req.paused
    ledger.save_rules(rules)
    return {"paused": rules.paused}


@app.post("/evaluate")
def evaluate_charity(req: EvalReq):
    """Check one charity against the donor's rules. Every result is logged."""
    d = evaluate(fetch_org(req.ein), req.ein, req.amount,
                 ledger.month_total(), ledger.get_rules())
    decision_id = ledger.record_decision(req.ein, req.name, req.amount, d)
    return ledger.get_decision(decision_id)


@app.post("/donate")
def donate(req: DonateReq):
    stored = ledger.get_decision(req.decision_id)
    if not stored:
        raise HTTPException(404, "Unknown decision")

    # RE-EVALUATE NOW instead of trusting the stored verdict: the donor may have
    # paused the agent, or the cap may be used up, since the card was shown.
    # Ein/name/amount come from the DB, so the client can't tamper with them.
    ein, name, amount = stored["ein"], stored["charity_name"], stored["amount"]
    fresh = evaluate(fetch_org(ein), ein, amount, ledger.month_total(), ledger.get_rules())
    fresh_id = ledger.record_decision(ein, name, amount, fresh)

    try:
        order = create_donation_order(
            fresh, ein, name, amount,
            return_url=f"{BASE_URL}/paypal/return",
            cancel_url=f"{BASE_URL}/paypal/cancel",
            confirmed=req.confirmed)
    except PermissionError as e:                # the gate in paypal_client said no
        raise HTTPException(403, str(e))

    ledger.record_order(order["order_id"], fresh_id)
    return {"approve_url": order["approve_url"], "decision_id": fresh_id}


@app.get("/paypal/return")
def paypal_return(token: str):
    """PayPal sends the donor here after approval; `token` is the order id."""
    cap = capture_order(token)
    if cap["status"] == "COMPLETED":
        ledger.mark_completed(token, cap["capture_id"])
    return cap

@app.get("/paypal/cancel")
def paypal_cancel():
    return {"status": "cancelled"}


@app.get("/ledger")
def read_ledger():
    return ledger.list_ledger()

class ProposeReq(BaseModel):
    intent: str               # e.g. "give $20 to a clean drinking water charity"

@app.post("/propose")
def propose(req: ProposeReq):
    """Full pipeline: LLM proposes -> registry resolves -> rules engine decides.
    Note the order: the LLM's output is only ever INPUT to evaluate()."""
    try:
        plan = agent.propose_charities(req.intent)
    except Exception as e:                                # LLM down, bad key, bad JSON...
        raise HTTPException(502, f"Agent failed: {e}")

    cards = []
    for c in plan["charities"]:
        d = evaluate(fetch_org(c["ein"]), c["ein"], plan["amount"],
                     ledger.month_total(), ledger.get_rules())
        did = ledger.record_decision(c["ein"], c["matched_name"], plan["amount"], d)
        cards.append({**c, "decision": ledger.get_decision(did)})

    return {"cause": plan["cause"], "amount": plan["amount"],
            "cards": cards, "unresolved": plan["unresolved"]}