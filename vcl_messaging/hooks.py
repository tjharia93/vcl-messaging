app_name = "vcl_messaging"
app_title = "VCL Messaging"
app_publisher = "Vimit Converters Limited"
app_description = "Unified inbox for VCL — WhatsApp (Cloud API + Baileys PA), Email, and Slack."
app_email = "tanuj.haria@vimit.com"
app_license = "MIT"

doc_events = {
    "Communication": {
        "after_insert": "vcl_messaging.vcl_messaging.email_api.on_communication_insert",
    },
    "VCL Message": {
        "after_insert": "vcl_messaging.vcl_messaging.whatsapp_api.dispatch_to_n8n",
    },
}
