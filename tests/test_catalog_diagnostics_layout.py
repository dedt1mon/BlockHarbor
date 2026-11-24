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
    return "\n".join(matches)


class CatalogDiagnosticsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs = (UI / "pack-hub.css").read_text(encoding="utf-8")
        cls.experience = (UI / "experience.css").read_text(encoding="utf-8")
        cls.script = (UI / "experience.js").read_text(encoding="utf-8")
        cls.app = (UI / "app.js").read_text(encoding="utf-8")
        cls.pack_ui = (UI / "pack-hub.js").read_text(encoding="utf-8")
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
        rule = css_rule(self.catalog, ".build-errors-heading")
        self.assertIn("margin-bottom: 16px;", rule)
        self.assertIn("display: flex;", rule)
        self.assertIn("justify-content: space-between;", rule)
        self.assertIn("margin: 0;", css_rule(self.catalog, ".build-errors-heading h2"))
        self.assertIn('Пока нет записанных ошибок.', self.script)
        self.assertIn('Проверить сборку', self.app)

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


    def test_diagnostic_action_is_next_to_error_section_heading(self):
        header = self.app.split('function addons()', 1)[1].split('async function showSteamCheck()', 1)[0]
        self.assertIn('btn("folder", "Папка сборки", "folder")', header)
        self.assertIn('kind === "errors"', header)
        self.assertIn('class="build-errors-heading"><h2>Ошибки сборки</h2><div class="build-error-actions">', header)
        self.assertIn('data-experience="diagnostics"', header)
        self.assertLess(header.index('data-experience="diagnostics"'), header.index('data-experience="clearBuildErrors"'))
        self.assertLess(header.index('btn("folder", "Папка сборки", "folder")'),
                        header.index('data-experience="diagnostics"'))
        listing = self.script.split('async function loadBuildErrors()', 1)[1].split('function showDiagnosticResults', 1)[0]
        self.assertNotIn('expButton("diagnostics"', listing)
        self.assertIn('Пока нет записанных ошибок.', listing)

    def test_empty_error_state_fills_full_container_width(self):
        box = css_rule(self.experience, "#buildErrorsPanel [data-error-list] > .empty")
        self.assertIn('width: 100%;', box)
        self.assertIn('max-width: none;', box)
        self.assertIn('margin: 0;', box)

    def test_report_buttons_have_requested_order_and_short_label(self):
        report = self.script.split('if (action === "report")', 1)[1].split('if (action === "backToDiagnostics")', 1)[0]
        self.assertIn('expButton("backToDiagnostics","Вернуться обратно","back")', report)
        self.assertIn('expButton("saveReport","Сохранить отчёт","download")', report)
        self.assertLess(report.index('expButton("backToDiagnostics"'), report.index('expButton("saveReport"'))
        self.assertNotIn('Сохранить проверенный отчёт', report)

    def test_share_dialog_publish_action_uses_footer_inside_form(self):
        publishing = self.pack_ui.split('function showPublish(', 1)[1].split('function setPublicationBusy(', 1)[0]
        self.assertIn('id="communityPublishForm"', publishing)
        self.assertIn('class="community-publish-footer"', publishing)
        self.assertIn('type="submit" data-pack-publish-submit', publishing)
        self.assertNotIn('headerActions:', publishing)
        self.assertLess(publishing.index('id="communityPublishSubmit"'), publishing.index('class="community-publication-status"'))
        self.assertLess(publishing.index('id="communityPublishSubmit"'), publishing.index('</form>'))
        self.assertNotIn('community-publish-actions', publishing)
        self.assertNotIn('Закрыть', publishing)
        self.assertIn('form.elements.namedItem("communityPublishSubmit")', self.pack_ui)
        checkbox = css_rule(self.packs, ".community-config-toggle input")
        self.assertIn('margin: 0;', checkbox)
        self.assertIn('align-self: center;', checkbox)

    def test_published_dialog_uses_visible_code_without_dropdown(self):
        details = self.pack_ui.split('function publicationRows(', 1)[1].split('async function readPublications(', 1)[0]
        self.assertNotIn('<details', details)
        self.assertNotIn('<summary', details)
        self.assertIn('class="community-code-label">Код сборки</label>', details)
        self.assertIn('data-pack-copy-id', details)
        self.assertIn('Удалить публикацию', details)
        self.assertNotIn('Закрыть', details)
        self.assertLess(details.index('Код сборки</label>'), details.index('Скопировать ID'))
        self.assertLess(details.index('Скопировать ID'), details.index('Удалить публикацию'))
        self.assertIn('data-pack-withdraw=', details)
        self.assertNotIn('data-pack-withdraw-confirm', details)

    def test_publication_fetch_error_and_direct_removal_are_handled(self):
        self.assertIn('form.classList.remove("is-checking")', self.pack_ui)
        self.assertIn('form.classList.toggle("is-published", publications.length > 0)', self.pack_ui)
        self.assertIn('packPublicationRetry', self.pack_ui)
        self.assertIn('if (d.packWithdraw) return await publicationAction(target.closest("form"), "communityUnpublish", {build: d.packWithdraw})', self.pack_ui)
        self.assertNotIn('data-pack-keep', self.pack_ui)
        self.assertNotIn('data-pack-withdraw-confirm', self.pack_ui)
        self.assertIn('grid-template-columns: minmax(0, 1fr) minmax(142px, .8fr);', self.packs)


if __name__ == "__main__":
    unittest.main()
