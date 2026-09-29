"""Dev/demo helper (not a runtime dependency): screenshot the generated page.

Usage: python examples/screenshot.py [site/index.html] [docs/screenshot.png]
Needs playwright and a Chromium install.
"""

import os
import sys

from playwright.sync_api import sync_playwright

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "site", "index.html")
dest = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "docs", "screenshot.png")
os.makedirs(os.path.dirname(dest), exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 860, "height": 900}, color_scheme="light")
    page.goto("file:///" + os.path.abspath(src).replace("\\", "/"))
    page.screenshot(path=dest, full_page=True)
    browser.close()
print("saved", dest)
