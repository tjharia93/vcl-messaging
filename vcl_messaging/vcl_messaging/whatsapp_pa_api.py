"""VCL Messaging - WhatsApp PA (Baileys group listener) integration.

The vcl_whatsapp_pa Node.js service runs an unofficial WhatsApp Web client
(Baileys) signed in as a VCL number. It listens to WhatsApp group messages
the number is in, and forwards every inbound message here as a POST.

This module:
  * authenticates the listener via an X-PA-Token shared secret
  * dedupes by external_id (wa-pa-{message_id})
  * find-or-creates VCL Message Contact + VCL Conversation (channel=WhatsApp Group)
  * inserts a VCL Message
  * (optionally) pings a Telegram chat with a short alert for every message

NOTE: The listener stays "dumb" by design (just forwards raw messages). All
classification + routing logic lives here so the listener is swap-out-able.
"""

import json

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime
from werkzeug.wrappers import Response

TELEGRAM_API = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def record_message():
    """Listener -> Frappe: store one inbound WhatsApp group message.

    Auth: X-PA-Token header must match VCL Channel Config.pa_shared_token
          for an enabled "WhatsApp Group" channel.

    POST body (JSON, from vcl_whatsapp_pa/src/forwarder.js):
        {
          "message_id": "<wa msg id>",
          "timestamp": 1715900000,
          "is_group": true,
          "group_id": "1234567890@g.us",
          "group_name": "VCL x Customer ABC",
          "chat_jid": "1234567890@g.us",
          "sender_jid": "254712345678@s.whatsapp.net",
          "sender_phone": "254712345678",
          "sender_name": "John Doe",
          "message_type": "text",
          "body": "5k assorted labels by Thursday please",
          "media": null
        }
    """
    request = frappe.local.request
    if request.method != "POST":
        frappe.local.response.http_status_code = 405
        return "Method not allowed"

    config = _validate_token(request.headers.get("X-PA-Token"))
    if not config:
        frappe.local.response.http_status_code = 401
        return "Unauthorized"

    try:
        payload = json.loads(request.get_data() or b"{}")
    except (json.JSONDecodeError, ValueError):
        frappe.local.response.http_status_code = 400
        return "Invalid JSON"

    try:
        result = _ingest(payload, config)
    except Exception as e:
        frappe.log_error(
            title="WhatsApp PA ingest failed",
            message=f"{e}\n\nPayload: {str(payload)[:5000]}",
        )
        frappe.local.response.http_status_code = 500
        return f"Ingest error: {e}"

    return Response(json.dumps(result), status=200, mimetype="application/json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _validate_token(token):
    """Match the shared token against any enabled WhatsApp Group channel config."""
    if not token:
        return None

    configs = frappe.get_all(
        "VCL Channel Config",
        filters={"channel_type": "WhatsApp Group", "enabled": 1},
        fields=["name"],
    )
    for cfg in configs:
        stored = frappe.utils.password.get_decrypted_password(
            "VCL Channel Config", cfg.name, "pa_shared_token", raise_exception=False
        )
        if stored and stored == token:
            return frappe.get_doc("VCL Channel Config", cfg.name)
    return None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _ingest(payload, config):
    msg_id = payload.get("message_id")
    if not msg_id:
        raise ValueError("missing message_id")

    external_id = f"wa-pa-{msg_id}"
    if frappe.db.exists("VCL Message", {"external_id": external_id}):
        return {"ok": True, "deduped": True, "external_id": external_id}

    contact_name = _get_or_create_contact(payload)
    conv_name = _get_or_create_conversation(payload, contact_name, config.name)

    sent_at = _ts_to_datetime(payload.get("timestamp"))
    body = payload.get("body") or ""
    msg_type = (payload.get("message_type") or "text").lower()
    if msg_type not in {
        "text", "image", "video", "audio", "document",
        "location", "contacts", "template", "interactive",
    }:
        msg_type = "text"

    msg = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conv_name,
        "direction": "Inbound",
        "message_type": msg_type,
        "status": "delivered",
        "content": body[:65000],
        "external_id": external_id,
        "sender_name": payload.get("sender_name") or payload.get("sender_phone"),
        "sent_at": sent_at,
    })
    msg.insert(ignore_permissions=True)

    preview = (body or f"[{msg_type}]")[:200]
    frappe.db.set_value("VCL Conversation", conv_name, {
        "last_message_at": sent_at,
        "last_message_preview": preview,
        "unread_count": (frappe.db.get_value("VCL Conversation", conv_name, "unread_count") or 0) + 1,
    })
    frappe.db.commit()

    alert = _send_telegram_alert(payload, conv_name, msg.name, config)

    return {
        "ok": True,
        "message": msg.name,
        "conversation": conv_name,
        "contact": contact_name,
        "alert": alert,
    }


def _get_or_create_contact(payload):
    """Resolve a VCL Message Contact for the sender by phone / whatsapp_id."""
    phone = payload.get("sender_phone") or ""
    wa_id = phone

    existing = (
        frappe.db.get_value("VCL Message Contact", {"whatsapp_id": wa_id}, "name")
        or (phone and frappe.db.get_value("VCL Message Contact", {"phone": phone}, "name"))
    )
    if existing:
        if wa_id and not frappe.db.get_value("VCL Message Contact", existing, "whatsapp_id"):
            frappe.db.set_value("VCL Message Contact", existing, "whatsapp_id", wa_id)
        return existing

    contact = frappe.get_doc({
        "doctype": "VCL Message Contact",
        "contact_name": payload.get("sender_name") or phone or "Unknown",
        "phone": phone,
        "whatsapp_id": wa_id,
    })
    contact.insert(ignore_permissions=True)
    frappe.db.commit()
    return contact.name


def _get_or_create_conversation(payload, contact_name, channel_config):
    """One open conversation per (group_id) — not per sender within the group.

    Group convo = everyone in the group shares one VCL Conversation. The
    contact link points to the *most recent sender* but VCL Message rows
    each carry their own sender_name, so multi-speaker history is preserved
    through the messages table.
    """
    group_id = payload.get("group_id")
    if group_id:
        existing = frappe.db.get_value(
            "VCL Conversation",
            {
                "channel": "WhatsApp Group",
                "whatsapp_group_id": group_id,
                "status": ["in", ["Open", "Pending"]],
            },
            "name",
        )
        if existing:
            frappe.db.set_value("VCL Conversation", existing, "contact", contact_name)
            return existing
    else:
        existing = frappe.db.get_value(
            "VCL Conversation",
            {
                "contact": contact_name,
                "channel": "WhatsApp Group",
                "status": ["in", ["Open", "Pending"]],
            },
            "name",
        )
        if existing:
            return existing

    conv = frappe.get_doc({
        "doctype": "VCL Conversation",
        "contact": contact_name,
        "channel": "WhatsApp Group",
        "channel_config": channel_config,
        "whatsapp_group_id": group_id,
        "whatsapp_group_name": payload.get("group_name"),
        "status": "Open",
    })
    conv.insert(ignore_permissions=True)
    frappe.db.commit()
    return conv.name


# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------

def _send_telegram_alert(payload, conv_name, msg_name, config):
    """Best-effort Telegram ping. Failure does NOT fail the ingest."""
    chat_id = config.get("pa_priority_telegram_chat_id")
    if not chat_id:
        return {"sent": False, "reason": "no_chat_id"}

    bot_token = frappe.utils.password.get_decrypted_password(
        "VCL Channel Config", config.name, "pa_telegram_bot_token", raise_exception=False
    )
    if not bot_token:
        return {"sent": False, "reason": "no_bot_token"}

    group_label = payload.get("group_name") or payload.get("group_id") or "(DM)"
    sender = payload.get("sender_name") or payload.get("sender_phone") or "?"
    msg_type = payload.get("message_type") or "text"
    body = payload.get("body") or f"[{msg_type}]"

    text = (
        f"WhatsApp · {group_label}\n"
        f"{sender}: {body[:400]}"
    )

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=5,
        )
        return {"sent": resp.ok, "status": resp.status_code}
    except Exception as e:
        frappe.log_error(title="WhatsApp PA Telegram alert failed", message=str(e))
        return {"sent": False, "reason": str(e)[:200]}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _ts_to_datetime(ts):
    if not ts:
        return now_datetime()
    try:
        return frappe.utils.datetime.datetime.fromtimestamp(int(ts))
    except Exception:
        return now_datetime()
