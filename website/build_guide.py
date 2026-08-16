#!/usr/bin/env python3
"""Build the FT-710 Operation Guide website page from docs/OPERATION_GUIDE.md.

Mirrors the site chrome of build_sdd.py (navbar + footer + amber theme).
Output: website/guide.html and website/zh/guide.html (same Chinese content).
"""
import subprocess, sys
from pathlib import Path

OUT = Path("/Users/cheenle/HAM/mrrc_ft710/website")

def convert(md_path: Path) -> str:
    result = subprocess.run(
        ["pandoc", str(md_path), "-f", "markdown", "-t", "html",
         "--no-highlight", "--wrap=none"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def build_page(body_html: str, lang: str) -> str:
    en = lang == "en"
    nav = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="MRRC FT-710 Web Control 操作指南 — 界面每个按钮与功能的编号图解与说明">
    <title>FT-710 操作指南 — MRRC FT-710</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="css/octen.css?v=4">
    <link rel="stylesheet" href="css/sunsdrmobile.css?v=1">
    <link rel="stylesheet" href="css/ft710.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .guide-layout {{ max-width: 900px; margin: 0 auto; padding: 0 1.5rem 4rem; }}
        .guide-content h1 {{ font-size: 2rem; font-weight: 700; margin: 2rem 0 0.5rem; letter-spacing: -0.02em; }}
        .guide-content h2 {{ font-size: 1.375rem; font-weight: 600; margin: 2rem 0 0.75rem; color: var(--accent); }}
        .guide-content h3 {{ font-size: 1.125rem; font-weight: 600; margin: 1.5rem 0 0.5rem; }}
        .guide-content h4 {{ font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }}
        .guide-content p, .guide-content li {{ color: var(--text-secondary); line-height: 1.7; margin-bottom: 0.75rem; }}
        .guide-content table {{ width: 100%; border-collapse: collapse; margin: 1.25rem 0; font-size: 0.875rem; }}
        .guide-content th, .guide-content td {{ padding: 0.625rem 0.875rem; text-align: left; border: 1px solid var(--border); vertical-align: top; }}
        .guide-content th {{ background: var(--bg-tertiary); color: var(--text-primary); font-weight: 600; }}
        .guide-content td {{ color: var(--text-secondary); }}
        .guide-content code {{ font-family: var(--font-mono); font-size: 0.85em; background: var(--bg-tertiary); padding: 1px 6px; border-radius: 3px; color: var(--accent); }}
        .guide-content pre {{ background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 0.5rem; padding: 1rem; overflow-x: auto; margin: 1rem 0; font-size: 0.8125rem; }}
        .guide-content blockquote {{ border-left: 3px solid var(--accent); padding: 0.5rem 1rem; margin: 1rem 0; color: var(--text-muted); font-size: 0.9375rem; background: var(--bg-tertiary); border-radius: 0 0.375rem 0.375rem 0; }}
        .guide-content img {{ max-width: 100%; height: auto; border-radius: 0.75rem; margin: 1rem 0; background: #111; }}
        .guide-content a {{ color: var(--accent); }}
        .guide-content hr {{ border: none; border-top: 1px solid var(--border); margin: 2rem 0; }}
        @media (max-width: 900px) {{ .guide-layout {{ padding: 0 1rem 3rem; }} }}
    </style>
</head>
<body data-site="mrrc_ft710">
<nav class="navbar">
    <div class="container navbar-content">
        <a href="index.html" class="logo">
            <span class="logo-icon"><i class="fas fa-microchip"></i></span>
            <span>MRRC FT<span style="color: var(--accent)">‑710</span></span>
        </a>
        <ul class="nav-links">
            <li><a href="index.html#features">Features</a></li>
            <li><a href="index.html#download">Download</a></li>
            <li><a href="index.html#start">Quick Start</a></li>
            <li><a href="guide.html" class="active">操作指南</a></li>
            <li><a href="sdd.html">SDD</a></li>
            <li><a href="https://github.com/cheenle/mrrc_ft710" target="_blank"><i class="fab fa-github"></i> GitHub</a></li>
        </ul>
        <div class="nav-actions">
            <a href="../zh/guide.html" class="lang-btn">中文</a>
            <button class="mobile-menu-toggle" onclick="toggleMobileMenu()"><i class="fas fa-bars"></i></button>
        </div>
    </div>
</nav>"""
    page = nav + f"""
<div class="guide-layout">
    <main class="guide-content">
{body_html}
    </main>
</div>

<footer class="footer" style="margin-top: 0;">
    <div class="container">
        <div class="footer-bottom">
            <p>&copy; 2026 MRRC FT-710 Project · <a href="https://github.com/cheenle/mrrc_ft710" style="color:var(--accent);">GitHub</a></p>
        </div>
    </div>
</footer>

<script>
function toggleMobileMenu() {{
    document.querySelector('.nav-links').classList.toggle('active');
}}
document.querySelectorAll('a[href^="#"]').forEach(a => {{
    a.addEventListener('click', function(e) {{
        e.preventDefault();
        const t = document.querySelector(this.getAttribute('href'));
        if (t) t.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
}});
const nav = document.querySelector('.navbar');
window.addEventListener('scroll', () => {{
    nav.style.background = window.scrollY > 50 ? 'rgba(0,0,0,0.95)' : 'rgba(0,0,0,0.8)';
}});
</script>
    <script src="js/global-nav.js?v=2" defer data-gn="1"></script>
</body>
</html>"""
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
    return page

def main():
    md_path = Path("/Users/cheenle/HAM/mrrc_ft710/docs/OPERATION_GUIDE.md")
    body = convert(md_path)
    for lang, out in (("en", "guide.html"), ("zh", "zh/guide.html")):
        dest = OUT / out
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(build_page(body, lang), encoding="utf-8")
        print(f"  wrote {dest}")

if __name__ == "__main__":
    main()
