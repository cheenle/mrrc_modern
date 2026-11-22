#!/usr/bin/env python3
"""Publish the FoloToy Passport terminal's SDD as /mrrc_modern/sdd-passport/.

Single source: ~/HAM/mrrc-passport/SDD (the firmware repository). This script
never copies markdown into the site — it renders on demand, so a chapter edit is
one rebuild and there is no second truth.

    python3 build_passport_sdd.py                 # uses ~/HAM/mrrc-passport
    PASSPORT_REPO=/tmp/passport python3 build_passport_sdd.py
    python3 build_passport_sdd.py --dry-run

Needs pandoc (brew install pandoc). Writes:
    sdd-passport/*.html            chapters + overview
    passport-field-card.html       the one-page bench card
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "sdd-passport"
CARD_OUT = HERE / "passport-field-card.html"
DEFAULT_REPO = Path.home() / "HAM" / "mrrc-passport"

CSS = ["css/scope.css?v=1", "css/mrrc_modern.scope.css?v=1"]

CHAPTERS = [
    ("README.md", "index.html", "Overview"),
    ("00-verification-discipline.md", "00-verification-discipline.html", "0. Verification Discipline"),
    ("01-executive-summary.md", "01-executive-summary.html", "1. Executive Summary"),
    ("03-project-definition.md", "03-project-definition.html", "3. Project Definition"),
    ("04-system-context.md", "04-system-context.html", "4. System Context"),
    ("05-non-functional-requirements.md", "05-non-functional-requirements.html", "5. Requirements"),
    ("06-use-case-model.md", "06-use-case-model.html", "6. Use Cases"),
    ("08-architecture-decisions.md", "08-architecture-decisions.html", "8. Decisions"),
    ("09-architecture-overview.md", "09-architecture-overview.html", "9. Architecture"),
    ("11-component-model.md", "11-component-model.html", "11. Components"),
    ("12-operational-model.md", "12-operational-model.html", "12. Operations"),
    ("13-feasibility-assessment.md", "13-feasibility-assessment.html", "13. Feasibility"),
    ("14-version-history.md", "14-version-history.html", "14. Version History"),
    ("15-transmit-safety-architecture.md", "15-transmit-safety-architecture.html", "15. Transmit Safety"),
    ("../docs/field-card.md", "field-card.html", "Field card"),
]

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>%(title)s — MRRC Passport terminal SDD</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="../%(css0)s" />
<link rel="stylesheet" href="../%(css1)s" />
<style>
  .doc-wrap{max-width:1180px;margin:0 auto;padding:2rem 1.25rem 4rem}
  .doc-head{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center;justify-content:space-between;
            margin-bottom:1.5rem;border-bottom:1px solid rgba(255,255,255,.12);padding-bottom:.8rem}
  .doc-head a{color:#ffa500;text-decoration:none;font-weight:600}
  .doc-layout{display:grid;grid-template-columns:250px 1fr;gap:2rem}
  @media(max-width:900px){.doc-layout{grid-template-columns:1fr}.doc-nav{position:static;max-height:none}}
  .doc-nav{position:sticky;top:1rem;align-self:start;font-size:.82rem;
           background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);
           border-radius:12px;padding:.7rem 0;max-height:calc(100vh - 2rem);overflow:auto}
  .doc-nav h4{margin:.25rem 1rem .5rem;font-size:.68rem;letter-spacing:.09em;
              text-transform:uppercase;color:#ffa500}
  .doc-nav ul{list-style:none;margin:0;padding:0}
  .doc-nav a{display:block;padding:.28rem 1rem;color:#c9d1d9;text-decoration:none}
  .doc-nav a:hover{background:rgba(255,165,0,.12);color:#fff}
  .doc-nav a.active{color:#ffa500;font-weight:600}
  .doc-body{min-width:0;line-height:1.62;color:#d7dde5}
  .doc-body h1{font-size:1.55rem;margin:.2rem 0 1rem;color:#fff}
  .doc-body h2{font-size:1.16rem;margin:1.9rem 0 .6rem;color:#ffa500}
  .doc-body h3{font-size:1rem;margin:1.3rem 0 .4rem}
  .doc-body table{width:100%%;border-collapse:collapse;margin:.8rem 0 1.2rem;
                  font-size:.82rem;display:block;overflow-x:auto}
  .doc-body th,.doc-body td{border:1px solid rgba(255,255,255,.12);padding:.4rem .55rem;
                  text-align:left;vertical-align:top}
  .doc-body th{background:rgba(255,165,0,.10)}
  .doc-body code{font-family:'JetBrains Mono',monospace;font-size:.85em;
                  background:rgba(255,255,255,.07);padding:.1em .3em;border-radius:4px}
  .doc-body pre{background:rgba(255,255,255,.05);padding:.85rem;border-radius:8px;overflow-x:auto}
  .doc-body pre code{background:none;padding:0}
  .doc-body blockquote{margin:1rem 0;padding:.35rem 1rem;border-left:3px solid #ffa500;color:#b8c0cc}
</style>
</head>
<body>
<div class="doc-wrap">
  <div class="doc-head">
    <div>%(title)s · <span style="color:#8b949e">MRRC Passport terminal · design record (SDD)</span></div>
    <nav><a href="../passport-terminal.html">← 终端介绍</a> &nbsp;·&nbsp;
         <a href="../passport-field-card.html">现场速查卡</a></nav>
  </div>
  <div class="doc-layout">
    <aside class="doc-nav"><h4>SDD · 13 chapters</h4><ul>
%(nav)s
    </ul></aside>
    <article class="doc-body">
%(body)s
    </article>
  </div>
</div>
</body>
</html>
"""

CARD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Field card — MRRC Passport terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="%(css0)s" />
<link rel="stylesheet" href="%(css1)s" />
<style>
  body{background:#0b0d10}
  .card{max-width:900px;margin:0 auto;padding:2rem 1.25rem 4rem;color:#d7dde5;line-height:1.6}
  .card a{color:#ffa500;text-decoration:none;font-weight:600}
  .card h1{font-size:1.5rem;color:#fff;margin:.2rem 0 1rem}
  .card h2{font-size:1.12rem;color:#ffa500;margin:1.8rem 0 .5rem}
  .card table{width:100%%;border-collapse:collapse;margin:.7rem 0 1.1rem;font-size:.82rem;
              display:block;overflow-x:auto}
  .card th,.card td{border:1px solid rgba(255,255,255,.12);padding:.4rem .55rem;text-align:left;vertical-align:top}
  .card th{background:rgba(255,165,0,.10)}
  .card code{font-family:'JetBrains Mono',monospace;font-size:.85em;background:rgba(255,255,255,.07);
             padding:.1em .3em;border-radius:4px}
  .card pre{background:rgba(255,255,255,.05);padding:.8rem;border-radius:8px;overflow-x:auto}
  @media print{body{background:#fff}.card{color:#111}.card h2{color:#a35b00}
    .card th{background:#f4e6d0}.card a{color:#a35b00}}
</style>
</head>
<body>
<div class="card">
  <div style="margin-bottom:1rem"><a href="passport-terminal.html">← 终端介绍</a> ·
       <a href="sdd-passport/index.html">设计记录 SDD</a></div>
%(body)s
</div>
</body>
</html>
"""


def find_pandoc():
    for cand in ("pandoc", "/opt/homebrew/bin/pandoc", "/usr/local/bin/pandoc"):
        if os.sep in cand:
            if os.access(cand, os.X_OK):
                return cand
            continue
        hit = shutil.which(cand)
        if hit:
            return hit
    return None


def convert(path, pandoc):
    out = subprocess.run([pandoc, str(path), "-f", "markdown", "-t", "html",
                          "--no-highlight", "--wrap=none"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("pandoc failed on %s: %s" % (path.name, out.stderr.strip()))
    return out.stdout.strip()


def links(html, prefix=""):
    """`[x](03-x.md)` must reach the published `.html` twin."""
    def fix(m):
        t = m.group(1)
        if not t.endswith(".md") or t.startswith(("http", "/", "#")) or ".." in t.split("/"):
            return m.group(0)
        return 'href="%s%s.html"' % (prefix, t[:-3])
    return re.sub(r'href="([^"]*)"', fix, html)


def tidy(html):
    html = html.replace("<table>", '<table class="doc-table">')
    return re.sub(r"<pre", '<pre class="doc-pre"', html)


def nav_html(current):
    return "\n".join(
        '      <li><a href="%s"%s>%s</a></li>'
        % (href, ' class="active"' if href == current else "", label)
        for _md, href, label in CHAPTERS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("PASSPORT_REPO", str(DEFAULT_REPO)))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    sdd = repo / "SDD"
    if not (sdd / "README.md").is_file():
        sys.exit("no SDD under %s (set PASSPORT_REPO)" % repo)
    pandoc = find_pandoc() or sys.exit("pandoc not found (brew install pandoc)")

    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for md_name, html_name, label in CHAPTERS:
        src = (repo / "docs/field-card.md") if md_name.startswith("../") else (sdd / md_name)
        if not src.is_file():
            print("  skip (missing): %s" % src)
            continue
        body = tidy(links(convert(src, pandoc)))
        if html_name == "field-card.html":
            out = CARD_PAGE % {"body": body, "css0": CSS[0], "css1": CSS[1]}
            dest = CARD_OUT
        else:
            title = "Overview" if md_name == "README.md" else label
            out = PAGE % {"title": title, "body": body, "nav": nav_html(html_name),
                          "css0": CSS[0], "css1": CSS[1]}
            dest = OUT_DIR / html_name
        if args.dry_run:
            print("  would write %s (%d B)" % (dest.name, len(out)))
        else:
            dest.write_text(out, encoding="utf-8")
            print("  %s ← %s" % (dest.name, src.name))
        count += 1
    print("done: %d pages" % count)


if __name__ == "__main__":
    main()
