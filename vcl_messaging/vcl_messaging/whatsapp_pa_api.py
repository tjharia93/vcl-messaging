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

import base64
import json
import re

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.file_manager import save_file
from werkzeug.wrappers import Response

TELEGRAM_API = "https://api.telegram.org"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VISION_MODEL = "claude-haiku-4-5-20251001"
MEDIA_BEARING_TYPES = {"image", "video", "document", "audio"}


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
    """Best-effort Telegram ping for inbound messages.

    For media messages (image/video/document/audio) we DEFER the ping until
    Claude Vision has produced a summary — _classify_media fires the alert
    instead. For text we ping immediately.

    Failure does NOT fail the ingest.
    """
    msg_type = (payload.get("message_type") or "text").lower()
    if msg_type in MEDIA_BEARING_TYPES:
        return {"sent": False, "reason": "deferred_for_vision"}

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
# Media ingest + Claude Vision classification
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def record_media():
    """Listener -> Frappe: attach the decrypted media bytes to a previously
    inserted VCL Message and trigger Claude Vision classification.

    Auth: X-PA-Token same as record_message.

    POST (multipart/form-data):
        message_id  -> raw WhatsApp message id (the listener's `payload.message_id`)
        filename    -> suggested filename, e.g. "IMG-20260519-094815.jpg"
        mime        -> MIME type, e.g. "image/jpeg"
        media       -> binary file part with the actual bytes
    """
    request = frappe.local.request
    if request.method != "POST":
        frappe.local.response.http_status_code = 405
        return "Method not allowed"

    config = _validate_token(request.headers.get("X-PA-Token"))
    if not config:
        frappe.local.response.http_status_code = 401
        return "Unauthorized"

    form = request.form
    files = request.files
    message_id = form.get("message_id")
    if not message_id:
        frappe.local.response.http_status_code = 400
        return "Missing message_id"

    external_id = f"wa-pa-{message_id}"
    msg_name = frappe.db.get_value("VCL Message", {"external_id": external_id}, "name")
    if not msg_name:
        frappe.local.response.http_status_code = 404
        return f"No VCL Message for external_id={external_id}"

    file_field = files.get("media")
    if not file_field:
        frappe.local.response.http_status_code = 400
        return "Missing 'media' file part"

    data = file_field.read()
    mime = form.get("mime") or file_field.mimetype or "application/octet-stream"
    filename = form.get("filename") or _default_filename(external_id, mime)

    saved = save_file(
        fname=filename,
        content=data,
        dt="VCL Message",
        dn=msg_name,
        is_private=1,
    )

    frappe.db.set_value("VCL Message", msg_name, {
        "media_url": saved.file_url,
        "media_mime_type": mime,
    })
    frappe.db.commit()

    # Fire Claude Vision in a background job — keep the listener fast.
    frappe.enqueue(
        "vcl_messaging.vcl_messaging.whatsapp_pa_api._classify_media",
        queue="short",
        timeout=120,
        message_name=msg_name,
        config_name=config.name,
    )

    return Response(
        json.dumps({"ok": True, "message": msg_name, "file_url": saved.file_url, "mime": mime}),
        status=200,
        mimetype="application/json",
    )


def _default_filename(external_id, mime):
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }.get(mime, "")
    return f"{external_id}{ext or '.bin'}"


def _classify_media(message_name, config_name):
    """Background job. Reads the attached media bytes, asks Claude Vision
    for a 1-line description + kind classification, writes the result back
    onto the VCL Message row, and fires a Telegram alert with the summary."""
    msg = frappe.get_doc("VCL Message", message_name)
    if not msg.media_url:
        return

    api_key = frappe.utils.password.get_decrypted_password(
        "VCL Channel Config", config_name, "pa_anthropic_api_key", raise_exception=False
    )
    if not api_key:
        frappe.log_error(
            title="WhatsApp PA: no Anthropic key",
            message=f"Channel {config_name} has no pa_anthropic_api_key; skipping vision.",
        )
        return

    file_doc = frappe.get_doc("File", {"file_url": msg.media_url})
    file_path = file_doc.get_full_path()
    with open(file_path, "rb") as fh:
        data = fh.read()
    b64 = base64.b64encode(data).decode("ascii")

    mime = msg.media_mime_type or "image/jpeg"

    conv = frappe.get_doc("VCL Conversation", msg.conversation)
    group_name = conv.whatsapp_group_name or "(unknown group)"

    summary, kind = _ask_claude_vision(b64, mime, group_name, msg.sender_name, api_key)

    frappe.db.set_value("VCL Message", message_name, {
        "ai_summary": (summary or "")[:500],
        "ai_kind": (kind or "other")[:60],
        "ai_processed_at": now_datetime(),
    })
    frappe.db.commit()

    config = frappe.get_doc("VCL Channel Config", config_name)
    _send_vision_alert(msg, conv, summary, kind, config)


def _ask_claude_vision(b64, mime, group_name, sender_name, api_key):
    """Returns (summary, kind). On failure returns (None, 'other')."""
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        # Vision only supports images today — videos/audio/PDFs go elsewhere.
        return (f"[{mime} attachment; not auto-readable]", "other")

    prompt = (
        f"You are watching the VCL WhatsApp group \"{group_name}\".\n"
        f"\"{sender_name}\" just sent this image.\n\n"
        f"In ONE sentence (max 220 chars), describe what is visible — be concrete "
        f"(numbers, names, what kind of document/object). Then classify the image "
        f"as exactly ONE of:\n"
        f"  pi              — proforma invoice\n"
        f"  ci              — commercial invoice\n"
        f"  bl              — bill of lading\n"
        f"  label_artwork   — price labels, sticker / label artwork\n"
        f"  job_card_photo  — production-floor or job-card snapshot\n"
        f"  product_photo   — finished goods or raw materials\n"
        f"  screenshot      — UI / chat / spreadsheet screenshot\n"
        f"  handwritten_note\n"
        f"  other\n\n"
        f"Reply STRICTLY as JSON: {{\"summary\": \"...\", \"kind\": \"...\"}}"
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_VISION_MODEL,
        "max_tokens": 400,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }

    try:
        resp = requests.post(ANTHROPIC_API, json=body, headers=headers, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        frappe.log_error(title="WhatsApp PA: Claude vision call failed", message=str(e))
        return (None, "other")

    try:
        out = resp.json()
        text = out["content"][0]["text"].strip()
    except (KeyError, IndexError, ValueError) as e:
        frappe.log_error(title="WhatsApp PA: bad Anthropic response", message=f"{e}\n{resp.text[:1000]}")
        return (None, "other")

    parsed = _safe_parse_json_object(text)
    return (parsed.get("summary", text[:200]), parsed.get("kind", "other"))


def _safe_parse_json_object(text):
    """Claude sometimes wraps JSON in ```json fences or chatter. Extract the
    first JSON object we can find, fall back to a permissive shape."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return {"summary": text[:200], "kind": "other"}


def _send_vision_alert(msg, conv, summary, kind, config):
    """Telegram ping after Claude has read a media message."""
    chat_id = config.get("pa_priority_telegram_chat_id")
    if not chat_id:
        return
    bot_token = frappe.utils.password.get_decrypted_password(
        "VCL Channel Config", config.name, "pa_telegram_bot_token", raise_exception=False
    )
    if not bot_token:
        return

    group_label = conv.whatsapp_group_name or conv.whatsapp_group_id or "(DM)"
    sender = msg.sender_name or "?"
    icon = {"image": "img", "video": "vid", "audio": "audio", "document": "doc"}.get(
        msg.message_type, "media"
    )

    text = (
        f"WhatsApp · {group_label}\n"
        f"{sender}: [{icon} · {kind}]\n"
        f"{summary or '(no summary)'}"
    )

    try:
        requests.post(
            f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True},
            timeout=5,
        )
    except Exception as e:
        frappe.log_error(title="WhatsApp PA: vision Telegram alert failed", message=str(e))


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
