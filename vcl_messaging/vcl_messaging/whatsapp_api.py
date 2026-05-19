"""VCL Messaging - WhatsApp Integration.

Integrates with WhatsApp Cloud API (Meta) for sending/receiving messages.
Webhook (Meta -> Frappe) creates VCL Message rows. Outbound messages can be
triggered from the UI or from n8n via the whitelisted send endpoints.

Architecture:
    Meta -> webhook (POST) -> _process_inbound_message -> VCL Message (Inbound)
    VCL Message after_insert -> dispatch_to_n8n -> n8n webhook
    n8n -> send_whatsapp_message (REST) -> _post_to_meta -> Meta
    Meta -> webhook (status) -> _process_status_update -> updates VCL Message
"""

import hashlib
import hmac
import json

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime
from werkzeug.wrappers import Response

META_GRAPH_VERSION = "v22.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


# ---------------------------------------------------------------------------
# Config + helpers
# ---------------------------------------------------------------------------

def _get_whatsapp_config(channel_name=None):
    """Get the WhatsApp channel config (by name, or first enabled)."""
    filters = {"channel_type": "WhatsApp", "enabled": 1}
    if channel_name:
        filters["channel_name"] = channel_name
    config = frappe.get_all(
        "VCL Channel Config",
        filters=filters,
        fields=[
            "name",
            "channel_name",
            "whatsapp_phone_number_id",
            "whatsapp_business_account_id",
            "whatsapp_verify_token",
            "webhook_url",
        ],
        limit=1,
    )
    return config[0] if config else None


def _get_access_token(config_name):
    """Resolve the encrypted WhatsApp access token from the config doc."""
    return frappe.utils.password.get_decrypted_password(
        "VCL Channel Config", config_name, "whatsapp_access_token"
    )


def _normalise_phone(phone):
    """Strip leading + / spaces. Meta uses E.164 without + in many payloads."""
    if not phone:
        return phone
    return str(phone).lstrip("+").replace(" ", "").replace("-", "")


def _get_or_create_contact(whatsapp_id, profile_name=None, phone=None):
    """Find an existing VCL Message Contact by whatsapp_id, else create one."""
    existing = frappe.db.get_value(
        "VCL Message Contact", {"whatsapp_id": whatsapp_id}, "name"
    )
    if existing:
        return existing

    contact = frappe.get_doc({
        "doctype": "VCL Message Contact",
        "contact_name": profile_name or whatsapp_id,
        "phone": phone or whatsapp_id,
        "whatsapp_id": whatsapp_id,
    })
    contact.insert(ignore_permissions=True)
    frappe.db.commit()
    return contact.name


def _get_or_create_conversation(contact_name, channel_config):
    """Get the open WhatsApp conversation for a contact, or open a new one."""
    existing = frappe.db.get_value(
        "VCL Conversation",
        {
            "contact": contact_name,
            "channel": "WhatsApp",
            "status": ["in", ["Open", "Pending"]],
        },
        "name",
    )
    if existing:
        return existing

    conv = frappe.get_doc({
        "doctype": "VCL Conversation",
        "contact": contact_name,
        "channel": "WhatsApp",
        "channel_config": channel_config,
        "status": "Open",
    })
    conv.insert(ignore_permissions=True)
    frappe.db.commit()
    return conv.name


# ---------------------------------------------------------------------------
# Webhook (Meta -> Frappe)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def webhook():
    """WhatsApp Cloud API webhook endpoint.

    GET: handles Meta's hub.challenge verification handshake.
    POST: receives incoming messages and status updates.

    Public URL (Frappe Cloud):
        https://vimitconverters.frappe.cloud/api/method/vcl_messaging.vcl_messaging.whatsapp_api.webhook
    """
    request = frappe.local.request
    method = request.method

    if method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        config = _get_whatsapp_config()
        if not config:
            frappe.local.response.http_status_code = 503
            return "WhatsApp not configured"

        if mode == "subscribe" and token == config.get("whatsapp_verify_token"):
            return Response(challenge or "", status=200, mimetype="text/plain")

        frappe.local.response.http_status_code = 403
        return "Verification failed"

    if method == "POST":
        raw_body = request.get_data()
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            frappe.local.response.http_status_code = 400
            return "Invalid JSON"

        config = _get_whatsapp_config()
        if not config:
            frappe.local.response.http_status_code = 503
            return "WhatsApp not configured"

        try:
            _process_webhook_payload(payload, config)
        except Exception as e:
            frappe.log_error(
                title="WhatsApp webhook processing error",
                message=f"{e}\n\nPayload: {raw_body[:5000]}",
            )

        return Response("OK", status=200, mimetype="text/plain")

    frappe.local.response.http_status_code = 405
    return "Method not allowed"


def _process_webhook_payload(payload, config):
    """Walk Meta's nested webhook shape and dispatch each change."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})

            if field != "messages":
                continue

            for message in value.get("messages", []) or []:
                _process_inbound_message(message, value, config)

            for status in value.get("statuses", []) or []:
                _process_status_update(status)


def _process_inbound_message(message, value, config):
    """Convert one Meta inbound message into a VCL Message row."""
    msg_id = message.get("id")
    if not msg_id:
        return

    external_id = f"wa-{msg_id}"
    if frappe.db.exists("VCL Message", {"external_id": external_id}):
        return

    wa_from = message.get("from")
    msg_type = message.get("type", "text")
    timestamp = message.get("timestamp")
    sent_at = frappe.utils.datetime.datetime.fromtimestamp(int(timestamp)) if timestamp else now_datetime()

    profile_name = None
    for contact_info in value.get("contacts", []) or []:
        if contact_info.get("wa_id") == wa_from:
            profile = contact_info.get("profile") or {}
            profile_name = profile.get("name")
            break

    contact_name = _get_or_create_contact(wa_from, profile_name=profile_name)
    conv_name = _get_or_create_conversation(contact_name, config.get("name"))

    content, media_url, media_mime = _extract_content(message, msg_type)

    msg = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conv_name,
        "direction": "Inbound",
        "message_type": msg_type if msg_type in {
            "text", "image", "video", "audio", "document",
            "location", "contacts", "template", "interactive",
        } else "text",
        "status": "delivered",
        "content": (content or "")[:65000],
        "media_url": media_url,
        "media_mime_type": media_mime,
        "external_id": external_id,
        "sender_name": profile_name or wa_from,
        "sent_at": sent_at,
    })
    msg.insert(ignore_permissions=True)

    frappe.db.set_value(
        "VCL Conversation", conv_name,
        {"last_message_at": sent_at, "unread_count": (frappe.db.get_value("VCL Conversation", conv_name, "unread_count") or 0) + 1},
    )
    frappe.db.commit()


def _extract_content(message, msg_type):
    """Return (text_content, media_url, media_mime_type) for a Meta message."""
    if msg_type == "text":
        return message.get("text", {}).get("body", ""), None, None
    if msg_type in {"image", "video", "audio", "document"}:
        media = message.get(msg_type, {}) or {}
        caption = media.get("caption", "") or ""
        media_id = media.get("id")
        mime = media.get("mime_type")
        return caption, f"meta-media:{media_id}" if media_id else None, mime
    if msg_type == "location":
        loc = message.get("location", {}) or {}
        return f"location: {loc.get('latitude')},{loc.get('longitude')} {loc.get('name', '')}", None, None
    if msg_type == "interactive":
        inter = message.get("interactive", {}) or {}
        if inter.get("type") == "button_reply":
            return inter.get("button_reply", {}).get("title", ""), None, None
        if inter.get("type") == "list_reply":
            return inter.get("list_reply", {}).get("title", ""), None, None
        return json.dumps(inter)[:5000], None, None
    return json.dumps(message)[:5000], None, None


def _process_status_update(status):
    """Update an existing VCL Message with delivered/read/failed status."""
    msg_id = status.get("id")
    new_status = status.get("status")
    timestamp = status.get("timestamp")

    if not (msg_id and new_status):
        return

    external_id = f"wa-{msg_id}"
    msg_name = frappe.db.get_value("VCL Message", {"external_id": external_id}, "name")
    if not msg_name:
        return

    updates = {"status": new_status}
    if timestamp:
        dt = frappe.utils.datetime.datetime.fromtimestamp(int(timestamp))
        if new_status == "delivered":
            updates["delivered_at"] = dt
        elif new_status == "read":
            updates["read_at"] = dt

    if new_status == "failed":
        errors = status.get("errors", [])
        if errors:
            updates["error_message"] = json.dumps(errors)[:500]

    frappe.db.set_value("VCL Message", msg_name, updates)
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Send (Frappe -> Meta)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def send_whatsapp_message(conversation, content):
    """Send a text WhatsApp reply on an existing conversation."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    conv = frappe.get_doc("VCL Conversation", conversation)
    if conv.channel != "WhatsApp":
        frappe.throw(_("Conversation is not a WhatsApp conversation"))

    contact = frappe.get_doc("VCL Message Contact", conv.contact)
    to = contact.whatsapp_id or contact.phone
    if not to:
        frappe.throw(_("Contact has no WhatsApp ID or phone"))

    return _send_text(to=to, body=content, conversation=conversation, config_name=conv.channel_config)


@frappe.whitelist()
def send_text_to_number(to, body, channel_name=None):
    """Send a text WhatsApp message to a raw phone number.

    Auto-creates contact + conversation if needed. Used by n8n / scripts
    that don't already hold a conversation reference.
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    to = _normalise_phone(to)
    config = _get_whatsapp_config(channel_name)
    if not config:
        frappe.throw(_("No enabled WhatsApp channel configured"))

    contact_name = _get_or_create_contact(to)
    conv_name = _get_or_create_conversation(contact_name, config.get("name"))

    return _send_text(to=to, body=body, conversation=conv_name, config_name=config.get("name"))


def _send_text(to, body, conversation, config_name):
    """Internal: insert outbound VCL Message + POST to Meta."""
    msg_doc = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conversation,
        "direction": "Outbound",
        "message_type": "text",
        "status": "pending",
        "content": body,
        "sent_at": now_datetime(),
        "sender_name": frappe.session.user,
    })
    msg_doc.insert(ignore_permissions=True)
    frappe.db.commit()

    try:
        meta_response = _post_to_meta(
            config_name=config_name,
            payload={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": _normalise_phone(to),
                "type": "text",
                "text": {"body": body},
            },
        )

        wa_message_id = None
        if isinstance(meta_response, dict):
            messages = meta_response.get("messages") or []
            if messages:
                wa_message_id = messages[0].get("id")

        updates = {"status": "sent"}
        if wa_message_id:
            updates["external_id"] = f"wa-{wa_message_id}"
        frappe.db.set_value("VCL Message", msg_doc.name, updates)
        frappe.db.commit()

        return {"success": True, "message_id": msg_doc.name, "meta_id": wa_message_id}

    except Exception as e:
        error_msg = str(e)
        frappe.db.set_value("VCL Message", msg_doc.name, {
            "status": "failed",
            "error_message": error_msg[:500],
        })
        frappe.db.commit()
        frappe.log_error(title="WhatsApp send failed", message=error_msg)
        return {"success": False, "message_id": msg_doc.name, "error": error_msg}


def _post_to_meta(config_name, payload):
    """POST to Meta Graph API. Returns parsed JSON or raises."""
    config = frappe.db.get_value(
        "VCL Channel Config", config_name,
        ["whatsapp_phone_number_id"], as_dict=True,
    )
    if not config or not config.whatsapp_phone_number_id:
        raise ValueError("WhatsApp channel config missing phone_number_id")

    token = _get_access_token(config_name)
    if not token:
        raise ValueError("WhatsApp access token not set")

    url = f"{META_GRAPH_BASE}/{config.whatsapp_phone_number_id}/messages"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Meta API {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------------
# n8n dispatch (hook on VCL Message after_insert)
# ---------------------------------------------------------------------------

def dispatch_to_n8n(doc, method=None):
    """Hook: when a new VCL Message lands, push inbound WhatsApp ones to n8n."""
    if doc.direction != "Inbound":
        return

    conv = frappe.db.get_value(
        "VCL Conversation", doc.conversation,
        ["channel", "channel_config", "contact"], as_dict=True,
    )
    if not conv or conv.channel != "WhatsApp":
        return

    webhook_url = frappe.db.get_value(
        "VCL Channel Config", conv.channel_config, "webhook_url"
    )
    if not webhook_url:
        return

    contact = frappe.db.get_value(
        "VCL Message Contact", conv.contact,
        ["contact_name", "phone", "whatsapp_id", "linked_customer", "linked_lead"],
        as_dict=True,
    ) or {}

    body = {
        "event": "whatsapp.inbound",
        "message": {
            "name": doc.name,
            "external_id": doc.external_id,
            "type": doc.message_type,
            "content": doc.content,
            "media_url": doc.media_url,
            "sent_at": str(doc.sent_at) if doc.sent_at else None,
        },
        "conversation": {"name": doc.conversation, "channel_config": conv.channel_config},
        "contact": dict(contact),
    }

    try:
        requests.post(webhook_url, json=body, timeout=5)
    except Exception as e:
        frappe.log_error(title="n8n dispatch failed", message=str(e))
