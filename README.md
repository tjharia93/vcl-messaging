# vcl_messaging

Unified inbox for Vimit Converters Limited — ingests inbound messages from multiple channels and routes them through a single Frappe data model.

## Channels

| Channel | Module | Endpoint(s) |
| --- | --- | --- |
| WhatsApp Group (Baileys PA) | `whatsapp_pa_api.py` | `record_message` (POST, X-PA-Token) |
| WhatsApp Cloud API (Meta) | `whatsapp_api.py` | webhook |
| Slack | `slack_api.py` | `slack_events` |
| Email (Communication) | `email_api.py` | doc event `Communication.after_insert` |

## Data model

- **VCL Message Contact** — phone / WhatsApp ID / email → CRM contact match
- **VCL Conversation** — one open thread per (channel, contact-or-group); holds last-message preview + unread count
- **VCL Message** — every inbound/outbound message, dedup'd by `external_id`
- **VCL Channel Config** — channel credentials (WhatsApp token, Slack signing secret, PA shared token, Telegram bot token)

## WhatsApp PA (Baileys group listener)

External listener lives at `~/projects/vcl_whatsapp_pa/` (separate Node.js service). It POSTs every group message to:

```
POST /api/method/vcl_messaging.vcl_messaging.whatsapp_pa_api.record_message
X-PA-Token: <pa_shared_token>
```

Token is validated against a `VCL Channel Config` row where `channel_type=WhatsApp Group` and `enabled=1`. Successful ingest dedupes by `wa-pa-{message_id}`, upserts the contact + conversation, inserts the message, and fires a best-effort Telegram alert (if `pa_telegram_bot_token` + `pa_priority_telegram_chat_id` are set on the channel config).

## Install

```bash
cd frappe-bench
bench get-app https://github.com/tjharia93/vcl-messaging
bench --site <site> install-app vcl_messaging
bench --site <site> migrate
```

## License

MIT
