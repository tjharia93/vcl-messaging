"""Classification review — read the classifier's verdict, record a human one.

Deliberately NO piping. This app already turns payment / purchase_order /
sales_order into a ``VCL Followup`` automatically; 301 of those sit unactioned.
Before wiring anything else (notably ``inquiry`` -> ``Lead``) we need to know
whether the classification is any good, so this surface only records agreement
and disagreement.

``ai_category`` / ``ai_priority`` are NEVER overwritten. A correction is written
to ``human_category`` / ``human_priority`` alongside them — the gap between the
two pairs is the measurement we are after.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

CATEGORIES = [
    "payment", "purchase_order", "sales_order", "inquiry", "sales_status",
    "supplier_update", "complaint", "job_update", "ops_chatter", "personal", "other",
]
PRIORITIES = ["LOW", "MED", "HIGH", "CRIT"]

FIELDS = [
    "name", "conversation", "sent_at", "sender_name", "direction", "message_type",
    "content", "media_url", "ai_category", "ai_priority", "ai_summary", "ai_kind",
    "ai_customer_mentions", "human_category", "human_priority", "human_confirmed",
    "human_reviewed_by", "human_reviewed_at",
]


def _guard():
    if not frappe.has_permission("VCL Message", "read"):
        frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
def list_for_review(days=7, category=None, only_unreviewed=0, limit=200):
    """Recent messages with both verdicts, newest first.

    ``category`` filters on the EFFECTIVE category (human where set, else ai),
    so a corrected row moves bucket the way the reviewer expects it to.
    """
    _guard()
    filters = [["sent_at", ">=", frappe.utils.add_days(frappe.utils.nowdate(), -int(days))]]
    if int(only_unreviewed or 0):
        filters.append(["human_confirmed", "=", 0])
        filters.append(["human_category", "in", [None, ""]])

    rows = frappe.get_all(
        "VCL Message", filters=filters, fields=FIELDS,
        order_by="sent_at desc", limit_page_length=int(limit),
    )

    convs = {r["conversation"] for r in rows if r.get("conversation")}
    names = {}
    if convs:
        for c in frappe.get_all("VCL Conversation", filters=[["name", "in", list(convs)]],
                                fields=["name", "whatsapp_group_name"]):
            names[c["name"]] = c["whatsapp_group_name"]

    out = []
    for r in rows:
        r["group_name"] = names.get(r.get("conversation")) or "(unknown group)"
        r["effective_category"] = r.get("human_category") or r.get("ai_category")
        r["effective_priority"] = r.get("human_priority") or r.get("ai_priority")
        r["corrected"] = bool(r.get("human_category") or r.get("human_priority"))
        if category and r["effective_category"] != category:
            continue
        out.append(r)

    return {"messages": out, "categories": CATEGORIES, "priorities": PRIORITIES}


@frappe.whitelist(methods=["POST"])
def set_review(message, category=None, priority=None, confirmed=None):
    """Record a human verdict on one message.

    Passing a category/priority EQUAL to the classifier's clears the correction
    rather than storing a redundant one — so ``corrected`` always means a real
    disagreement, and the accuracy number stays honest.
    """
    _guard()
    if not frappe.has_permission("VCL Message", "write", message):
        frappe.throw(_("Not permitted."), frappe.PermissionError)

    doc = frappe.get_doc("VCL Message", message)
    vals = {}

    if category is not None:
        category = (category or "").strip()
        if category and category not in CATEGORIES:
            frappe.throw(_("Unknown category {0}.").format(category))
        vals["human_category"] = "" if (not category or category == doc.ai_category) else category

    if priority is not None:
        priority = (priority or "").strip().upper()
        if priority and priority not in PRIORITIES:
            frappe.throw(_("Unknown priority {0}.").format(priority))
        vals["human_priority"] = "" if (not priority or priority == doc.ai_priority) else priority

    if confirmed is not None:
        vals["human_confirmed"] = 1 if str(confirmed) in ("1", "true", "True", "yes") else 0

    if not vals:
        return {"ok": True, "unchanged": True}

    vals["human_reviewed_by"] = frappe.session.user
    vals["human_reviewed_at"] = now_datetime()
    frappe.db.set_value("VCL Message", message, vals)

    return {
        "ok": True,
        "message": message,
        "human_category": vals.get("human_category", doc.human_category),
        "human_priority": vals.get("human_priority", doc.human_priority),
        "human_confirmed": vals.get("human_confirmed", doc.human_confirmed),
    }


@frappe.whitelist()
def accuracy(days=30):
    """How often the classifier agrees with the people reading it.

    Only reviewed messages count — an unreviewed message is not evidence either
    way, and counting it as agreement would flatter the number.
    """
    _guard()
    rows = frappe.get_all(
        "VCL Message",
        filters=[["sent_at", ">=", frappe.utils.add_days(frappe.utils.nowdate(), -int(days))]],
        fields=["ai_category", "human_category", "human_confirmed"],
        limit_page_length=0,
    )
    reviewed = [r for r in rows if r.get("human_confirmed") or r.get("human_category")]
    wrong = [r for r in reviewed if r.get("human_category")]
    by_cat = {}
    for r in wrong:
        k = r.get("ai_category") or "other"
        by_cat[k] = by_cat.get(k, 0) + 1
    return {
        "captured": len(rows),
        "reviewed": len(reviewed),
        "corrected": len(wrong),
        "agreed": len(reviewed) - len(wrong),
        "accuracy_pct": round(100.0 * (len(reviewed) - len(wrong)) / len(reviewed), 1) if reviewed else None,
        "worst_categories": sorted(by_cat.items(), key=lambda kv: -kv[1])[:5],
    }
