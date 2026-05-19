import frappe
from frappe.model.document import Document
from frappe.utils import get_url


class VCLChannelConfig(Document):
    def before_save(self):
        base_url = get_url()
        if self.channel_type == "WhatsApp":
            self.webhook_url = f"{base_url}/api/method/vcl_messaging.vcl_messaging.api.whatsapp_webhook"
        elif self.channel_type == "Slack":
            self.webhook_url = f"{base_url}/api/method/vcl_messaging.vcl_messaging.slack_api.slack_events"
        elif self.channel_type == "Email":
            self.webhook_url = "Uses Frappe Email Account polling"
        else:
            self.webhook_url = None
