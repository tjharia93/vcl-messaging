"""VCL Messaging - Email Integration.

Integrates with Frappe's Email Account for sending/receiving emails.
Uses Communication doctype hooks for incoming email processing.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, cint
from frappe.core.doctype.communication.email import make as make_email


def _get_email_config():
    """Get the first enabled Email channel config."""
    config = frappe.get_all(
        "VCL Channel Config",
        filters={"channel_type": "Email", "enabled": 1},
        fields=["name", "email_account", "email_from_address", "email_from_name"],
        limit=1,
    )
    return config[0] if config else None


def _get_or_create_email_contact(email, sender_name=None):
    """Get or create a VCL Message Contact from email address."""
    existing = frappe.db.get_value("VCL Message Contact", {"email": email}, "name")
    if existing:
        return existing

    name = sender_name or email.split("@")[0].replace(".", " ").title()

    contact = frappe.get_doc({
        "doctype": "VCL Message Contact",
        "contact_name": name,
        "email": email,
    })
    contact.insert(ignore_permissions=True)
    frappe.db.commit()
    return contact.name


def _get_or_create_email_conversation(contact_name, subject, thread_id=None, channel_config=None):
    """Get or create an email conversation based on thread ID or subject."""
    if thread_id:
        existing = frappe.db.get_value(
            "VCL Conversation",
            {"channel": "Email", "email_thread_id": thread_id, "status": ["in", ["Open", "Pending"]]},
            "name",
        )
        if existing:
            return existing

    clean_subject = subject
    if clean_subject:
        for prefix in ["Re:", "RE:", "Fwd:", "FWD:", "Fw:"]:
            clean_subject = clean_subject.replace(prefix, "").strip()

    if clean_subject:
        existing = frappe.db.get_value(
            "VCL Conversation",
            {
                "contact": contact_name,
                "channel": "Email",
                "email_subject": clean_subject,
                "status": ["in", ["Open", "Pending"]],
            },
            "name",
        )
        if existing:
            if thread_id:
                frappe.db.set_value("VCL Conversation", existing, "email_thread_id", thread_id)
            return existing

    conv = frappe.get_doc({
        "doctype": "VCL Conversation",
        "contact": contact_name,
        "channel": "Email",
        "channel_config": channel_config,
        "email_subject": clean_subject or "No Subject",
        "email_thread_id": thread_id,
        "status": "Open",
    })
    conv.insert(ignore_permissions=True)
    frappe.db.commit()
    return conv.name


def process_incoming_email(communication):
    """Process an incoming email Communication and create VCL Message.

    Called from hooks when a Communication is inserted.
    """
    if communication.communication_type != "Communication":
        return
    if communication.sent_or_received != "Received":
        return
    if communication.communication_medium != "Email":
        return

    config = _get_email_config()
    if not config:
        return

    msg_id = f"email-{communication.name}"
    if frappe.db.exists("VCL Message", {"external_id": msg_id}):
        return

    sender_email = communication.sender
    sender_name = communication.sender_full_name
    subject = communication.subject
    content = communication.content or communication.text_content or ""

    message_id_header = communication.message_id
    in_reply_to = None
    if hasattr(communication, "_header") and communication._header:
        in_reply_to = communication._header.get("In-Reply-To")

    thread_id = in_reply_to or message_id_header

    contact_name = _get_or_create_email_contact(sender_email, sender_name)
    conv_name = _get_or_create_email_conversation(
        contact_name,
        subject,
        thread_id,
        config.get("name"),
    )

    if content and "<" in content:
        from frappe.utils import strip_html_tags
        plain_content = strip_html_tags(content)
    else:
        plain_content = content

    message = frappe.get_doc({
        "doctype": "VCL Message",
        "conversation": conv_name,
        "direction": "Inbound",
        "message_type": "text",
        "status": "delivered",
        "content": plain_content[:65000] if plain_content else "",
        "external_id": msg_id,
        "sender_name": sender_name or sender_email,
        "sent_at": communication.creation,
    })
    message.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def send_email_message(conversation, content, subject=None):
    """Send an email message.

    Args:
        conversation: VCL Conversation name
        content: Email body (plain text or HTML)
        subject: Optional subject override

    Returns:
        dict with success status
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    config = _get_email_config()
    if not config:
        frappe.throw(_("Email not configured"))

    conv = frappe.get_doc("VCL Conversation", conversation)
    if conv.channel != "Email":
        frappe.throw(_("Conversation is not an Email conversation"))

    contact = frappe.get_doc("VCL Message Contact", conv.contact)
    if not contact.email:
        frappe.throw(_("Contact does not have an email address"))

    email_subject = subject or conv.email_subject
    if not email_subject:
        email_subject = "Message from VCL"
    elif not email_subject.startswith("Re:"):
        email_subject = f"Re: {email_subject}"

    from_email = config.get("email_from_address")
    from_name = config.get("email_from_name") or "VCL"

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
        email_account = config.get("email_account")

        frappe.sendmail(
            recipients=[contact.email],
            sender=f"{from_name} <{from_email}>" if from_email else None,
            subject=email_subject,
            message=content,
            reference_doctype="VCL Conversation",
            reference_name=conversation,
            email_account=email_account,
            now=True,
        )

        msg_doc.db_set("status", "sent")
        msg_doc.db_set("external_id", f"email-out-{msg_doc.name}")
        frappe.db.commit()

        return {"success": True, "message_id": msg_doc.name}

    except Exception as e:
        error_msg = str(e)
        msg_doc.db_set("status", "failed")
        msg_doc.db_set("error_message", error_msg)
        frappe.db.commit()
        return {"success": False, "error": error_msg}


@frappe.whitelist()
def start_email_conversation(to_email, subject, content, contact_name=None):
    """Start a new email conversation.

    Args:
        to_email: Recipient email address
        subject: Email subject
        content: Email body
        contact_name: Optional contact name

    Returns:
        dict with conversation and message IDs
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    config = _get_email_config()
    if not config:
        frappe.throw(_("Email not configured"))

    contact = _get_or_create_email_contact(to_email, contact_name)
    conv_name = _get_or_create_email_conversation(contact, subject, None, config.get("name"))

    result = send_email_message(conv_name, content, subject)

    return {
        "conversation": conv_name,
        "message_id": result.get("message_id"),
        "success": result.get("success"),
        "error": result.get("error"),
    }


def on_communication_insert(doc, method):
    """Hook called when a Communication is inserted."""
    try:
        process_incoming_email(doc)
    except Exception as e:
        frappe.log_error(f"VCL Messaging email processing error: {e}")
