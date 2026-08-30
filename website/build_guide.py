#!/usr/bin/env python3
"""Build the MRRC Modern Operation Guide website page from docs/OPERATION_GUIDE.md.

Mirrors the site chrome of build_sdd.py (navbar + footer + amber theme).
Output: website/guide.html and website/zh/guide.html (same Chinese content).

Layout: sticky sidebar TOC + content column (single column on mobile),
unified site font tokens (--font-sans/--font-mono), numbered amber badges
in the control reference tables, info/warning callouts, figure captions,
hero header and back-to-top.
"""
import re, subprocess, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent

def convert(md_path: Path):
    """pandoc → (toc_fragment, body_html). TOC extracted to sidebar."""
    result = subprocess.run(
        ["pandoc", str(md_path), "-f", "markdown", "-t", "html", "-s",
         "--toc", "--toc-depth=2", "--syntax-highlighting=none", "--wrap=none"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr}")
        sys.exit(1)
    html = result.stdout.strip()
    m = re.search(r'<nav id="TOC".*?</nav>', html, re.S)
    toc = m.group(0) if m else '<nav id="TOC"><ul></ul></nav>'
    # Body = everything between <body> and </body>, minus the TOC.
    bm = re.search(r'<body[^>]*>(.*)</body>', html, re.S)
    body = bm.group(1).strip() if bm else html
    body = re.sub(r'<nav id="TOC".*?</nav>', '', body, flags=re.S).strip()

    # Drop the markdown h1 (title) — the hero header carries it instead.
    body = re.sub(r'<h1[^>]*>.*?</h1>\s*', '', body, count=1, flags=re.S)

    # Tag control reference tables (first header cell is '#') with a class.
    # The class must go INSIDE the opening tag.
    def _tag_ref_table(m2):
        tag = m2.group(1)
        if 'class=' in tag:
            tag = re.sub(r'class="', 'class="ref-table ', tag, count=1)
        else:
            tag = tag[:-1] + ' class="ref-table">'
        return tag
    body = re.sub(
        r'(<table[^>]*>)(?=.{0,500}?<th[^>]*>#</th>)',
        _tag_ref_table, body, flags=re.S)

    # Wrap every table in a horizontal-scroll region (mobile-friendly).
    def _wrap_table(m2):
        return ('<div class="table-scroll" role="region" aria-label="数据表格" tabindex="0">'
                '<div class="table-hint"><i class="fas fa-arrows-left-right"></i> 左右滑动查看全部列</div>'
                + m2.group(0) + '</div>')
    body = re.sub(r'<table\b.*?</table>', _wrap_table, body, flags=re.S)

    # Lazy-load images for slow mobile networks.
    body = re.sub(r'<img (?![^>]*loading=)', '<img loading="lazy" decoding="async" ', body)

    # Number the figures (fig1/fig2) so the hero can link to them.
    figures = [m2.start() for m2 in re.finditer(r'<figure>', body)]
    for i, pos in enumerate(reversed(figures), 1):
        body = body[:pos] + f'<figure id="fig{len(figures)+1-i}">' + body[pos + len('<figure>'):]

    # Callouts: blockquotes containing ⚠️ become warning boxes, others info.
    def _callout(m2):
        inner = m2.group(1)
        cls = "callout-warn" if "⚠️" in inner else "callout-info"
        return f'<blockquote class="{cls}">{inner}</blockquote>'
    body = re.sub(r'<blockquote>(.*?)</blockquote>', _callout, body, flags=re.S)
    return toc, body

CSS = """
        html { -webkit-text-size-adjust: 100%; scroll-padding-top: calc(var(--gn-h, 44px) + var(--nav-h, 64px) + 12px); }
        body { -webkit-tap-highlight-color: transparent; }
        .guide-hero-inner { max-width: 1200px; margin: 0 auto; }
        .guide-layout { display: flex; max-width: 1200px; margin: 0 auto; padding: 0 2rem 4rem; gap: 2rem; align-items: flex-start; }
        /* ── Sidebar TOC ── */
        .guide-toc {
            width: 270px; flex-shrink: 0; position: sticky; top: calc(var(--gn-h, 36px) + var(--nav-h, 64px) + 16px);
            max-height: calc(100vh - var(--gn-h, 36px) - var(--nav-h, 64px) - 32px); overflow-y: auto;
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 0.75rem; padding: 1.25rem 1.25rem 1.5rem;
        }
        .guide-toc h4 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--accent); margin: 0 0 0.9rem; }
        .guide-toc ul { list-style: none; margin: 0; padding: 0; }
        .guide-toc ul ul { padding-left: 1rem; margin-top: 0.25rem; }
        .guide-toc a {
            display: block; padding: 0.3rem 0; font-size: 0.8125rem; line-height: 1.45;
            color: var(--text-secondary); text-decoration: none; border-left: 2px solid transparent; padding-left: 0.6rem;
            transition: color 0.15s, border-color 0.15s;
        }
        .guide-toc ul ul a { font-size: 0.75rem; color: var(--text-muted); }
        .guide-toc a:hover { color: var(--text-primary); border-left-color: var(--border-hover); }
        .guide-toc a.active { color: var(--accent); border-left-color: var(--accent); font-weight: 500; }
        /* Drawer chrome: hidden on desktop */
        .guide-toc-toggle { display: none; }
        .toc-fab { display: none; }
        .table-hint { display: none; }

        /* ── Hero ── */
        .guide-hero {
            max-width: 1200px; margin: 2rem auto 0; padding: 2.5rem 2rem 2.25rem;
            background: linear-gradient(135deg, rgba(240,160,48,0.10), rgba(240,160,48,0.02) 45%, transparent 70%);
            border-bottom: 1px solid var(--border);
        }
        .guide-hero .badge { display: inline-block; font-size: 0.72rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); border: 1px solid var(--border-hover); background: var(--accent-glow); padding: 0.25rem 0.7rem; border-radius: 999px; margin-bottom: 1rem; }
        .guide-hero h1 { font-size: 2.1rem; font-weight: 700; margin: 0 0 0.6rem; letter-spacing: -0.02em; }
        .guide-hero p { color: var(--text-secondary); max-width: 640px; line-height: 1.7; margin: 0 0 1.25rem; }
        .guide-hero .actions { display: flex; gap: 0.75rem; flex-wrap: wrap; }
        .guide-hero .btn { display: inline-flex; align-items: center; gap: 0.45rem; font-size: 0.87
5rem; font-weight: 600; padding: 0.55rem 1.05rem; border-radius: 0.5rem; text-decoration: none; border: 1px solid var(--border-hover); color: var(--accent); background: var(--bg-card); transition: all 0.15s; }
        .guide-hero .btn:hover { background: var(--accent-glow); border-color: var(--accent); }

        /* ── Content typography (site-wide tokens, matches SDD pages) ── */
        .guide-content { flex: 1; min-width: 0; padding-top: 2rem; }
        .guide-content h2 { font-size: 1.375rem; font-weight: 600; margin: 2.5rem 0 0.9rem; padding-bottom: 0.5rem; border-bottom: 1px solid var(--border); color: var(--accent); scroll-margin-top: calc(var(--gn-h, 44px) + var(--nav-h, 64px) + 12px); }
        .guide-content h3 { font-size: 1.05rem; font-weight: 600; margin: 1.75rem 0 0.6rem; color: var(--text-primary); scroll-margin-top: calc(var(--gn-h, 44px) + var(--nav-h, 64px) + 12px); }
        .guide-content h4 { font-size: 0.95rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }
        .guide-content p, .guide-content li { color: var(--text-secondary); line-height: 1.7; margin-bottom: 0.75rem; font-size: 0.9375rem; }
        .guide-content strong { color: var(--text-primary); }
        .guide-content code { font-family: var(--font-mono); font-size: 0.85em; background: var(--bg-tertiary); padding: 1px 6px; border-radius: 3px; color: var(--accent); }
        .guide-content pre { background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 0.5rem; padding: 1rem; overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 1rem 0; font-size: 0.8125rem; line-height: 1.6; }
        .guide-content pre code { background: none; padding: 0; color: var(--text-secondary); }

        /* ── Tables ── */
        .guide-content table { width: 100%; border-collapse: collapse; margin: 1.25rem 0; font-size: 0.875rem; }
        .guide-content th, .guide-content td { padding: 0.625rem 0.875rem; text-align: left; border: 1px solid var(--border); vertical-align: top; }
        .guide-content th { background: var(--bg-tertiary); color: var(--text-primary); font-weight: 600; white-space: nowrap; }
        .guide-content td { color: var(--text-secondary); }
        .guide-content tbody tr:nth-child(even) { background: rgba(255,255,255,0.015); }
        .guide-content tbody tr:hover { background: var(--accent-glow); }
        /* Horizontal scroll wrapper injected by the builder */
        .guide-content .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 1.25rem 0; border-radius: 0.5rem; }
        .guide-content .table-scroll > table { margin: 0; }
        .guide-content .table-scroll:focus { outline: 1px solid var(--border-hover); outline-offset: 2px; }
        .guide-content .table-scroll.scrollable .table-hint {
            display: flex; align-items: center; justify-content: flex-end; gap: 0.4rem;
            position: sticky; left: 0; width: max-content; margin-left: auto;
            font-size: 0.72rem; color: var(--text-muted); padding: 0 0.15rem 0.4rem;
        }
        .guide-content .table-hint i { color: var(--accent); }
        .guide-content .ref-table { min-width: 520px; }
        .guide-content .ref-table td:first-child {
            font-family: var(--font-mono); font-weight: 700; text-align: center; white-space: nowrap;
            color: #141414; background: var(--accent); border-radius: 999px;
            width: 2.1em; min-width: 2.1em; line-height: 1;
        }
        .guide-content .ref-table th:first-child { text-align: center; }
        .guide-content .ref-table th:nth-child(2) { white-space: nowrap; }

        /* ── Callouts ── */
        .guide-content blockquote {
            border-left: 3px solid var(--accent); padding: 0.65rem 1rem; margin: 1rem 0;
            color: var(--text-muted); font-size: 0.9rem; background: var(--bg-tertiary);
            border-radius: 0 0.5rem 0.5rem 0; line-height: 1.7;
        }
        .guide-content blockquote p { margin-bottom: 0.3rem; }
        .guide-content .callout-warn { border-left-color: #e5534b; background: rgba(229,83,75,0.07); color: #e8b3ae; }
        .guide-content .callout-warn strong { color: #f4b8b3; }

        /* ── Figures ── */
        .guide-content figure { margin: 1.75rem auto; text-align: center; }
        .guide-content figure img { max-width: 100%; height: auto; border-radius: 0.75rem; border: 1px solid var(--border); background: #0d0d0d; }
        .guide-content figcaption { margin-top: 0.5rem; font-size: 0.8rem; color: var(--text-muted); }

        /* ── Back to top ── */
        .back-top {
            position: fixed; right: 1.5rem; bottom: 1.5rem; z-index: 50;
            width: 2.6rem; height: 2.6rem; border-radius: 999px; border: 1px solid var(--border-hover);
            background: var(--bg-card); color: var(--accent); font-size: 1rem; cursor: pointer;
            opacity: 0; pointer-events: none; transition: opacity 0.2s;
        }
        .back-top.show { opacity: 1; pointer-events: auto; }
        .back-top:hover { background: var(--accent-glow); }

        /* ── Figure lightbox (mobile tap-to-zoom) ── */
        .guide-lightbox {
            display: none; position: fixed; inset: 0; z-index: 10000;
            background: rgba(0,0,0,0.95); align-items: center; justify-content: center;
            padding: 0.5rem; overscroll-behavior: contain;
        }
        .guide-lightbox.open { display: flex; }
        .guide-lightbox img { max-width: 100%; max-height: calc(100% - 1rem); object-fit: contain; touch-action: pinch-zoom pan-x pan-y; }
        .guide-lightbox-close {
            position: absolute; top: calc(0.75rem + env(safe-area-inset-top, 0px)); right: 0.9rem;
            width: 44px; height: 44px; border-radius: 999px;
            background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
            color: #fff; font-size: 1rem; cursor: pointer;
            display: flex; align-items: center; justify-content: center;
        }

        /* ══════════ Responsive ══════════ */
        /* ── Tablet & phone: TOC becomes a bottom-sheet drawer ── */
        @media (max-width: 960px) {
            .guide-layout { display: block; padding: 0 1.25rem 3rem; max-width: 100%; }
            .guide-content { padding-top: 1rem; }
            .guide-hero { padding: 2rem 1.5rem 1.75rem; margin-top: 1rem; }
            .guide-hero h1 { font-size: 1.6rem; }

            .guide-toc {
                position: fixed; left: 0; right: 0; bottom: 0; top: auto; width: 100%;
                max-height: 74vh; max-height: 74dvh; overflow: hidden;
                display: flex; flex-direction: column;
                border-radius: 1rem 1rem 0 0; border-bottom: none;
                padding: 0 1.25rem calc(1rem + env(safe-area-inset-bottom, 0px));
                transform: translateY(105%); transition: transform 0.28s ease;
                z-index: 300; box-shadow: 0 -12px 40px rgba(0,0,0,0.55);
            }
            .guide-toc.open { transform: translateY(0); }
            .guide-toc-head {
                display: flex; align-items: center; justify-content: space-between;
                position: sticky; top: 0; background: var(--bg-card);
                padding: 1rem 0 0.6rem; border-bottom: 1px solid var(--border);
            }
            .guide-toc-head h4 { margin: 0; }
            .guide-toc nav, .guide-toc #TOC { overflow-y: auto; -webkit-overflow-scrolling: touch; padding-bottom: 0.5rem; }
            .guide-toc a { padding: 0.5rem 0; font-size: 0.9rem; }
            .guide-toc ul ul a { font-size: 0.82rem; }
            .guide-toc-close {
                display: flex; align-items: center; justify-content: center;
                width: 40px; height: 40px; border-radius: 999px; flex-shrink: 0;
                border: 1px solid var(--border); background: var(--bg-tertiary);
                color: var(--text-primary); font-size: 1.05rem; cursor: pointer;
            }
            .guide-toc-backdrop {
                position: fixed; inset: 0; z-index: 290; background: rgba(0,0,0,0.6);
                opacity: 0; pointer-events: none; transition: opacity 0.25s;
                backdrop-filter: blur(2px); -webkit-backdrop-filter: blur(2px);
            }
            .guide-toc-backdrop.show { opacity: 1; pointer-events: auto; }
            .toc-fab {
                display: inline-flex; align-items: center; gap: 0.45rem;
                position: fixed; left: 1rem; bottom: calc(1rem + env(safe-area-inset-bottom, 0px));
                z-index: 250; height: 44px; padding: 0 1rem; border-radius: 999px;
                border: 1px solid var(--border-hover); background: var(--bg-card);
                color: var(--accent); font-size: 0.85rem; font-weight: 600; cursor: pointer;
                box-shadow: 0 6px 20px rgba(0,0,0,0.45);
            }
            body.toc-lock { overflow: hidden; }
        }
        @media (min-width: 961px) { .guide-toc-close, .guide-toc-backdrop { display: none; } }

        /* ── Phone ── */
        @media (max-width: 768px) {
            body { padding-left: env(safe-area-inset-left, 0px); padding-right: env(safe-area-inset-right, 0px); }
            .guide-layout { padding: 0 1.1rem 3.5rem; }

            /* Hero */
            .guide-hero { padding: 1.4rem 1.15rem 1.4rem; margin: 0.75rem 1.1rem 0; border: 1px solid var(--border); border-radius: 0.85rem; background: linear-gradient(135deg, rgba(240,160,48,0.12), rgba(240,160,48,0.02) 55%, transparent 75%); }
            .guide-hero .badge { font-size: 0.62rem; padding: 0.2rem 0.6rem; margin-bottom: 0.7rem; letter-spacing: 0.08em; }
            .guide-hero h1 { font-size: 1.32rem; line-height: 1.35; margin-bottom: 0.5rem; }
            .guide-hero p { font-size: 0.875rem; line-height: 1.65; margin-bottom: 1rem; }
            .guide-hero .actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
            .guide-hero .btn { justify-content: center; min-height: 44px; padding: 0.5rem 0.5rem; font-size: 0.78rem; }
            .guide-hero .btn .btn-extra { display: none; }

            /* Typography */
            .guide-content p, .guide-content li { font-size: 1rem; line-height: 1.75; overflow-wrap: anywhere; }
            .guide-content h2 { font-size: 1.175rem; margin: 2.25rem 0 0.75rem; }
            .guide-content h3 { font-size: 1.02rem; margin: 1.5rem 0 0.5rem; }
            .guide-content code { overflow-wrap: anywhere; word-break: break-word; }
            .guide-content pre { padding: 0.8rem 0.9rem; font-size: 0.76rem; border-radius: 0.5rem; }
            .guide-content blockquote { padding: 0.75rem 0.9rem; font-size: 0.875rem; }

            /* Tables */
            .guide-content .table-scroll { margin: 1.15rem 0; }
            .guide-content th, .guide-content td { padding: 0.55rem 0.65rem; font-size: 0.8125rem; }
            .guide-content .ref-table { min-width: 480px; }
            .guide-content .ref-table td:first-child { width: 1.9em; min-width: 1.9em; font-size: 0.72rem; padding-left: 0.25rem; padding-right: 0.25rem; }

            /* Figures: edge-to-edge + tap to zoom */
            .guide-content figure { margin: 1.5rem -1.1rem; }
            .guide-content figure img { width: 100%; max-width: none; border-radius: 0; border-left: none; border-right: none; cursor: zoom-in; }
            .guide-content figcaption { padding: 0.5rem 1.1rem 0; font-size: 0.76rem; }

            /* Back to top sits above the safe area, opposite the TOC fab */
            .back-top { right: calc(1rem + env(safe-area-inset-right, 0px)); bottom: calc(1rem + env(safe-area-inset-bottom, 0px)); width: 42px; height: 42px; }
        }

        /* ── Small phones ── */
        @media (max-width: 420px) {
            .guide-hero { margin: 0.6rem 0.9rem 0; padding: 1.25rem 1rem 1.25rem; }
            .guide-layout { padding: 0 0.95rem 3.5rem; }
            .guide-hero h1 { font-size: 1.2rem; }
            .guide-content figure { margin: 1.4rem -0.95rem; }
            .guide-content figcaption { padding: 0.5rem 0.95rem 0; }
        }
"""

GUIDE_JS = """function toggleMobileMenu() {
    var nl = document.querySelector('.nav-links');
    if (nl) nl.classList.toggle('active');
}
(function () {
    var mqMobile = window.matchMedia('(max-width: 960px)');
    var bt = document.getElementById('back-top');
    var toc = document.getElementById('guide-toc');
    var tocClose = document.getElementById('toc-close');
    var fab = document.getElementById('toc-fab');
    var backdrop = document.getElementById('toc-backdrop');
    var links = Array.prototype.slice.call(document.querySelectorAll('.guide-toc a'));
    var headings = links.map(function (a) {
        var id = (a.getAttribute('href') || '').replace('#', '');
        return id ? document.getElementById(id) : null;
    });

    /* ── TOC bottom-sheet drawer (tablet/phone) ── */
    function setToc(open) {
        if (!toc) return;
        toc.classList.toggle('open', open);
        if (backdrop) backdrop.classList.toggle('show', open);
        document.body.classList.toggle('toc-lock', open && mqMobile.matches);
        if (tocClose) tocClose.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    if (fab) fab.addEventListener('click', function () { setToc(true); });
    if (tocClose) tocClose.addEventListener('click', function () { setToc(false); });
    if (backdrop) backdrop.addEventListener('click', function () { setToc(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setToc(false); });
    links.forEach(function (a) {
        a.addEventListener('click', function () { if (mqMobile.matches) setToc(false); });
    });
    window.addEventListener('resize', function () { if (!mqMobile.matches) setToc(false); });

    /* ── Horizontal-scroll hint on wide tables ── */
    Array.prototype.slice.call(document.querySelectorAll('.table-scroll')).forEach(function (wrap) {
        var tb = wrap.querySelector('table');
        if (!tb) return;
        function check() { wrap.classList.toggle('scrollable', tb.scrollWidth > wrap.clientWidth + 8); }
        check();
        window.addEventListener('resize', check);
    });

    /* ── Figure tap-to-zoom lightbox ── */
    var lb = null;
    function closeLb() {
        if (!lb) return;
        lb.classList.remove('open');
        document.documentElement.style.overflow = '';
    }
    function openLb(img) {
        if (!lb) {
            lb = document.createElement('div');
            lb.className = 'guide-lightbox';
            lb.innerHTML = '<img alt="放大图片">' +
                '<button class="guide-lightbox-close" aria-label="关闭"><i class="fas fa-xmark"></i></button>';
            document.body.appendChild(lb);
            lb.addEventListener('click', closeLb);
            document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeLb(); });
        }
        lb.querySelector('img').src = img.currentSrc || img.src;
        lb.classList.add('open');
        document.documentElement.style.overflow = 'hidden';
    }
    Array.prototype.slice.call(document.querySelectorAll('.guide-content figure img')).forEach(function (img) {
        img.addEventListener('click', function () {
            if (mqMobile.matches || window.matchMedia('(pointer: coarse)').matches) openLb(img);
        });
    });

    /* ── Scrollspy + back-to-top ── */
    function onScroll() {
        if (window.scrollY > 500) bt.classList.add('show'); else bt.classList.remove('show');
        var idx = 0;
        for (var i = 0; i < headings.length; i++) {
            if (headings[i] && headings[i].getBoundingClientRect().top <= 130) idx = i;
        }
        links.forEach(function (a, i) { a.classList.toggle('active', i === idx); });
        if (toc && toc.classList.contains('open') && links[idx]) {
            var act = links[idx];
            var body = toc.querySelector('#TOC') || toc;
            var ar = act.getBoundingClientRect(), br = body.getBoundingClientRect();
            if (ar.top < br.top || ar.bottom > br.bottom) act.scrollIntoView({
 block: 'nearest' });
        }
    }
    if (bt) bt.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: 'smooth' }); });
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();

    var navEl = document.querySelector('.navbar');
    if (navEl) window.addEventListener('scroll', function () {
        navEl.style.background = window.scrollY > 50 ? 'rgba(0,0,0,0.95)' : 'rgba(0,0,0,0.8)';
    }, { passive: true });
})();
"""


def build_page(toc: str, body_html: str, lang: str) -> str:
    en = lang == "en"
    nav = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="description" content="MRRC Modern Web Control 操作指南 — 界面每个按钮与功能的编号图解与说明">
    <title>MRRC Modern 操作指南</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="css/octen.css?v=4">
    <link rel="stylesheet" href="css/sunsdrmobile.css?v=1">
    <link rel="stylesheet" href="css/ft710.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
{CSS}
    </style>
</head>
<body data-site="mrrc_modern">
<nav class="navbar">
    <div class="container navbar-content">
        <a href="index.html" class="logo">
            <span class="logo-icon"><i class="fas fa-microchip"></i></span>
            <span>MRRC <span style="color: var(--accent)">Modern</span></span>
        </a>
        <ul class="nav-links">
            <li><a href="index.html#features">Features</a></li>
            <li><a href="index.html#download">Download</a></li>
            <li><a href="index.html#start">Quick Start</a></li>
            <li><a href="guide.html" class="active">操作指南</a></li>
            <li><a href="sdd.html">SDD</a></li>
            <li><a href="https://github.com/cheenle/mrrc_modern" target="_blank"><i class="fab fa-github"></i> GitHub</a></li>
        </ul>
        <div class="nav-actions">
            <a href="../zh/guide.html" class="lang-btn">中文</a>
            <button class="mobile-menu-toggle" onclick="toggleMobileMenu()"><i class="fas fa-bars"></i></button>
        </div>
    </div>
</nav>

<header class="guide-hero">
    <div class="guide-hero-inner">
        <span class="badge"><i class="fas fa-book"></i> 操作指南 · Operation Guide</span>
        <h1>MRRC Modern Web 遥控 — 操作指南</h1>
        <p>界面每一个按钮、滑杆、下拉框的编号图解与精确说明。琥珀色序号与正文速查表一一对应，点下方任一图直接跳转。</p>
        <div class="actions">
            <a class="btn" href="#fig1"><i class="fas fa-mobile-screen"></i> 图 1 · 主界面</a>
            <a class="btn" href="#fig2"><i class="fas fa-bars"></i> 图 2 · 抽屉菜单</a>
            <a class="btn" href="https://github.com/cheenle/mrrc_modern/blob/main/docs/OPERATION_GUIDE.md" target="_blank"><i class="fab fa-github"></i> 源文档<span class="btn-extra"> (md)</span></a>
        </div>
    </div>
</header>

<button class="toc-fab" id="toc-fab" aria-label="打开目录"><i class="fas fa-list"></i> 目录</button>
<div class="guide-toc-backdrop" id="toc-backdrop"></div>

<div class="guide-layout">
    <aside class="guide-toc" id="guide-toc" aria-label="页面目录">
        <div class="guide-toc-head">
            <h4>目录 · Contents</h4>
            <button class="guide-toc-close" id="toc-close" aria-label="关闭目录"><i class="fas fa-xmark"></i></button>
        </div>
{toc}
    </aside>
    <main class="guide-content">
{body_html}
    </main>
</div>

<button class="back-top" id="back-top" title="回到顶部"><i class="fas fa-arrow-up"></i></button>

<footer class="footer" style="margin-top: 0;">
    <div class="container">
        <div class="footer-bottom">
            <p>&copy; 2026 MRRC Modern Project · <a href="https://github.com/cheenle/mrrc_modern" style="color:var(--accent);">GitHub</a></p>
        </div>
    </div>
</footer>

<script>
{GUIDE_JS}
</script>
    <script src="js/global-nav.js?v=3" defer data-gn="1"></script>
</body>
</html>"""
    page = nav
    if not en:
        page = page.replace('href="index.html"', 'href="../index.html"')
        page = page.replace('href="sdd.html"', 'href="../sdd.html"')
        page = page.replace('<a href="../zh/guide.html"', '<a href="../guide.html"')
        page = page.replace('href="index.html#features"', 'href="../index.html#features"')
        page = page.replace('href="index.html#download"', 'href="../index.html#download"')
        page = page.replace('href="index.html#start"', 'href="../index.html#start"')
        page = page.replace('href="css/', 'href="../css/')
        page = page.replace('src="js/global-nav.js', 'src="../js/global-nav.js')
        page = page.replace('src="images/', 'src="../images/')
        page = page.replace('href="guide.html"', 'href="../guide.html"')
        # sidebar links in the zh copy must keep same-page anchors
        page = page.replace('class="guide-toc"', 'class="guide-toc"')
    return page

def main():
    md_path = Path(__file__).resolve().parent.parent / "docs" / "OPERATION_GUIDE.md"
    toc, body = convert(md_path)
    for lang, out in (("en", "guide.html"), ("zh", "zh/guide.html")):
        dest = OUT / out
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(build_page(toc, body, lang), encoding="utf-8")
        print(f"  wrote {dest}")

if __name__ == "__main__":
    main()
