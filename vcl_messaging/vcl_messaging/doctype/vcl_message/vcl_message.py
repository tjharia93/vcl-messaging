import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class VCLMessage(Document):
    def after_insert(self):
        self._update_conversation_preview()

    def _update_conversation_preview(self):
        if self.conversation:
            preview = (self.content or "")[:100]
            if len(self.content or "") > 100:
                preview += "..."
            frappe.db.set_value(
                "VCL Conversation",
                self.conversation,
                {
                    "last_message_at": self.sent_at or now_datetime(),
                    "last_message_preview": preview,
                },
                update_modified=False,
            )
            if self.direction == "Inbound":
                frappe.db.sql(
                    """UPDATE `tabVCL Conversation`
                    SET unread_count = unread_count + 1
                    WHERE name = %s""",
                    self.conversation,
                )
