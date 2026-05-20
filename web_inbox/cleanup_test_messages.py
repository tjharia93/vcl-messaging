"""Remove synthetic test messages from VCL Messaging — keep only real
WhatsApp traffic. Deletes the 8 curl-injected messages (smoke-test,
mediatest, visiontest, paytest external_ids) and the fake SmokeTest
conversation. Genuine messages are untouched.

Run:  ~/projects/vcl-erpnext-mcp/venv/bin/python cleanup_test_messages.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "projects" / "vcl-erpnext-mcp"))
import frappe_client as fc  # noqa: E402

# 8 synthetic messages, identified by their non-WhatsApp external_id prefixes
SYNTH_MSGS = [
    "MSG-00001",  # wa-pa-smoke-test-...
    "MSG-00029",  # wa-pa-mediatest-...
    "MSG-00041",  # wa-pa-visiontest-...
    "MSG-00061", "MSG-00062", "MSG-00063", "MSG-00064", "MSG-00065",  # wa-pa-paytest-...
]
SYNTH_CONV = "CONV-00001"  # SmokeTest Group — fake JID smoketest@g.us

deleted = 0
for m in SYNTH_MSGS:
    try:
        fc.delete_doc("VCL Message", m)
        deleted += 1
        print(f"deleted  {m}")
    except Exception as e:
        print(f"  skip   {m}: {str(e)[:120]}")

try:
    fc.delete_doc("VCL Conversation", SYNTH_CONV)
    print(f"deleted  {SYNTH_CONV} (SmokeTest Group)")
except Exception as e:
    print(f"  skip   {SYNTH_CONV}: {str(e)[:120]}")

print(f"\n{deleted}/8 synthetic messages removed. Real messages untouched.")
