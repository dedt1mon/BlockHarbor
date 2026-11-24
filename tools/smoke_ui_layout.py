"""Render representative production UI controls in a real Chromium browser.

The fixture loads every production stylesheet in the index's cascade order.
No server, account, mod download, or browser library is required.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"

FIXTURE = r"""
<aside><div class="brand"><div>BlockHarbor</div></div><nav><button class="nav sel">Главная</button><button class="nav">Сборки</button></nav></aside>
<main>
  <div class="page-head"><div><h1>Настройки сборки с очень длинным названием</h1></div><div class="actions"><button class="btn">Каталог сборок</button><button class="btn main">Создать сборку</button></div></div>
  <div class="build-filter"><label class="search-field"><i>⌕</i><input class="field" id="buildSearch" aria-label="Поиск" placeholder="Найти сборку"></label><label class="inline-check"><input type="checkbox">Избранное</label></div>
  <div class="build-row active"><button class="build-pick"><span class="mini-icon">M</span><span class="build-name"><strong>Длинное_название_пользовательской_сборки</strong><small>Fabric</small></span><span class="meta">1.20.1</span></button><button class="btn build-favorite-toggle">★</button><button class="btn build-settings"><span>Настройки</span></button><button class="btn build-settings"><span>Выгрузить</span></button><button class="btn build-settings is-published"><span>Опубликовано</span></button></div>
  <div class="tabs"><button class="on">Моды</button><button>Ресурс-паки</button><button>Миры и копии</button><button>Ошибки сборки</button></div>
  <section class="panel stack">
    <div class="field-group"><label>Название<input id="name" class="field" value="Моя сборка"></label><label>Загрузчик<select class="field" id="loader"><option>Fabric</option></select></label></div>
    <label>Память<input class="field" type="number" value="4096"></label>
    <div class="actions"><button id="primary" class="btn main">Сохранить</button><button class="btn danger">Удалить</button></div>
    <div class="care-content-head"><label class="care-search"><i>⌕</i><input class="field" type="search" id="careSearch" placeholder="Найти мод"></label></div>
    <div class="care-list"><div class="care-row"><input class="care-select" type="checkbox" checked><span class="installed-icon">M</span><button class="care-title"><strong>really-long-mod-file-name-1.20.1-fabric.jar</strong><span>Очень длинное описание</span></button><span class="care-version">1.2.3</span><label class="care-switch"><input type="checkbox" checked><span></span></label><span class="care-size">4 МБ</span></div></div>
    <div class="care-list"><div class="empty" id="empty">Пока нет установленных дополнений.</div></div>
    <div class="care-retention-fields"><label>Копий<input class="field" type="number" value="10"></label><label>Лимит, МБ<input class="field" type="number" value="256"></label><button class="btn">Сохранить</button></div>
    <div class="catalog-search"><input type="search" id="catalogSearch" class="field" aria-label="Поиск модов"><button class="btn">Поиск</button></div>
    <div class="catalog-choice"><button class="catalog-result" aria-current="true"><span></span><span class="catalog-copy">Мод</span><span>1 МБ</span></button><button class="btn catalog-mark" aria-pressed="true">+</button></div>
  </section>
  <section class="panel">
    <div class="pack-search"><input class="field" id="packSearch" type="search"><button class="btn">Найти</button></div>
    <div class="pack-filters"><label>Версия<select class="field"><option>1.20.1</option></select></label><label>Категория<select class="field"><option>Приключения</option></select></label><label>Загрузчик<select class="field"><option>NeoForge</option></select></label><label>Сортировка<select class="field"><option>Релевантность</option></select></label></div>
    <div class="pack-grid"><article class="pack-card"><h4>Модпак</h4><p>Описание</p><div class="pack-card-footer"><span class="meta">BH-AB1234567890</span><button class="btn">Скачать</button></div></article></div>
    <form class="community-publish"><label>Имя<input class="field" value="Публикация"></label><div class="field-group"><label>Версия<input class="field" readonly value="1.20.1"></label><label>Загрузчик<input class="field" readonly value="Forge"></label></div><div class="dialog-actions community-publish-actions"><button class="btn" type="button">Закрыть</button><button class="btn main" type="button">Опубликовать</button></div></form>
  </section>
  <section class="panel updater-channel"><div><h2>Загрузки и обновления</h2><p>Канал обновления</p></div><label>Канал<select class="field"><option>Стабильный</option></select></label></section>
  <section class="panel"><label class="updater-auto-control"><span><strong>Автоматические обновления</strong><span>Проверка при запуске</span></span><input type="checkbox" checked></label><article class="transfer-row"><span class="transfer-icon">+</span><div class="transfer-info"><div class="transfer-title"><strong>очень-длинное-название-файла.jar</strong><span class="pill">Выполняется</span></div><progress value="50" max="100"></progress></div><div class="transfer-actions"><button class="btn">Отмена</button></div></article></section>
  <div class="gallery-grid"><figure class="gallery-card selected"><figcaption>Скриншот</figcaption></figure></div>
</main>
<script>
window.addEventListener("load", async () => {
// The geometry reads below flush layout. A headless DOM dump need not paint
// a frame before its virtual-time budget expires, so do not gate on rAF.
const errors = [];
const check = (value, message) => { if (!value) errors.push(message); };
const q = s => document.querySelector(s);
const css = (s, prop) => getComputedStyle(q(s))[prop];
const rect = s => q(s).getBoundingClientRect();
const equal = (a,b) => Math.abs(a-b) < 1;
const fields = ["#name", "#loader", "#buildSearch", "#careSearch", "#catalogSearch", "#packSearch"];
for (const selector of fields) {
  check(equal(rect(selector).height, 44), selector + " height " + rect(selector).height);
  check(css(selector,"borderRadius") === css("#name","borderRadius"), selector + " radius");
  check(css(selector,"borderTopWidth") === "1px", selector + " border");
  check(css(selector,"borderTopColor") === css("#name","borderTopColor"), selector + " border color");
}
check(equal(rect("#primary").height, 44), "primary button height");
check(equal(rect(".catalog-mark").height, rect(".catalog-mark").width), "catalog + button is square");
const before=rect("#name"); q("#name").focus({preventScroll:true});
await new Promise(resolve => setTimeout(resolve, 200));
check(css("#name","outlineStyle")==="none", "duplicate outer input outline");
check(q("#name").matches(":focus"), "input focus selector inactive: documentFocus="+document.hasFocus());
check(css("#name","borderTopColor")!==css("#loader","borderTopColor"), "input focus not distinguishable: focused="+document.activeElement?.id+", input="+css("#name","borderTopColor")+", idle="+css("#loader","borderTopColor")+", hover="+q("#name").matches(":hover")+", focus="+q("#name").matches(":focus")+", documentFocus="+document.hasFocus()+", shadow="+css("#name","boxShadow"));
check(equal(before.width,rect("#name").width)&&equal(before.height,rect("#name").height),"field geometry changed on focus");
q("#name").blur();
check(css(".nav.sel","backgroundColor")===css(".tabs .on","backgroundColor"),"inconsistent selection fill");
check(css(".build-row.active","backgroundColor")===css(".gallery-card.selected","backgroundColor"),"inconsistent card selection");
check(css("#empty","borderTopWidth")==="0px","nested empty-state border");
const pane=q("main");
check(pane.scrollWidth<=pane.clientWidth+1, "horizontal overflow "+pane.scrollWidth+"/"+pane.clientWidth);
for(const selector of [".field-group",".care-list",".pack-grid",".community-publish-actions",".updater-channel"]) {
  const r=rect(selector), p=pane.getBoundingClientRect();
  check(r.left>=p.left-1 && r.right<=p.right+1, selector+" outside main");
}
check(equal(rect(".community-publish-actions .btn").height,q(".community-publish-actions .btn:last-child").getBoundingClientRect().height),"unequal publish buttons");
document.body.dataset.layoutResult=errors.length ? "FAIL" : "PASS";
document.body.dataset.layoutErrors=errors.join("; ");
}, {once:true});
</script>
"""


def main():
    browser = next((p for name in ("google-chrome", "chromium", "chromium-browser") if (p := shutil.which(name))), None)
    if not browser:
        raise RuntimeError("Chromium is required for the layout smoke test")
    index = (UI / "index.html").read_text(encoding="utf-8")
    names = re.findall(r'<link rel="stylesheet" href="/([^"]+)"', index)
    assert len(names) >= 5
    css = "\n".join((UI / name).read_text(encoding="utf-8") for name in names)
    html = '<!doctype html><html lang="ru"><meta charset="utf-8"><style>' + css + '</style><body class="reduced-motion">' + FIXTURE + '</body></html>'
    with tempfile.TemporaryDirectory(prefix="bh-layout-") as tmp:
        root = Path(tmp)
        page = root / "layout.html"
        page.write_text(html, encoding="utf-8")
        for width, height in ((1440, 900), (900, 700), (520, 760)):
            result = subprocess.run(
                [browser, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-gpu", "--disable-background-networking", "--no-first-run",
                 "--force-device-scale-factor=1", "--user-data-dir=" + str(root / str(width)),
                 f"--window-size={width},{height}", "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=45, check=True,
            )
            if 'data-layout-result="PASS"' not in result.stdout:
                evidence = ROOT / "artifacts" / "ui-audit"
                evidence.mkdir(parents=True, exist_ok=True)
                stem = f"controls-layout-{width}x{height}"
                (evidence / (stem + ".html")).write_text(result.stdout, encoding="utf-8")
                (evidence / (stem + "-stderr.txt")).write_text(result.stderr, encoding="utf-8")
                errors = re.search(r'data-layout-errors="([^"]*)"', result.stdout)
                raise AssertionError(f"Layout {width}x{height}: " + (errors[1] if errors else "no browser result"))
            print(f"PASS: Chromium layout {width}x{height}, controls, focus, selection, responsive widths")


if __name__ == "__main__":
    main()
