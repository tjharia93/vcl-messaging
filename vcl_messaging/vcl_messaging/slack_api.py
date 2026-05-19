"""VCL Messaging - Slack Integration.

Handles Slack Events API webhooks and Slack Web API for sending messages.
Slack API docs: https://api.slack.com/
"""

import hashlib
import hmac
import json
import time
import frappe
from frappe import _
from frappe.utils import now_datetime, cint
import requests


def _get_slack_config():
    """Get the first enabled Slack channel config."""
    config = frappe.get_all(
        "VCL Channel Config",
        filters={"channel_type": "Slack", "enabled": 1},
        fields=["name", "slack_bot_token", "slack_signing_secret", "slack_default_channel"],
        limit=1,
    )
    return config[0] if config else None


def _verify_slack_signature(signing_secret, timestamp, body, signature):
    """Verify Slack request signature."""
    if abs(time.time() - float(timestamp)) > 60 * 5:
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_signature, signature)


def _get_or_create_slack_contact(user_id, user_info=None):
    """Get or create a VCL Message Contact from Slack user ID."""
    existing = frappe.db.get_value("VCL Message Contact", {"slack_user_id": user_id}, "name")
    if existing:
        return existing

    name = user_info.get("real_name") or user_info.get("name") if user_info else f"Slack User {user_id}"
    email = user_info.get("profile", {}).get("email") if user_info else None

    contact = frappe.get_doc({
        "doctype": "VCL Message Contact",
        "contact_name": name,
        "email": email,
        "slack_user_id": user_id,
    })
    contact.insert(ignore_permissions=True)
    frappe.db.commit()
    return contact.name


def _get_or_create_slack_conversation(contact_name, channel_id, thread_ts=None, channel_config=None):
    """Get or create a Slack conversation."""
    filters = {
        "contact": contact_name,
        "channel": "Slack",
        "slack_channel_id": channel_id,
        "status": ["in", ["Open", "Pending"]],
    }
    if thread_ts:
        filters["slack_thread_ts"] = thread_ts

    existing = frappe.db.get_value("VCL Conversation", filters, "name")
    if existing:
        return existing

    conv = frappe.get_doc({
        "doctype": "VCL Conversation",
        "contact": contact_name,
        "channel": "Slack",
        "channel_config": channel_config,
        "slack_channel_id": channel_id,
        "slack_thread_ts": thread_ts,
        "status": "Open",
    })
    conv.insert(ignore_permissions=True)
    frappe.db.commit()
    return conv.name


def _fetch_slack_user_info(bot_token, user_id):
    """Fetch user info from Slack API."""
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("user", {})
    except Exception:
        pass
    return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def slack_events():
    """Slack Events API webhook endpoint.

    Handles:
    - URL verification challenge
    - message events (incoming messages)
    - app_mention events
    """
    config = _get_slack_config()

    try:
        body = frappe.request.data.decode("utf-8")
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "Invalid request"}

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    if config and config.get("slack_signing_secret"):
        timestamp = frappe.request.headers.get("X-Slack-Request-Timestamp", "")
        signature = frappe.request.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(config["slack_signing_secret"], timestamp, body, signature):
            frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        _handle_slack_event(event, config)

    return {"ok": True}


def _handle_slack_event(event, config):
    """Process a Slack event."""
    event_type = event.get("type")

    if event_type == "message" and not event.get("subtype"):
        _handle_slack_message(event, config)
    elif event_type == "app_mention":
        _handle_slack_message(event, config)


def _handle_slack_message(event, config):
    """Process an incoming Slack message."""
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "")
    ts = event.get("ts")
    thread_ts = event.get("thread_ts")

    if not user_id or event.get("bot_id"):
        return

    msg_id = f"slack-{channel_id}-{ts}"
    if frappe.db.exists("VCL Message", {"external_id": msg_id}):
        return

    bot_token = config.get("slack_bot_token") if config else None
    user_info = _fetch_slack_user_info(bot_token, user_id) if bot_token else None

    contact_name = _get_or_create_slack_contact(user_id, user_info)
    conv_name = _get_or_create_slack_conversation(
        contact_name,
        channel_id,
        thread_ts or ts,
        config.get("name") if config else None,
    )

    sender_name = user_info.get("real_name") if user_info else user_id

    message = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conv_name,
        "direction": "Inbound",
        "message_type": "text",
        "status": "delivered",
        "content": text,
        "external_id": msg_id,
        "sender_name": sender_name,
        "sent_at": now_datetime(),
    })
    message.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def send_slack_message(conversation, content):
    """Send a Slack message.

    Args:
        conversation: VCL Conversation name
        content: Message text

    Returns:
        dict with success status
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    config = _get_slack_config()
    if not config:
        frappe.throw(_("Slack not configured"))

    conv = frappe.get_doc("VCL Conversation", conversation)
    if conv.channel != "Slack":
        frappe.throw(_("Conversation is not a Slack conversation"))

    bot_token = config.get("slack_bot_token")
    channel_id = conv.slack_channel_id
    thread_ts = conv.slack_thread_ts

    if not channel_id:
        frappe.throw(_("Slack channel ID not set"))

    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "channel": channel_id,
        "text": content,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    msg_doc = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conversation,
        "direction": "Outbound",
        "message_type": "text",
        "status": "pending",
        "content": content,
        "sent_at": now_datetime(),
        "sender_name": frappe.session.user,
    })
    msg_doc.insert(ignore_permissions=True)

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        result = response.json()

        if result.get("ok"):
            external_id = f"slack-{channel_id}-{result.get('ts')}"
            msg_doc.db_set("external_id", external_id)
            msg_doc.db_set("status", "sent")

            if not conv.slack_thread_ts and result.get("ts"):
                conv.db_set("slack_thread_ts", result.get("ts"))

            frappe.db.commit()
            return {"success": True, "message_id": msg_doc.name}
        else:
            error = result.get("error", "Unknown error")
            msg_doc.db_set("status", "failed")
            msg_doc.db_set("error_message", error)
            frappe.db.commit()
            return {"success": False, "error": error}

    except requests.RequestException as e:
        msg_doc.db_set("status", "failed")
        msg_doc.db_set("error_message", str(e))
        frappe.db.commit()
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def list_slack_channels():
    """List available Slack channels for the bot."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    config = _get_slack_config()
    if not config:
        return []

    bot_token = config.get("slack_bot_token")

    try:
        resp = requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"types": "public_channel,private_channel", "limit": 100},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return [
                {"id": c["id"], "name": c["name"], "is_private": c.get("is_private", False)}
                for c in data.get("channels", [])
            ]
    except Exception:
        pass
    return []
