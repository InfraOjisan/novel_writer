#!/usr/bin/env python3
"""make_epub — 確定した章（chapters/chNN.md）から縦書き・右綴じの EPUB3 を作る。標準ライブラリのみ。

  python3 tools/novelctl.py epub            （novelctl 経由。通常はこちら）
  python3 tools/make_epub.py [--source final|proofread]

入力:
  chapters/chNN.md                 各章（1行目 `# 第N章　章題`）
  output/final_proofread.md        --source proofread のとき、校正済みの結合原稿（`## 第N章` 区切り）
  canon/CANON.md                   タイトル（「タイトル：」の行）
  config.json の "publish"         クレジット、AI生成の表記、評価ページのURL
出力:
  output/<タイトル>.epub
  output/book_meta.json            評価ページに読み込ませる作品情報（章題・設計上の緊張度・各章の仕掛け）
"""
import datetime
import html
import json
import re
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PUB = CFG.get("publish", {})
N_CH = CFG["chapters"]
KANJI = ["〇", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四", "十五", "十六"]


def kn(n):
    return KANJI[n] if 0 <= n < len(KANJI) else str(n)


EV_JA = {"planted": "設置", "reinforced": "補強", "recovered": "回収"}


def esc(s):
    return html.escape(s, quote=True)


def tcy(s):
    """縦中横：半角の1〜3桁の数字や !? を1文字分に収める（1桁も横倒しにしない）。"""
    s = esc(s)
    s = re.sub(r"(?<![0-9A-Za-z])([0-9]{1,3})(?![0-9A-Za-z])", r'<span class="tcy">\1</span>', s)
    s = re.sub(r"(!\?|\?!|!!|\?\?)", r'<span class="tcy">\1</span>', s)
    return s


def title_from_canon():
    f = ROOT / "canon" / "CANON.md"
    if f.exists():
        m = re.search(r"タイトル：\s*(.+)", f.read_text(encoding="utf-8"))
        if m and "（記入）" not in m.group(1):
            return m.group(1).strip().strip("『』「」")
    return PUB.get("fallback_title", "無題")


def load_chapters(source):
    chs = []
    if source == "proofread":
        f = ROOT / "output" / "final_proofread.md"
        if not f.exists():
            sys.exit("output/final_proofread.md がありません（校正前なら --source final）。")
        parts = re.split(r"^#{1,2}\s*第\s*(\d+)\s*章[ 　]*(.*)$", f.read_text(encoding="utf-8"), flags=re.M)
        for k in range(1, len(parts), 3):
            chs.append((int(parts[k]), parts[k + 1].strip(), parts[k + 2].strip()))
    else:
        for i in range(1, N_CH + 1):
            f = ROOT / "chapters" / f"ch{i:02d}.md"
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8").strip()
            head, _, body = text.partition("\n")
            m = re.match(r"#\s*第\s*\d+\s*章[ 　]*(.*)", head)
            chs.append((i, (m.group(1).strip() if m else ""), body.strip()))
    if not chs:
        sys.exit("確定した章がありません。")
    return chs


def paragraphs(body):
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"[＊*◇◆・―\-]{1,}[ 　＊*◇◆]*", line):
            out.append('<p class="break">＊</p>')
        elif line[0] in "「『（(―…":
            out.append(f'<p class="talk">{tcy(line)}</p>')
        else:
            out.append(f"<p>{tcy(line)}</p>")
    return "\n".join(out)


def page(title, body, cls=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja" class="vrtl">
<head><meta charset="UTF-8"/><title>{esc(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body class="{cls}">
{body}
</body>
</html>
"""


CSS = """@charset "UTF-8";
html { writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; }
body { margin: 0; font-family: "Hiragino Mincho ProN", "Hiragino Mincho Pro", "YuMincho", "Yu Mincho", "Noto Serif CJK JP", "Noto Serif JP", "Source Han Serif JP", "IPAexMincho", "IPAMincho", serif-ja, serif; line-height: 1.8; text-align: justify; }
h1, h2 { font-weight: normal; }
h2.chapter { font-size: 1.3em; margin-left: 3em; margin-top: 2em; }
h2.chapter .no { display: block; font-size: 0.8em; margin-bottom: 0.5em; }
p { margin: 0; text-indent: 1em; }
p.talk { text-indent: 0; }
p.break { text-indent: 0; text-align: center; margin: 0 1em; }
.tcy { text-combine-upright: all; -webkit-text-combine: horizontal; -epub-text-combine: horizontal; }
.rate { margin-right: 3em; font-size: 0.85em; text-indent: 0; }
.rate a { text-decoration: none; }
body.titlepage { text-align: center; }
body.titlepage h1 { font-size: 2em; margin-top: 30%; }
body.titlepage .studio { margin-top: 3em; }
body.colophon p { text-indent: 0; }
.credit { margin-top: 1em; }
"""


def image_page(title, href, w=1600, h=2560):
    """画像1枚を画面いっぱいに収める見開き1ページ（電子書籍で一般的な SVG ラッパー方式）。"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja">
<head><meta charset="UTF-8"/><title>{esc(title)}</title>
<style>html,body{{margin:0;padding:0;height:100%;writing-mode:horizontal-tb;}} svg{{display:block;}}</style></head>
<body epub:type="cover">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet">
<image width="{w}" height="{h}" xlink:href="{href}"/>
</svg>
</body>
</html>
"""


def rating_link(n):
    url = PUB.get("rating_url", "").strip()
    if not url:
        return ""
    return (f'<div class="rate"><p>――</p><p>第{kn(n)}章を読み終えたら、ひとことどうぞ。</p>'
            f'<p><a href="{esc(url)}#ch{n}">この章を評価する（五段階・ネタバレなしの一言）</a></p></div>')


def design_curves():
    """BRIEF.md の章立て表から、設計上の緊張度・二人の距離を読む。"""
    tension, distance = [], []
    brief = (ROOT / "BRIEF.md").read_text(encoding="utf-8")
    for m in re.finditer(r"^\|\s*(\d+)\s*\|[^|]*\|[^|]*\|\s*(\d)[^|]*\|\s*(\d)[^|]*\|\s*$", brief, flags=re.M):
        tension.append(int(m.group(2)))
        distance.append(int(m.group(3)))
    return tension[:N_CH], distance[:N_CH]


def devices():
    names = CFG.get("slot_names", {})
    out = {str(i): [] for i in range(1, N_CH + 1)}
    for sid, evs in CFG.get("slot_plan", {}).items():
        for c, ev in evs:
            out[str(c)].append(f"{names.get(sid, sid)}：{EV_JA.get(ev, ev)}")
    return out


def build(source="final"):
    title = title_from_canon()
    chs = load_chapters(source)
    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "novelctl:" + title))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    studio = PUB.get("studio", "")
    credits = PUB.get("credits", [])
    disclosure = PUB.get("ai_disclosure", "")

    files = {}
    files["OEBPS/style.css"] = CSS
    byline = PUB.get("author", "") or studio
    files["OEBPS/title.xhtml"] = page(title, f"<h1>{esc(title)}</h1>" + (f'<p class="studio">{esc(byline)}</p>' if byline else ""), "titlepage")
    manifest, spine, nav_items, ncx_points = [], [], [], []
    images = {}
    cover_meta = ""
    for key, iid, page_id, label in [("cover_image", "cover-img", "cover", "表紙"), ("frontispiece_image", "front-img", "frontis", "口絵")]:
        rel = PUB.get(key, "")
        src = ROOT / rel if rel else None
        if not (src and src.exists()):
            continue
        ext = src.suffix.lower()
        mt = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        name = f"images/{page_id}{ext}"
        images[f"OEBPS/{name}"] = src.read_bytes()
        prop = ' properties="cover-image"' if page_id == "cover" else ""
        manifest.append(f'<item id="{iid}" href="{name}" media-type="{mt}"{prop}/>')
        files[f"OEBPS/{page_id}.xhtml"] = image_page(label, name).replace('epub:type="cover"', 'epub:type="cover"' if page_id == "cover" else "")
        manifest.append(f'<item id="{page_id}" href="{page_id}.xhtml" media-type="application/xhtml+xml" properties="svg"/>')
        spine.append(f'<itemref idref="{page_id}"/>')
        if page_id == "cover":
            cover_meta = '<meta name="cover" content="cover-img"/>'
    manifest.append('<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="title"/>')
    for idx, (n, ctitle, body) in enumerate(chs, start=1):
        name = f"ch{n:02d}.xhtml"
        head = f'<h2 class="chapter"><span class="no">第{kn(n)}章</span>{esc(ctitle)}</h2>'
        files[f"OEBPS/{name}"] = page(f"第{n}章 {ctitle}", head + "\n" + paragraphs(body) + rating_link(n))
        manifest.append(f'<item id="c{n}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{n}"/>')
        label = f"第{kn(n)}章　{ctitle}".strip()
        nav_items.append(f'<li><a href="{name}">{esc(label)}</a></li>')
        ncx_points.append(f'<navPoint id="np{idx}" playOrder="{idx}"><navLabel><text>{esc(label)}</text></navLabel><content src="{name}"/></navPoint>')

    col = [f"<h2>{esc(title)}</h2>"]
    col += [f'<p class="credit">{esc(r)}　{esc(p)}</p>' for r, p in credits]
    if disclosure:
        col.append(f'<p class="credit">{esc(disclosure)}</p>')
    if PUB.get("rating_url"):
        col.append(f'<p class="credit"><a href="{esc(PUB["rating_url"])}">みんなの評価を見る</a></p>')
    col.append(f'<p class="credit">{datetime.date.today().isoformat()}　発行</p>')
    files["OEBPS/colophon.xhtml"] = page("奥付", "\n".join(col), "colophon")
    manifest.append('<item id="colophon" href="colophon.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="colophon"/>')
    nav_items.append('<li><a href="colophon.xhtml">奥付</a></li>')

    files["OEBPS/nav.xhtml"] = page("目次", f'<nav epub:type="toc" id="toc"><h2>目次</h2><ol>{"".join(nav_items)}</ol></nav>')
    files["OEBPS/toc.ncx"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ja">
<head><meta name="dtb:uid" content="{book_id}"/></head>
<docTitle><text>{esc(title)}</text></docTitle>
<navMap>{"".join(ncx_points)}</navMap>
</ncx>
"""
    author = PUB.get("author", "")
    creators = (f'<dc:creator id="cr0">{esc(author)}</dc:creator>' if author else
                "".join(f'<dc:creator id="cr{i}">{esc(p)}</dc:creator>' for i, (r, p) in enumerate(credits[:3])))
    files["OEBPS/content.opf"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ja" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{esc(title)}</dc:title>
<dc:language>ja</dc:language>
{creators}
<dc:publisher>{esc(studio)}</dc:publisher>
<meta property="dcterms:modified">{now}</meta>
<meta name="primary-writing-mode" content="vertical-rl"/>
{cover_meta}
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
{chr(10).join(manifest)}
</manifest>
<spine toc="ncx" page-progression-direction="rtl">
{chr(10).join(spine)}
</spine>
</package>
"""
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_") or "novel"
    epub = out_dir / f"{safe}.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        for name, content in files.items():
            z.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in images.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED)

    tension, distance = design_curves()
    meta = {
        "title": title,
        "chapters": [{"n": n, "title": t} for n, t, _ in chs],
        "design": {"tension": tension, "distance": distance},
        "devices": devices(),
        "edition": now,
    }
    (out_dir / "book_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(re.sub(r"\s", "", b)) for _, _, b in chs)
    print(f"{epub.relative_to(ROOT)} を作成（{len(chs)}章・本文{total}字・縦書き右綴じ）")
    print("output/book_meta.json を作成（評価ページの「作品情報を読み込む」に貼り付ける）")
    if not PUB.get("rating_url"):
        print("注意: config.json の publish.rating_url が空なので、章末の評価リンクは入っていません。")
    return epub


if __name__ == "__main__":
    src = "proofread" if "--source" in sys.argv and sys.argv[sys.argv.index("--source") + 1] == "proofread" else "final"
    build(src)
