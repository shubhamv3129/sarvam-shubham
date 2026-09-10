"""
crm.py — the downstream system the voice agent drives.

This is the "agentic" half of the solution. The conversation is not the end of
the process: when the agent classifies an intent that a human must action, it
calls a tool, which writes a ticket to the CRM and notifies the operations
team. That is the full loop the assignment asks for:

    event occurs  ->  agent reasons  ->  tool is called  ->  downstream updated
    (borrower          (intent            (create_ticket)     (CRM record +
     speaks)            classified)                            webhook fired)

The "CRM" here is a local JSON file, which is deliberate: the assignment allows
mock data, and a real deployment would swap this one function for a Salesforce
/ LeadSquared / internal-core-banking call without touching the voice pipeline.
The webhook is real though — point CRM_WEBHOOK_URL at an n8n or Make workflow
(or webhook.site for a demo) and the ticket genuinely lands there live.
"""

import os
import json
import asyncio
import datetime
from pathlib import Path

CRM_FILE = Path(__file__).parent / "crm_tickets.json"
WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "").strip()

# Which intents a human must action, and how urgently. Kept as data rather than
# scattered through the conversation code so ops can change policy without
# touching the agent.
ESCALATION_POLICY = {
    "promise_to_pay": {
        "queue": "active-collections",
        "priority": "normal",
        "sla_hours": 12,
        "action": "Borrower committed to a payment date. Schedule automated SMS reminder.",
    },
    "hardship_request": {
        "queue": "restructuring",
        "priority": "high",
        "sla_hours": 24,
        "action": "Confirm 7-day extension and update repayment schedule",
    },
    "payment_dispute": {
        "queue": "verification",
        "priority": "urgent",
        "sla_hours": 24,
        "action": "Reconcile borrower's claimed payment against ledger",
    },
    "refuses_to_pay": {
        "queue": "field-collections",
        "priority": "urgent",
        "sla_hours": 24,
        "action": "Borrower refused to pay — senior officer contact required",
    },
    "denies": {
        "queue": "data-quality",
        "priority": "normal",
        "sla_hours": 72,
        "action": "Wrong-number reported — quarantine contact and re-KYC",
    },
    "unclear": {
        "queue": "manual-callback",
        "priority": "normal",
        "sla_hours": 48,
        "action": "Bot could not understand borrower — human callback required",
    },
}
DEFAULT_POLICY = {
    "queue": "manual-callback",
    "priority": "normal",
    "sla_hours": 48,
    "action": "Review call and follow up",
}


def _load():
    if CRM_FILE.exists():
        try:
            return json.loads(CRM_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def _save(tickets):
    CRM_FILE.write_text(json.dumps(tickets, ensure_ascii=False, indent=2), encoding="utf-8")


async def create_ticket(*, call_id, intent, details, language, transcript, account="LN-4471-RK"):
    """Tool the agent calls when a turn needs a human. Returns the ticket dict
    so the UI can show what was actually created — the point of the demo is
    that the panel SEES the downstream effect, not just hears the call."""
    policy = ESCALATION_POLICY.get(intent, DEFAULT_POLICY)
    tickets = _load()
    ticket = {
        "ticket_id": f"TKT-{1000 + len(tickets) + 1}",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "call_id": call_id,
        "account": account,
        "borrower": "Ravi Kumar",
        "intent": intent,
        "extracted_detail": details or "—",
        "language": language,
        "queue": policy["queue"],
        "priority": policy["priority"],
        "sla_hours": policy["sla_hours"],
        "next_action": policy["action"],
        "status": "open",
        "transcript_excerpt": transcript[-3:],
    }
    tickets.append(ticket)
    _save(tickets)
    print(f"[crm] {ticket['ticket_id']} -> {ticket['queue']} ({ticket['priority']})")

    if WEBHOOK_URL:
        asyncio.create_task(_fire_webhook(ticket))
    return ticket


async def _fire_webhook(ticket):
    """Best-effort POST to n8n / Make / Zapier / webhook.site. Never allowed to
    break the call — a downstream outage must not take the voice agent down."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(WEBHOOK_URL, json=ticket)
            print(f"[crm] webhook {r.status_code}")
    except Exception as e:
        print(f"[crm] webhook failed (non-fatal): {e}")


def list_tickets():
    return _load()


def reset_tickets():
    _save([])