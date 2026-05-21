"""VCL Messaging — rule-based allocator.

Pure regex rule engine. No Frappe, no Claude — just pattern matching.
M-Pesa confirmations, Pesalink transfers and "X cheque ready for collection"
notes are perfectly regular text; this module recognises them and returns a
ready-made classification + follow-up spec so the message never has to go
through Claude.

`allocate(text)` returns either {"matched": False} or a dict the caller
(whatsapp_pa_api._apply_allocation) turns into a classified VCL Message + an
auto-created VCL Followup.

This file is deliberately standalone and testable:

    python3 -c "import allocator, json; print(json.dumps(
        allocator.allocate('Oriel cheque ready for collection'), indent=2))"
"""

import re


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

# M-Pesa: "UEKB74X6PZ Confirmed. Ksh58,500.00 sent to VIMIT CONVERTERS
#          for account hs said printers on 20/5/26 at 9:16 AM ..."
RE_MPESA = re.compile(
    r"\b([A-Z0-9]{10})\s+Confirmed\.?\s+Ksh\s*([\d,]+(?:\.\d+)?)\s+sent to\s+"
    r"VIMIT\s+CONVERTERS(?:\s+for account\s+(.+?))?\s+on\s+"
    r"(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)

# Pesalink: "Pesalink transfer of KES 590,000.00 to A/c ... processed successfully"
RE_PESALINK = re.compile(
    r"Pesalink\s+transfer\s+of\s+KES\s*([\d,]+(?:\.\d+)?)\s+to\s+A/c"
    r".+?processed\s+successfully",
    re.IGNORECASE | re.DOTALL,
)
RE_REFID = re.compile(
    r"\b(?:Transaction\s+)?Ref(?:erence)?\s*(?:ID|No)?\.?\s*[:#]?\s*([A-Za-z0-9]{4,})",
    re.IGNORECASE,
)

# "<name> cheque ready for collection"  /  "<name> cheque is ready"
RE_CHEQUE = re.compile(
    r"^\s*(.{2,60}?)\s+che?ques?\s+(?:is\s+|are\s+)?ready",
    re.IGNORECASE,
)

# RTGS: "RTGS done - KES 1,240,000 paid to ..."
RE_RTGS = re.compile(
    r"\bRTGS\b.{0,60}?KES\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _num(s):
    try:
        return float((s or "0").replace(",", "").strip() or 0)
    except (ValueError, AttributeError):
        return 0.0


def _fmt(amt):
    try:
        return f"{float(amt):,.0f}"
    except (ValueError, TypeError):
        return str(amt)


def _result(rule, ref, amount, summary, customer_hint, status, action):
    """Standard match shape — a classified payment plus a follow-up spec."""
    return {
        "matched": True,
        "rule": rule,
        "priority": "HIGH",
        "category": "payment",
        "summary": summary,
        "customer_hint": (customer_hint or "").strip() or None,
        "amount": amount,
        "ref": ref,
        "followup": {
            "type": "Payment Entry",
            "status": status,
            "action": action,
        },
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def allocate(text, group_name=None):
    """Run the rules over `text`. Returns a match dict, or {"matched": False}
    when nothing recognises it (caller then falls back to Claude)."""
    t = (text or "").strip()
    if not t:
        return {"matched": False}

    # --- M-Pesa received ---------------------------------------------------
    m = RE_MPESA.search(t)
    if m:
        code, amt = m.group(1), _num(m.group(2))
        acct = (m.group(3) or "").strip()
        summary = (f"M-Pesa {code} — KES {_fmt(amt)} received"
                   + (f" for account '{acct}'" if acct else "") + ".")
        return _result(
            "mpesa", code, amt, summary,
            customer_hint=acct,
            status="Pending",
            action=f"Raise the Payment Entry — M-Pesa {code}, KES {_fmt(amt)}.",
        )

    # --- Pesalink transfer -------------------------------------------------
    m = RE_PESALINK.search(t)
    if m:
        amt = _num(m.group(1))
        rm = RE_REFID.search(t)
        ref = rm.group(1) if rm else None
        summary = (f"Pesalink transfer — KES {_fmt(amt)} received"
                   + (f" (ref {ref})" if ref else "") + ".")
        return _result(
            "pesalink", ref, amt, summary,
            customer_hint=None,
            status="Pending",
            action=(f"Raise the Payment Entry — Pesalink KES {_fmt(amt)}"
                    + (f", ref {ref}" if ref else "") + "."),
        )

    # --- Cheque ready for collection --------------------------------------
    m = RE_CHEQUE.search(t)
    if m:
        who = m.group(1).strip(" .,-")
        return _result(
            "cheque_ready", None, None,
            f"{who} cheque ready for collection.",
            customer_hint=who,
            status="Cheque Pending Collection",
            action=f"Collect & bank the {who} cheque, then raise the Payment Entry.",
        )

    # --- RTGS --------------------------------------------------------------
    m = RE_RTGS.search(t)
    if m:
        amt = _num(m.group(1))
        return _result(
            "rtgs", None, amt,
            f"RTGS transfer — KES {_fmt(amt)}.",
            customer_hint=None,
            status="Pending",
            action=f"Confirm / raise the Payment Entry — RTGS KES {_fmt(amt)}.",
        )

    return {"matched": False}
