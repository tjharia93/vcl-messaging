"""Deploy the 3 VCL Inbox design mockups as Frappe Web Pages.

  /vcl-inbox-a  — Daylight  (light, crisp)
  /vcl-inbox-b  — Console   (dark ops console)
  /vcl-inbox-c  — Paper     (warm editorial)

All three share mock.html + mock.js; only the theme CSS differs.
Run:  ~/projects/vcl-erpnext-mcp/venv/bin/python deploy_mockups.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "projects" / "vcl-erpnext-mcp"))
import frappe_client as fc  # noqa: E402

HERE = Path(__file__).parent
html = (HERE / "mock.html").read_text()
js = (HERE / "mock.js").read_text()

OPTIONS = [
    ("vcl-inbox-a", "VCL Inbox — Option A (Daylight)", "theme-daylight.css"),
    ("vcl-inbox-b", "VCL Inbox — Option B (Console)", "theme-console.css"),
    ("vcl-inbox-c", "VCL Inbox — Option C (Paper)", "theme-paper.css"),
]

for route, title, cssfile in OPTIONS:
    css = (HERE / cssfile).read_text()
    payload = {
        "title": title,
        "route": route,
        "published": 1,
        "content_type": "HTML",
        "main_section_html": html,
        "css": css,
        "javascript": js,
        "insert_style": 1,
        "full_width": 1,
        "show_title": 0,
    }
    existing = fc.list_docs("Web Page", filters={"route": route}, fields=["name"], limit=1)
    if existing:
        fc.update_doc("Web Page", existing[0]["name"], payload)
        print(f"updated  /{route}  ({title})")
    else:
        fc.create_doc("Web Page", payload)
        print(f"created  /{route}  ({title})")
