"""In-app Help: the Markdown renderer and the /help routes. Isolation via SHOPBOOKS_DATA_DIR."""
import os
import re
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_help_")

import db          # noqa: E402
import helpdocs    # noqa: E402
from testutil import ok  # noqa: E402

db.init()

# --- renderer subset ----------------------------------------------------------
h = helpdocs.render_markdown("# Title\n\nsome **bold** and `code` and [a link](https://x.com).")
ok("<h1>Title</h1>" in h, "heading renders")
ok("<strong>bold</strong>" in h and "<code>code</code>" in h, "bold + inline code render")
ok('<a href="https://x.com">a link</a>' in h, "links render")

h = helpdocs.render_markdown("- one\n- two\n  wrapped\n\n1. first\n2. second")
ok(h.count("<li>") == 4 and "<ul>" in h and "<ol>" in h, "bullet and numbered lists render")
ok("two wrapped" in h, "a wrapped list-item continuation line joins its item")

h = helpdocs.render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
ok("<table>" in h and "<th>A</th>" in h and "<td>1</td>" in h, "GitHub pipe table renders")

h = helpdocs.render_markdown("```\nline1\nline2\n```")
ok("<pre><code>line1\nline2</code></pre>" in h, "fenced code block renders verbatim")

# HTML in source is escaped (no injection through a doc)
h = helpdocs.render_markdown("a <script>x</script> tag")
ok("<script>" not in h and "&lt;script&gt;" in h, "raw HTML in source is escaped")

# --- every whitelisted doc renders cleanly ------------------------------------
for slug, (title, _) in helpdocs.DOCS.items():
    got = helpdocs.get(slug)
    ok(got is not None, f"doc '{slug}' loads")
    _, body = got
    # no leftover raw-markdown artifacts
    clean = not re.search(r"^#{1,6}\s|\*\*|^\s*[-*]\s|^\|", body, re.M)
    ok(clean, f"doc '{slug}' renders with no raw-markdown artifacts")

ok(helpdocs.get("nonexistent") is None, "unknown slug returns None")

# --- routes -------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
client = TestClient(appmod.app)

idx = client.get("/help")
ok(idx.status_code == 200 and b"ShopBooks Help" in idx.content, "/help hub renders")
ok(b"/help/guide" in idx.content and b"/help/email" in idx.content, "hub links to the guides")

doc = client.get("/help/email")
ok(doc.status_code == 200 and b"App Password" in doc.content, "/help/email renders the email guide")

nav = client.get("/")
ok(b'href="/help"' in nav.content, "Help appears in the nav on every page")

miss = client.get("/help/nope", follow_redirects=False)
ok(miss.status_code == 303 and miss.headers["location"] == "/help", "unknown help slug redirects to the hub")

# the old private-GitHub doc links are gone from the pages that had them
for path in ("/settings", "/mileage"):
    body = client.get(path).text
    ok("github.com/outlierworkshop" not in body, f"{path} no longer links to the private GitHub repo")

print("\nHELP TESTS DONE")
