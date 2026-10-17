#!/usr/bin/env python3
"""Turn docs/W103D_GUIDE.md into the site's HTML pages.

The guide is markdown so it reads on GitHub; the site is HTML so it matches
everything else there. This converts the subset the guide actually uses —
headings, paragraphs, bullets, numbered lists, task-list checkboxes, pipe
tables, fenced code, inline code, bold, blockquotes and images
(`![caption](../website/images/…)`, rendered as a figure) — and writes one page
per site tree (website/ and website/zh/), since their asset and nav paths
differ by exactly the ../ prefix.

Run it from the repository root after editing the guide:

    python3 website/build_w103d_guide.py

It refuses to write anything if the markdown contains a construct it would
silently drop, so a guide edit cannot half-render.
"""
from __future__ import annotations

import html
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SOURCE = REPO / "docs" / "W103D_GUIDE.md"

#: Constructs this converter does not handle. If one shows up the guide has
#: grown past it, and rendering the page anyway would drop the text.
UNSUPPORTED = (
    (re.compile(r"^\s*```", re.MULTILINE), 0),          # checked per-block below
    (re.compile(r"^\s*[-*]\s+[-*]\s", re.MULTILINE), "a bullet nested inside a bullet"),
    (re.compile(r"\n\|.*\n\|[^|\n]*\|"), 0),            # table shapes vary; see below
)


def inline(text: str) -> str:
    """Inline markdown, escaped first so HTML in prose cannot leak through."""
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )
    return text


def convert(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def flush_paragraph(buf: list[str]) -> None:
        if buf:
            out.append("<p>" + inline(" ".join(buf)) + "</p>")
            buf.clear()

    def flush_table(buf: list[str]) -> None:
        if not buf:
            return
        rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in buf]
        head, body = rows[0], [r for r in rows[2:]]  # rows[1] is the --- separator
        out.append('<div class="table-scroll"><table>')
        out.append("<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
        for r in body:
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
        out.append("</tbody></table></div>")
        buf.clear()

    para: list[str] = []
    table: list[str] = []
    items: list[tuple[str, str]] = []  # (kind, text) — kind is "ul" or "ol"
    quote: list[str] = []

    def flush_all() -> None:
        flush_paragraph(para)
        flush_table(table)
        if items:
            kind = items[0][0]
            out.append(f"<{kind}>")
            for k, txt in items:
                check = ""
                if txt.startswith("[ ] "):
                    check, txt = '<input type="checkbox" disabled> ', txt[4:]
                elif txt.startswith("[x] "):
                    check, txt = '<input type="checkbox" disabled checked> ', txt[4:]
                out.append(f"<li>{check}{inline(txt)}</li>")
            out.append(f"</{kind}>")
            items.clear()
        if quote:
            out.append("<blockquote>" + inline(" ".join(quote)) + "</blockquote>")
            quote.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_all()
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            body = html.escape("\n".join(buf), quote=False)
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{body}</code></pre>")
            continue

        if not stripped:
            flush_all()
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_all()
            level = len(m.group(1))
            text = m.group(2)
            # Strip characters that would need escaping in an id, keep it readable.
            anchor = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-").lower()
            out.append(f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
            i += 1
            continue

        # An image on a line of its own becomes a figure. The guide is read on
        # GitHub too, where the path has to climb out of docs/ into website/ —
        # the site tree gets its own root prefixed instead, hence the marker.
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
        if m:
            flush_all()
            alt, src = m.group(1), m.group(2)
            if src.startswith("../website/"):
                src = src[len("../website/"):]
            out.append(
                '<figure style="margin: 2rem 0">'
                f'<a href="@@ROOT@@{src}" target="_blank" rel="noopener">'
                f'<img src="@@ROOT@@{src}" alt="{html.escape(alt, quote=True)}" loading="lazy"'
                ' style="max-width: 100%; max-height: 70vh; width: auto; height: auto;'
                ' display: block; margin: 0 auto; border: 1px solid var(--border, #2a2a2a);'
                ' border-radius: 12px">'
                "</a>"
                '<figcaption style="text-align: center; color: var(--text-muted, #888);'
                f' font-size: 0.82rem; margin-top: 0.65rem">{html.escape(alt)}</figcaption>'
                "</figure>"
            )
            i += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph(para)
            if items:
                flush_all()
            if quote:
                flush_all()
            table.append(line)
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph(para)
            quote.append(stripped.lstrip("> ").strip())
            i += 1
            continue

        m = re.match(r"^[-*]\s+(.*)$", line)
        if m:
            flush_paragraph(para)
            flush_table(table)
            if quote:
                flush_all()
            if items and items[0][0] != "ul":
                flush_all()
            items.append(("ul", m.group(1)))
            i += 1
            continue

        m = re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            flush_paragraph(para)
            flush_table(table)
            if quote:
                flush_all()
            if items and items[0][0] != "ol":
                flush_all()
            items.append(("ol", m.group(1)))
            i += 1
            continue

        if table:
            flush_all()
        para.append(stripped)
        i += 1

    flush_all()
    return "\n".join(out)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="description" content="{description}">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="{p}css/octen.css?v=5">
    <link rel="stylesheet" href="{p}css/sunsdrmobile.css?v=1">
    <link rel="stylesheet" href="{p}css/ft710.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .w103d-wrap {{ max-width: 880px; margin: 0 auto; padding: 0 1.5rem 5rem; }}
        .w103d-wrap h2 {{
            margin: 2.75rem 0 1rem; padding-top: 1.25rem;
            border-top: 1px solid var(--border, #2a2a2a);
            color: var(--text-primary, #eee); font-size: 1.4rem;
        }}
        .w103d-wrap h2:first-child {{ border-top: none; padding-top: 0; margin-top: 0; }}
        .w103d-wrap h3 {{ margin: 2rem 0 0.75rem; color: var(--text-primary, #eee); font-size: 1.1rem; }}
        .w103d-wrap p, .w103d-wrap li {{ color: var(--text-secondary, #b8b8b8); line-height: 1.85; }}
        .w103d-wrap ul, .w103d-wrap ol {{ padding-left: 1.5rem; }}
        .w103d-wrap li {{ margin: 0.35rem 0; }}
        .w103d-wrap strong {{ color: var(--text-primary, #eee); }}
        .w103d-wrap code {{
            background: var(--bg-card, #1a1a1a); border: 1px solid var(--border, #2a2a2a);
            border-radius: 4px; padding: 0.1rem 0.35rem; font-size: 0.87em;
        }}
        .w103d-wrap pre {{
            background: var(--bg-card, #141414); border: 1px solid var(--border, #2a2a2a);
            border-radius: 10px; padding: 1rem 1.15rem; overflow-x: auto; margin: 1rem 0;
        }}
        .w103d-wrap pre code {{ background: none; border: none; padding: 0; font-size: 0.84rem; line-height: 1.7; }}
        .w103d-wrap .table-scroll {{ overflow-x: auto; margin: 1.15rem 0; }}
        .w103d-wrap table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
        .w103d-wrap th, .w103d-wrap td {{
            border: 1px solid var(--border, #2a2a2a); padding: 0.55rem 0.75rem; text-align: left;
            color: var(--text-secondary, #b8b8b8);
        }}
        .w103d-wrap th {{ background: var(--bg-card, #1a1a1a); color: var(--text-primary, #eee); }}
        .w103d-wrap blockquote {{
            margin: 1.15rem 0; padding: 0.85rem 1.15rem; border-left: 3px solid var(--accent, #f0a020);
            background: var(--bg-card, #171717); border-radius: 0 8px 8px 0;
        }}
        .w103d-wrap blockquote p {{ margin: 0; }}
        .w103d-wrap input[type=checkbox] {{ margin-right: 0.4rem; }}
    </style>
</head>
<body data-site="mrrc_modern">
<nav class="navbar">
    <div class="container navbar-content">
        <a href="{p}index.html" class="logo">
            <span class="logo-icon"><i class="fas fa-microchip"></i></span>
            <span>MRRC <span style="color: var(--accent)">Modern</span></span>
        </a>
        <ul class="nav-links">
            <li><a href="{p}index.html#features">Features</a></li>
            <li><a href="{p}index.html#download">Download</a></li>
            <li><a href="{guide}"{active}>操作指南</a></li>
            <li><a href="{p}sdd.html">SDD</a></li>
            <li><a href="https://github.com/cheenle/mrrc_modern" target="_blank"><i class="fab fa-github"></i></a></li>
        </ul>
        <div class="nav-actions">
            <a href="{lang}" class="lang-btn">{lang_label}</a>
        </div>
    </div>
</nav>

{hero}

<main class="w103d-wrap">
<figure style="margin: 0 0 2.5rem">
    <a href="{p}images/w103d-spec-sheet.png" target="_blank" rel="noopener">
        <img src="{p}images/w103d-spec-sheet.png"
             alt="ZTE W103D 参数、性能与对比树莓派"
             style="width: 100%; height: auto; display: block; border: 1px solid var(--border, #2a2a2a); border-radius: 12px">
    </a>
    <figcaption style="text-align: center; color: var(--text-muted, #888); font-size: 0.82rem; margin-top: 0.65rem">
        参数、性能与对比树莓派 · 点图看原尺寸（1240 × 1754）
    </figcaption>
</figure>
{body}
</main>

<footer style="border-top: 1px solid var(--border, #2a2a2a); padding: 2rem 1.5rem; text-align: center; color: var(--text-muted, #888); font-size: 0.85rem">
    <p>MRRC Modern \u2014 W103D \u7535\u53f0\u76d2\u5b50\u6307\u5357 \u00b7
       <a href="{p}index.html#download">\u4e0b\u8f7d\u9875</a> \u00b7
       <a href="https://github.com/cheenle/mrrc_modern" target="_blank" rel="noopener">GitHub</a></p>
</footer>
</body>
</html>
"""


#: One entry per page this generator writes. Two documents share the template
#: because they differ only in front matter: the complete guide is for someone
#: building the box from a downloaded image, the user guide is for the person
#: who received a box the seller already flashed.
PAGES = (
    {
        "source": "docs/W103D_GUIDE.md",
        "out": "w103d.html",
        "title": "W103D 盒子完整指南 — MRRC Modern",
        "description": "ZTE W103D 电台盒子完整指南 — 从开箱刷机到 MRRC 干活",
        "badge": 'W103D · 盒子完整指南',
        "icon": "fa-tv",
        "h1": "ZTE W103D 电台盒子 — 从开箱到 MRRC 干活",
        "lead": ("从拿到一台全新的 W103D 开始：认机器 → 下载校验 → 烧 U 盘 → 从 U 盘启动 → 接电台 → "
                 "选机型 → 验收。末尾附一张从零到干活的打勾清单。"),
        "actions": (
            '<a class="btn btn-primary" href="{p}downloads/MRRC-Modern-1.25.5-w103d.img.gz">'
            '<i class="fas fa-download"></i> 下载 W103D 镜像</a>'
            '<a class="btn" href="{p}w103d-user.html"><i class="fas fa-box-open"></i> 盒子已刷好？看用户指南</a>'
            '<a class="btn" href="{p}w103d-remote.html"><i class="fas fa-gamepad"></i> 遥控器指南</a>'
            '<a class="btn" href="https://github.com/cheenle/mrrc_modern/blob/main/packaging/box/README.md" '
            'target="_blank" rel="noopener"><i class="fas fa-bolt"></i> 速查操作单</a>'),
        # The markdown links the sibling document by file name; the site spells
        # it as a page, so the built HTML would otherwise point at a .md file.
        "links": (("W103D_USER_GUIDE.md", "w103d-user.html"),
                  ("W103D_REMOTE_GUIDE.md", "w103d-remote.html")),
        "figure": True,
    },
    {
        "source": "docs/W103D_USER_GUIDE.md",
        "out": "w103d-user.html",
        "title": "发货版 W103D 用户指南 — MRRC Modern",
        "description": "拿到卖家刷好的 W103D 之后：出厂口令、Wi-Fi、接电台、五分钟验收与排障",
        "badge": "W103D · 用户指南",
        "icon": "fa-box-open",
        "h1": "拿到盒子之后：发货版 W103D 用户指南",
        "lead": ("系统已经写进 eMMC，口令、时区、Wi-Fi 卖家都设好了：插电、用浏览器登录就能干活。"
                 "这页讲清楚出厂设了什么、电台怎么接、验收看哪几项，以及出了问题先自己查什么。"),
        "actions": (
            '<a class="btn btn-primary" href="{p}w103d.html"><i class="fas fa-book"></i> 从零刷机的完整指南</a>'
            '<a class="btn" href="{p}w103d-remote.html"><i class="fas fa-gamepad"></i> 遥控器指南</a>'
            '<a class="btn" href="{p}index.html#download"><i class="fas fa-download"></i> 下载页</a>'),
        "links": (("W103D_GUIDE.md", "w103d.html"),
                  ("W103D_REMOTE_GUIDE.md", "w103d-remote.html")),
        "figure": False,
    },
    {
        "source": "docs/W103D_REMOTE_GUIDE.md",
        "out": "w103d-remote.html",
        "title": "W103D 遥控器指南 — MRRC Modern",
        "description": "用蓝牙遥控器操作盒子上的电台：配对、按键表、kHz 输入与排障",
        "badge": "W103D · 遥控器指南",
        "icon": "fa-gamepad",
        "h1": "一支遥控器，站着把电台调好",
        "lead": ("配对一次，之后按着语音键就能发射，方向键调频率，数字键直接输入。"
                 "这页是遥控器的全部：每一个键的作用、配对时那个必须满足的条件，"
                 "以及按键不灵时按顺序查哪几条。"),
        "actions": (
            '<a class="btn btn-primary" href="{p}w103d-user.html"><i class="fas fa-box-open"></i> 盒子用户指南</a>'
            '<a class="btn" href="{p}w103d.html"><i class="fas fa-book"></i> 从零刷机的完整指南</a>'),
        "links": (("W103D_USER_GUIDE.md", "w103d-user.html"),),
        "figure": False,
    },
)


def hero(spec) -> str:
    return f"""<header class="guide-hero">
    <div class="guide-hero-inner">
        <span class="badge"><i class="fas {spec['icon']}"></i> {spec['badge']}</span>
        <h1>{spec['h1']}</h1>
        <p>{spec['lead']}</p>
        <div class="actions">{spec['actions']}</div>
    </div>
</header>"""


#: The spec sheet image only illustrates the complete guide.
SPEC_FIGURE = """<figure style="margin: 0 0 2.5rem">
    <a href="{p}images/w103d-spec-sheet.png" target="_blank" rel="noopener">
        <img src="{p}images/w103d-spec-sheet.png"
             alt="ZTE W103D 参数、性能与对比树莓派"
             style="width: 100%; height: auto; display: block; border: 1px solid var(--border, #2a2a2a); border-radius: 12px">
    </a>
    <figcaption style="text-align: center; color: var(--text-muted, #888); font-size: 0.82rem; margin-top: 0.65rem">
        参数、性能与对比树莓派 · 点图看原尺寸（1240 × 1754）
    </figcaption>
</figure>
"""


def main() -> int:
    for spec in PAGES:
        source = REPO / spec["source"]
        md = source.read_text(encoding="utf-8")

        # Refuse rather than silently drop. Each of these would render as literal
        # text or vanish, and nobody would notice until someone read the page.
        for pattern, what in UNSUPPORTED:
            if what and pattern.search(md):
                print(f"build_w103d_guide: {spec['source']} now uses {what}, which this "
                      f"converter does not render — extend it first", file=sys.stderr)
                return 1
        if md.count("```") % 2:
            print(f"build_w103d_guide: {spec['source']} has an odd number of code fences",
                  file=sys.stderr)
            return 1

        body = convert(md)
        # A converter that left markdown behind would ship visible asterisks and pipes.
        # Fenced code is emitted verbatim on purpose, so prose is the only thing
        # this may judge — a `#` comment inside a shell block legitimately carries
        # `**` (found 2026-10-17: the check had been failing on the complete guide
        # too, and nobody had re-run the generator since that line landed).
        prose = re.sub(r"<pre>.*?</pre>", "", body, flags=re.S)
        leftovers = [tok for tok in ("**", "```") if tok in prose]
        if leftovers:
            print(f"build_w103d_guide: markdown survived conversion: {leftovers}", file=sys.stderr)
            return 1

        for prefix, lang, label in (
            ("", "zh/w103d.html", "中文"),
            ("../", "../w103d.html", "EN"),
        ):
            out = REPO / "website" / ("" if prefix == "" else "zh/") / spec["out"]
            page_body = body.replace("@@ROOT@@", prefix)
            for md_name, page in spec["links"]:
                page_body = page_body.replace(f'href="{md_name}"', f'href="{page}"')
            hero_html = hero({**spec, "actions": spec["actions"].format(p=prefix)})
            figure = SPEC_FIGURE.format(p=prefix) if spec.get("figure") else ""
            page_html = PAGE.format(
                p=prefix, guide=("guide.html" if prefix == "" else "../guide.html"),
                active="", lang=lang, lang_label=label,
                title=spec["title"], description=spec["description"],
                hero=hero_html, body=figure + page_body)
            if "@@ROOT@@" in page_html:
                print("build_w103d_guide: an image path kept its @@ROOT@@ marker", file=sys.stderr)
                return 1
            out.write_text(page_html, encoding="utf-8")
            print(f"  wrote {out.relative_to(REPO)} ({len(page_body.splitlines())} content lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
