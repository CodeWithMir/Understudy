"""The demo ERP portals are styled from local files (no CDN / Google Fonts), so they also work offline."""
import unittest

from tests.wizard_helpers import ROOT  # noqa: F401  (sets sys.path)


class TestPortalStatic(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import server
        self.c = TestClient(server.app)

    def test_portals_reference_only_local_assets(self):
        for page in ("/portal1", "/portal2"):
            html = self.c.get(page).text
            self.assertIn('href="/erp-static/portal.css"', html)
            self.assertNotIn("cdn.tailwindcss.com", html)
            self.assertNotIn("fonts.googleapis.com", html)

    def test_css_and_fonts_are_served(self):
        css = self.c.get("/erp-static/portal.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers["content-type"])
        for cls in (".bg-slate-950", ".rounded-xl", ".text-emerald-400"):
            self.assertIn(cls, css.text)
        font = self.c.get("/erp-static/fonts/plus-jakarta-sans-latin-400-normal.woff2")
        self.assertEqual(font.status_code, 200)
        self.assertEqual(font.headers["content-type"], "font/woff2")

    def test_nothing_else_is_reachable(self):
        for bad in ("/erp-static/../mock_erp.py", "/erp-static/%2e%2e/mock_erp.py", "/erp-static/fonts/OFL-JetBrainsMono.txt",
                    "/erp-static/nope.css"):
            self.assertEqual(self.c.get(bad).status_code, 404, bad)


if __name__ == "__main__":
    unittest.main()
