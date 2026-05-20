"""Deploy the VCL Inbox as a Frappe Web Page at /vcl-inbox.

Reads inbox.html / inbox.css / inbox.js from this directory and creates
(or updates) the Web Page doc on vimitconverters.frappe.cloud, reusing the
vcl-erpnext-mcp Frappe connection.

Run:  ~/projects/vcl-erpnext-mcp/venv/bin/python deploy_webpage.py
"""
import sys
from pathlib import Path

MCP_DIR = Path.home() / "projects" / "vcl-erpnext-mcp"
sys.path.insert(0, str(MCP_DIR))

import frappe_client as fc  # noqa: E402

HERE = Path(__file__).parent
ROUTE = "vcl-inbox"
TITLE = "VCL Inbox"

html = (HERE / "inbox.html").read_text()
css = (HERE / "inbox.css").read_text()
js = (HERE / "inbox.js").read_text()

payload = {
    "title": TITLE,
    "route": ROUTE,
    "published": 1,
    "content_type": "HTML",
    "main_section_html": html,
    "css": css,
    "javascript": js,
    "insert_style": 1,
    "full_width": 1,
    "show_title": 0,
}

existing = fc.list_docs("Web Page", filters={"route": ROUTE}, fields=["name"], limit=1)
if existing:
    name = existing[0]["name"]
    fc.update_doc("Web Page", name, payload)
    print(f"updated Web Page '{name}'  ->  /{ROUTE}")
else:
    doc = fc.create_doc("Web Page", payload)
    print(f"created Web Page '{doc.get('name')}'  ->  /{ROUTE}")
