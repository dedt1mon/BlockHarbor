"""Layout contracts for the build catalog and diagnostic dialogs."""

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"


def css_rule(css: str, selector: str) -> str:
    pattern = re.compile(re.escape(selector) + r"\s*\{([^{}]*)\}")
    matches = pattern.findall(css)
    if not matches:
        raise AssertionError(f"Missing CSS rule: {selector}")
    return matches[-1]


class CatalogDiagnosticsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs = (UI / "pack-hub.css").read_text(encoding="utf-8")
        cls.experience = (UI / "experience.css").read_text(encoding="utf-8")
        cls.script = (UI / "experience.js").read_text(encoding="utf-8")
        cls.catalog = (UI / "style.css").read_text(encoding="utf-8")

    def test_catalog_scroll_matches_mods_without_extra_green_border(self):
        mods_scroll = css_rule(self.catalog, ".catalog-results,\n.catalog-detail")
        packs_scroll = css_rule(self.packs, ".pack-dialog .pack-results")
        for property_name in ("scrollbar-width: thin;", "scrollbar-color: #59695d transparent;", "scrollbar-gutter: stable;"):
            self.assertIn(property_name, mods_scroll)
            self.assertIn(property_name, packs_scroll)
        self.assertNotIn("border-top", packs_scroll)
        self.assertNotIn("border-bottom", packs_scroll)
        self.assertIn("margin-top: 16px;", packs_scroll)
        self.assertIn("padding: 0 4px 12px 0;", packs_scroll)

    def test_build_errors_heading_is_visually_separated(self):
        rule = css_rule(self.experience, "#buildErrorsPanel > h2")
        self.assertIn("margin: 0 0 18px;", rule)
        self.assertIn('Пока нет записанных ошибок.', self.script)
        self.assertIn('Проверить сборку', self.script)

    def test_diagnostic_buttons_fill_two_equal_columns(self):
        grid = css_rule(self.experience, ".diagnostic-actions")
        controls = css_rule(self.experience, ".diagnostic-actions > .btn")
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", grid)
        self.assertIn("width: 100%;", controls)
        self.assertIn('class="actions diagnostic-actions"', self.script)
        self.assertIn("Управлять содержимым", self.script)

    def test_report_scroll_uses_catalog_style(self):
        scroll = css_rule(self.experience, ".report-text")
        self.assertIn("scrollbar-color: #59695d transparent;", scroll)
        self.assertIn("scrollbar-width: thin;", scroll)
        self.assertIn("scrollbar-gutter: stable;", scroll)
        thumb = css_rule(self.experience, ".report-text::-webkit-scrollbar-thumb")
        self.assertIn("border-radius: 999px;", thumb)

    def test_report_can_return_without_new_diagnostic_query(self):
        self.assertIn('expButton("backToDiagnostics","Вернуться обратно","back")', self.script)
        self.assertIn('diagnosticSession = {profileId: p.id, data};', self.script)
        self.assertIn('const previous = diagnosticSession;', self.script)
        self.assertIn('report = $("#diagnosticReport")?.value ?? report;', self.script)
        self.assertIn('return showDiagnosticResults(profile, previous.data);', self.script)
        self.assertIn('class="actions diagnostic-actions diagnostic-report-actions"', self.script)


if __name__ == "__main__":
    unittest.main()
