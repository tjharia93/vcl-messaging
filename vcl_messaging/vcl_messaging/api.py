"""VCL Messaging API - WhatsApp Business Cloud API Integration.

Webhook endpoints and messaging APIs for the unified inbox.
WhatsApp Cloud API docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import json
import frappe
from frappe import _
from frappe.utils import now_datetime, cint
import requests


def _get_whatsapp_config():
    """Get the first enabled WhatsApp channel config."""
    config = frappe.get_all(
        "VCL Channel Config",
        filters={"channel_type": "WhatsApp", "enabled": 1},
        fields=["name", "whatsapp_phone_number_id", "whatsapp_access_token", "whatsapp_verify_token"],
        limit=1,
    )
    return config[0] if config else None


def _get_or_create_contact(wa_id, profile_name=None):
    """Get or create a VCL Message Contact from WhatsApp ID."""
    existing = frappe.db.get_value("VCL Message Contact", {"whatsapp_id": wa_id}, "name")
    if existing:
        return existing

    contact = frappe.get_doc({
        "doctype": "VCL Message Contact",
        "contact_name": profile_name or f"WhatsApp {wa_id}",
        "phone": wa_id,
        "whatsapp_id": wa_id,
    })
    contact.insert(ignore_permissions=True)
    frappe.db.commit()
    return contact.name


def _get_or_create_conversation(contact_name, channel="WhatsApp", channel_config=None):
    """Get or create a conversation for a contact."""
    existing = frappe.db.get_value(
        "VCL Conversation",
        {"contact": contact_name, "channel": channel, "status": ["in", ["Open", "Pending"]]},
        "name",
    )
    if existing:
        return existing

    conv = frappe.get_doc({
        "doctype": "VCL Conversation",
        "contact": contact_name,
        "channel": channel,
        "channel_config": channel_config,
        "status": "Open",
    })
    conv.insert(ignore_permissions=True)
    frappe.db.commit()
    return conv.name


@frappe.whitelist(allow_guest=True, methods=["GET"])
def whatsapp_webhook():
    """Webhook verification endpoint for Meta WhatsApp Cloud API.

    Meta sends a GET request with hub.mode, hub.verify_token, hub.challenge.
    We must respond with hub.challenge if the token matches.
    """
    mode = frappe.form_dict.get("hub.mode")
    token = frappe.form_dict.get("hub.verify_token")
    challenge = frappe.form_dict.get("hub.challenge")

    config = _get_whatsapp_config()
    if not config:
        frappe.throw(_("WhatsApp not configured"), frappe.ValidationError)

    if mode == "subscribe" and token == config.get("whatsapp_verify_token"):
        frappe.response["type"] = "text"
        frappe.response["body"] = challenge
        return

    frappe.throw(_("Verification failed"), frappe.AuthenticationError)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def whatsapp_webhook_post():
    """Webhook receiver for incoming WhatsApp messages.

    Handles: messages, message status updates (sent, delivered, read).
    """
    try:
        payload = json.loads(frappe.request.data)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON"}

    entry = payload.get("entry", [])
    for e in entry:
        changes = e.get("changes", [])
        for change in changes:
            value = change.get("value", {})

            if "messages" in value:
                _handle_incoming_messages(value)

            if "statuses" in value:
                _handle_status_updates(value)

    return {"status": "ok"}


def _handle_incoming_messages(value):
    """Process incoming WhatsApp messages."""
    messages = value.get("messages", [])
    contacts = value.get("contacts", [])
    metadata = value.get("metadata", {})

    contact_map = {c.get("wa_id"): c.get("profile", {}).get("name") for c in contacts}
    config = _get_whatsapp_config()

    for msg in messages:
        wa_id = msg.get("from")
        profile_name = contact_map.get(wa_id)
        msg_id = msg.get("id")
        msg_type = msg.get("type", "text")
        timestamp = msg.get("timestamp")

        if frappe.db.exists("VCL Message", {"external_id": msg_id}):
            continue

        contact_name = _get_or_create_contact(wa_id, profile_name)
        conv_name = _get_or_create_conversation(
            contact_name, "WhatsApp", config.get("name") if config else None
        )

        content = ""
        media_url = None
        media_mime = None

        if msg_type == "text":
            content = msg.get("text", {}).get("body", "")
        elif msg_type in ("image", "video", "audio", "document"):
            media_obj = msg.get(msg_type, {})
            content = media_obj.get("caption", "")
            media_url = media_obj.get("id")
            media_mime = media_obj.get("mime_type")
        elif msg_type == "location":
            loc = msg.get("location", {})
            content = f"Location: {loc.get('latitude')}, {loc.get('longitude')}"

        message = frappe.get_doc({
            "doctype": "VCL Message",
            "conversation": conv_name,
            "direction": "Inbound",
            "message_type": msg_type,
            "status": "delivered",
            "content": content,
            "media_url": media_url,
            "media_mime_type": media_mime,
            "external_id": msg_id,
            "sender_name": profile_name,
            "sent_at": now_datetime(),
        })
        message.insert(ignore_permissions=True)
        frappe.db.commit()


def _handle_status_updates(value):
    """Process message status updates (sent, delivered, read)."""
    statuses = value.get("statuses", [])

    for status in statuses:
        msg_id = status.get("id")
        status_value = status.get("status")
        timestamp = status.get("timestamp")

        existing = frappe.db.get_value("VCL Message", {"external_id": msg_id}, "name")
        if not existing:
            continue

        updates = {"status": status_value}
        if status_value == "delivered":
            updates["delivered_at"] = now_datetime()
        elif status_value == "read":
            updates["read_at"] = now_datetime()

        frappe.db.set_value("VCL Message", existing, updates, update_modified=False)
        frappe.db.commit()


@frappe.whitelist()
def send_whatsapp_message(conversation, content, message_type="text"):
    """Send a WhatsApp message via Cloud API.

    Args:
        conversation: VCL Conversation name
        content: Message text content
        message_type: Type of message (text, template, etc.)

    Returns:
        dict with message_id and status
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    config = _get_whatsapp_config()
    if not config:
        frappe.throw(_("WhatsApp not configured"))

    conv = frappe.get_doc("VCL Conversation", conversation)
    contact = frappe.get_doc("VCL Message Contact", conv.contact)

    if not contact.whatsapp_id:
        frappe.throw(_("Contact does not have a WhatsApp ID"))

    phone_number_id = config.get("whatsapp_phone_number_id")
    access_token = config.get("whatsapp_access_token")

    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": contact.whatsapp_id,
        "type": message_type,
    }

    if message_type == "text":
        payload["text"] = {"preview_url": False, "body": content}

    msg_doc = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conversation,
        "direction": "Outbound",
        "message_type": message_type,
        "status": "pending",
        "content": content,
        "sent_at": now_datetime(),
        "sender_name": frappe.session.user,
    })
    msg_doc.insert(ignore_permissions=True)

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()

        external_id = result.get("messages", [{}])[0].get("id")
        msg_doc.db_set("external_id", external_id)
        msg_doc.db_set("status", "sent")
        frappe.db.commit()

        return {"success": True, "message_id": msg_doc.name, "external_id": external_id}

    except requests.RequestException as e:
        error_msg = str(e)
        msg_doc.db_set("status", "failed")
        msg_doc.db_set("error_message", error_msg)
        frappe.db.commit()
        return {"success": False, "error": error_msg}


@frappe.whitelist()
def get_conversations(status=None, channel=None, limit=50):
    """Get conversations for the inbox view."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    filters = {}
    if status:
        filters["status"] = status
    if channel:
        filters["channel"] = channel

    conversations = frappe.get_all(
        "VCL Conversation",
        filters=filters,
        fields=[
            "name", "contact", "channel", "status", "assigned_to",
            "last_message_at", "last_message_preview", "unread_count",
            "linked_customer", "linked_lead", "email_subject",
        ],
        order_by="last_message_at desc",
        limit=cint(limit),
    )

    for conv in conversations:
        contact = frappe.db.get_value(
            "VCL Message Contact", conv["contact"],
            ["contact_name", "phone", "email", "profile_picture"], as_dict=True
        )
        conv["contact_name"] = contact.get("contact_name") if contact else conv["contact"]
        conv["contact_phone"] = contact.get("phone") if contact else None
        conv["contact_email"] = contact.get("email") if contact else None
        conv["contact_image"] = contact.get("profile_picture") if contact else None

    return conversations


@frappe.whitelist()
def get_messages(conversation, limit=50, offset=0):
    """Get messages for a conversation."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    messages = frappe.get_all(
        "VCL Message",
        filters={"conversation": conversation},
        fields=[
            "name", "direction", "message_type", "status", "content",
            "media_url", "sent_at", "delivered_at", "read_at", "sender_name",
        ],
        order_by="sent_at asc",
        limit=cint(limit),
        start=cint(offset),
    )

    frappe.db.set_value(
        "VCL Conversation", conversation, "unread_count", 0, update_modified=False
    )

    return messages


@frappe.whitelist()
def mark_conversation_read(conversation):
    """Mark all messages in a conversation as read."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    frappe.db.set_value(
        "VCL Conversation", conversation, "unread_count", 0, update_modified=False
    )
    return {"success": True}


@frappe.whitelist()
def assign_conversation(conversation, user):
    """Assign a conversation to a user."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    frappe.db.set_value("VCL Conversation", conversation, "assigned_to", user)
    return {"success": True}


@frappe.whitelist()
def update_conversation_status(conversation, status):
    """Update conversation status."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    valid_statuses = ["Open", "Pending", "Resolved", "Closed"]
    if status not in valid_statuses:
        frappe.throw(_("Invalid status"))

    frappe.db.set_value("VCL Conversation", conversation, "status", status)
    return {"success": True}


@frappe.whitelist()
def send_message(conversation, content):
    """Send a message via the appropriate channel.

    Routes to WhatsApp, Email, or Slack based on conversation channel.

    Args:
        conversation: VCL Conversation name
        content: Message content

    Returns:
        dict with success status and message_id
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    conv = frappe.get_doc("VCL Conversation", conversation)
    channel = conv.channel

    if channel == "WhatsApp":
        return send_whatsapp_message(conversation, content)
    elif channel == "Slack":
        from vcl_messaging.vcl_messaging.slack_api import send_slack_message
        return send_slack_message(conversation, content)
    elif channel == "Email":
        from vcl_messaging.vcl_messaging.email_api import send_email_message
        return send_email_message(conversation, content)
    else:
        frappe.throw(_("Unsupported channel: {0}").format(channel))
