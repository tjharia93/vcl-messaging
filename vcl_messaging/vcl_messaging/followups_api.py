"""VCL Messaging — Follow-ups.

A Follow-up is a tracked action raised off an inbox message: a customer, an
action, a deadline, an owner. The first type is "Payment Entry" — confirm a
WhatsApp-reported payment results in an ERPNext Payment Entry.

Loop:
  * inbox raises a Follow-up (Claude pre-fills customer / amount / action)
  * the clerk sees it, raises the Payment Entry in ERPNext, resolves it here
    (picking the matching Payment Entry from suggested candidates)
  * a daily cron escalates anything still Open past its deadline — Telegram
    to escalate_to. If exactly one Payment Entry now matches, it auto-resolves
    instead of nagging.

All write logic lives here; the VCL Followup doctype stays a plain record.
"""

import frappe
import requests
from frappe.utils import flt, now_datetime, today
from frappe.utils.password import get_decrypted_password

from vcl_messaging.vcl_messaging.whatsapp_pa_api import _first_pa_config

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_OWNER = "tanuj.haria@vimit.com"
AMOUNT_TOLERANCE = 0.02  # +/- 2% when matching Payment Entry amounts

# Statuses still "in flight" — escalated if past the deadline.
ACTIVE_STATUSES = ["Pending", "Cheque Pending Collection", "Pending Review"]
CLOSED_STATUSES = ["Completed", "Cancelled"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_user():
    """Owner / escalation default — Tanuj, falling back to the caller."""
    if frappe.db.exists("User", DEFAULT_OWNER):
        return DEFAULT_OWNER
    return frappe.session.user


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_followup(message, action, due_date, followup_type="Payment Entry",
                    status="Pending", customer=None, customer_text=None,
                    expected_amount=None, assigned_to=None, escalate_to=None,
                    payment_account=None, payment_ref=None, payment_date=None,
                    notes=None):
    """Raise a Follow-up against a VCL Message."""
    if not frappe.db.exists("VCL Message", message):
        frappe.throw(f"VCL Message {message} not found")

    summary = frappe.db.get_value("VCL Message", message, "ai_summary")
    fu = frappe.get_doc({
        "doctype": "VCL Followup",
        "followup_type": followup_type or "Payment Entry",
        "status": status or "Pending",
        "message": message,
        "customer": customer or None,
        "customer_text": (customer_text or None) if not customer else None,
        "expected_amount": flt(expected_amount) or None,
        "action": action,
        "due_date": due_date,
        "assigned_to": assigned_to or _default_user(),
        "escalate_to": escalate_to or _default_user(),
        "payment_account": payment_account or None,
        "payment_ref": payment_ref or None,
        "payment_date": payment_date or None,
        "source_summary": summary,
        "notes": notes or None,
    })
    fu.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": fu.name}


@frappe.whitelist()
def get_followups(status=None):
    """Every Follow-up, newest first — the inbox renders these per message."""
    filters = {}
    if status:
        filters["status"] = status
    return frappe.get_all(
        "VCL Followup",
        filters=filters,
        fields=[
            "name", "followup_type", "status", "message", "conversation",
            "customer", "customer_text", "expected_amount", "action", "due_date",
            "assigned_to", "escalate_to", "linked_payment_entry",
            "payment_account", "payment_ref", "payment_date", "notes",
            "resolved_on", "escalated_on",
        ],
        order_by="creation desc",
        limit_page_length=0,
    )


@frappe.whitelist()
def match_payment_entries(followup):
    """Return up to 3 candidate Payment Entries for a Follow-up — matched on
    customer, amount (+/- 2%) and posting date. The clerk picks the right one."""
    fu = frappe.get_doc("VCL Followup", followup)
    return _match_payment_entries(fu)


def _match_payment_entries(fu):
    if not fu.customer:
        return []
    rows = frappe.get_all(
        "Payment Entry",
        filters={
            "party_type": "Customer",
            "party": fu.customer,
            "payment_type": "Receive",
            "docstatus": ["<", 2],
        },
        fields=["name", "paid_amount", "received_amount", "posting_date",
                "reference_no", "reference_date", "paid_to", "mode_of_payment",
                "docstatus"],
        order_by="posting_date desc",
        limit_page_length=25,
    )
    want = flt(fu.expected_amount)
    out = []
    for r in rows:
        amt = flt(r.get("received_amount")) or flt(r.get("paid_amount"))
        if want and amt and abs(amt - want) > max(want * AMOUNT_TOLERANCE, 1):
            continue
        out.append(r)
    return out[:3]


@frappe.whitelist()
def resolve_followup(followup, status="Completed", payment_entry=None,
                     payment_account=None, payment_ref=None, payment_date=None,
                     expected_amount=None, notes=None):
    """Move a Follow-up along the pipeline. `status` is any of the 6 states.
    Optionally link a Payment Entry and/or record the payment detail
    (account / ref / date) — whether the PE was system-matched or keyed in."""
    fu = frappe.get_doc("VCL Followup", followup)
    fu.status = status
    if payment_entry:
        fu.linked_payment_entry = payment_entry
    if payment_account:
        fu.payment_account = payment_account
    if payment_ref:
        fu.payment_ref = payment_ref
    if payment_date:
        fu.payment_date = payment_date
    if expected_amount is not None and str(expected_amount) != "":
        fu.expected_amount = flt(expected_amount)
    if notes:
        fu.notes = notes
    if status in ("Completed", "Cancelled"):
        fu.resolved_on = now_datetime()
    fu.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": fu.name, "status": status}


# ---------------------------------------------------------------------------
# Daily escalation cron  (wired in hooks.py scheduler_events.daily)
# ---------------------------------------------------------------------------

def escalate_followups():
    """Any active Follow-up (Pending / Cheque Pending Collection / Pending
    Review) on/after its deadline: try once more to match a Payment Entry —
    auto-resolve on a single submitted match, else escalate to Telegram and
    flip status to Escalated."""
    rows = frappe.get_all(
        "VCL Followup",
        filters={"status": ["in", ACTIVE_STATUSES], "due_date": ["<=", today()]},
        fields=["name"],
    )
    for r in rows:
        try:
            _process_overdue(r["name"])
        except Exception as e:
            frappe.log_error(
                title="VCL Followup escalation failed",
                message=f"{r['name']}: {e}",
            )


def _process_overdue(name):
    fu = frappe.get_doc("VCL Followup", name)
    candidates = _match_payment_entries(fu)
    submitted = [c for c in candidates if c.get("docstatus") == 1]

    if len(submitted) == 1:
        # The Payment Entry was raised — nobody closed the follow-up. Do it.
        pe = submitted[0]
        fu.status = "Completed"
        fu.linked_payment_entry = pe["name"]
        fu.payment_account = pe.get("paid_to")
        fu.payment_ref = pe.get("reference_no")
        fu.payment_date = pe.get("posting_date")
        fu.resolved_on = now_datetime()
        fu.notes = (fu.notes or "") + "\n[auto-resolved by escalation cron]"
        fu.save(ignore_permissions=True)
        frappe.db.commit()
        return

    fu.status = "Escalated"
    fu.escalated_on = now_datetime()
    fu.save(ignore_permissions=True)
    frappe.db.commit()
    _escalate_telegram(fu, candidates)


def _escalate_telegram(fu, candidates):
    """Best-effort Telegram flag to escalate_to. Failure never breaks the cron."""
    config_name = _first_pa_config()
    if not config_name:
        return
    chat_id = frappe.db.get_value("VCL Channel Config", config_name,
                                  "pa_priority_telegram_chat_id")
    if not chat_id:
        return
    token = get_decrypted_password("VCL Channel Config", config_name,
                                   "pa_telegram_bot_token", raise_exception=False)
    if not token:
        return

    who = fu.customer or fu.customer_text or "(customer unknown)"
    lines = [
        f"OVERDUE FOLLOW-UP · {fu.followup_type}",
        fu.action or "",
        f"Customer: {who}",
    ]
    if fu.expected_amount:
        lines.append(f"Amount: KES {flt(fu.expected_amount):,.0f}")
    lines.append(f"Deadline {fu.due_date} — still {fu.status}.")
    if candidates:
        lines.append(f"{len(candidates)} possible Payment Entry match(es) "
                     f"— confirm in the inbox: {fu.name}")
    else:
        lines.append(f"No Payment Entry found. Please check. ({fu.name})")

    try:
        requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join([l for l in lines if l]),
                  "disable_web_page_preview": True},
            timeout=5,
        )
    except Exception as e:
        frappe.log_error(title="VCL Followup Telegram escalation failed",
                         message=str(e))
