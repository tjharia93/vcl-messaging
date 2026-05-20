"""VCL Followup — a tracked action raised off an inbox message.

Kept deliberately thin: all create / match / resolve / escalate logic lives
in vcl_messaging.followups_api so the doctype stays a plain record.
"""

import frappe
from frappe.model.document import Document


class VCLFollowup(Document):
    def before_insert(self):
        # Pull the conversation from the linked message if not set.
        if self.message and not self.conversation:
            self.conversation = frappe.db.get_value("VCL Message", self.message, "conversation")
