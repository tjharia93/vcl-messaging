app_name = "vcl_messaging"
app_title = "VCL Messaging"
app_publisher = "Vimit Converters Limited"
app_description = "Unified inbox for VCL — WhatsApp (Cloud API + Baileys PA), Email, and Slack."
app_email = "tanuj.haria@vimit.com"
app_license = "MIT"

# Custom Fields shipped with the app — kept under fixtures/ so messaging
# doctype changes live in this repo, not as ad-hoc Custom Fields on the
# live site. Re-export with: bench --site <site> export-fixtures
fixtures = [
    {"dt": "Custom Field", "filters": [["name", "in", [
        "VCL Message-custom_inbox_tag",
        "VCL Message-custom_inbox_done",
        "VCL Message-custom_inbox_note",
        "VCL Inbox Tag-custom_creates_control_item",
    ]]]},
]

doc_events = {
    "Communication": {
        "after_insert": "vcl_messaging.vcl_messaging.email_api.on_communication_insert",
    },
    "VCL Message": {
        "after_insert": "vcl_messaging.vcl_messaging.whatsapp_api.dispatch_to_n8n",
    },
}

scheduler_events = {
    "daily": [
        # Flag any Follow-up still Open past its deadline (Telegram to escalate_to).
        "vcl_messaging.vcl_messaging.followups_api.escalate_followups",
    ],
}
