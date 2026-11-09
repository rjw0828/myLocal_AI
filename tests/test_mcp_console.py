"""Flask rendering/asset smoke checks; not browser layout verification."""
from html.parser import HTMLParser
import unittest

from app import create_app
from sqlite_test_support import isolated_database


class SectionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = []
        self.manager_parents = None
        self.scripts = []
        self.elements = {}
        self.in_main = False
        self.dialog = None
        self.registration_dialog = None
        self.dialog_inside_main = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.elements[attrs["id"]] = (tag, attrs)
        if tag == "main":
            self.in_main = True
        if tag == "dialog":
            self.dialog = attrs.get("id")
            self.dialog_inside_main = self.in_main
        if tag == "form" and attrs.get("id") == "mcp-register":
            self.registration_dialog = self.dialog
        if tag == "section":
            if attrs.get("id") == "mcp-manager":
                self.manager_parents = list(self.sections)
            self.sections.append(attrs.get("id"))
        if tag == "script":
            self.scripts.append(attrs.get("src"))

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main = False
        if tag == "dialog":
            self.dialog = None
        if tag == "section" and self.sections:
            self.sections.pop()


class MCPConsoleTests(unittest.TestCase):
    def setUp(self):
        isolated_database(self)
        self.app = create_app()
        self.client = self.app.test_client()
        self.addCleanup(self.app.extensions["mcp_service"].shutdown)

    def test_page_nests_manager_in_servers_without_initializing_registry_or_runtime(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        parser = SectionParser()
        parser.feed(response.get_data(as_text=True))
        self.assertIn("server-view", parser.manager_parents)
        self.assertEqual(parser.scripts[-1], "/static/js/mcp-ui.js")
        service = self.app.extensions["mcp_service"]
        self.assertIsNone(service._registry)
        self.assertIsNone(service._runtime)
        self.assertFalse(service.database_path.exists())

    def test_mcp_asset_is_served_without_touching_sessions_or_starting_mcp(self):
        for asset in ("mcp-ui.js", "mcp-schema.js"):
            with self.client.get("/static/js/" + asset) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.mimetype, {"text/javascript", "application/javascript"})
        service = self.app.extensions["mcp_service"]
        self.assertIsNone(service._registry)
        self.assertIsNone(service._runtime)
        self.assertEqual(self.client.get("/sessions").json["sessions"], [])

    def test_registration_is_in_a_native_modal_and_area_titles_share_one_style(self):
        parser = SectionParser()
        parser.feed(self.client.get("/").get_data(as_text=True))
        self.assertEqual(parser.elements["mcp-dialog"][0], "dialog")
        self.assertEqual(parser.registration_dialog, "mcp-dialog")
        self.assertFalse(parser.dialog_inside_main)
        self.assertEqual(parser.elements["mcp-add"][1]["aria-controls"], "mcp-dialog")
        for heading in ("mcp-servers-title", "mcp-tools-title"):
            self.assertEqual(parser.elements[heading][0], "h3")
            self.assertEqual(parser.elements[heading][1]["class"], "mcp-area-title")
        self.assertEqual(parser.elements["mcp-tools-hint"][0], "p")


if __name__ == "__main__":
    unittest.main()
