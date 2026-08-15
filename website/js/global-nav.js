/* ════════════════════════════════════════════════════════════════════
   VLSC Global Nav + UX Enhancements
   Shared across all project sites (portal, MRRC, SunMRRC, SunsdrMobile).
   Injects: global top nav, scroll-progress bar, back-to-top button,
   scroll-reveal (IntersectionObserver), active-link highlighting,
   navbar scrolled state, mobile-menu overlay.
   Self-contained — no dependencies. Site identified via <body data-site>
   or location.pathname. Language via <html lang> or /zh/ in path.
   ════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── Site detection ──
  var SITE = document.body.getAttribute('data-site');
  if (!SITE) {
    var p = location.pathname.replace(/\/+$/, '');
    if (p === '' || p === '/index.html' || /(^|\/)portal\//.test(p)) SITE = 'portal';
    else if (/\/fde\.html/.test(p)) SITE = 'fde';
    else if (/\/mrrc\//.test(p)) SITE = 'mrrc';
    else if (/\/sunmrrc\//.test(p)) SITE = 'sunmrrc';
    else if (/\/sunsdrmobile\//.test(p)) SITE = 'sunsdrmobile';
    else if (/\/mrrc_ft710\//.test(p)) SITE = 'mrrc_ft710';
    else if (/\/sdd\//.test(p)) SITE = 'sunmrrc';
    else SITE = 'portal';
  }

  // ── Language detection ──
  var isCN = (document.documentElement.getAttribute('lang') || '').indexOf('zh') === 0
    || /\/zh\//.test(location.pathname);

  // Absolute paths (sites live under www.vlsc.net)
  var PATHS = {
    portal: '/',
    fde: '/fde.html',
    mrrc: '/mrrc/',
    mrrc_ft710: '/mrrc_ft710/',
    sunmrrc: '/sunmrrc/',
    sunsdrmobile: '/sunsdrmobile/',
    blog: '/blog/'
  };

  var L = isCN ? {
    brand: 'VLSC 项目',
    projects: '项目',
    back: '返回顶部'
  } : {
    brand: 'VLSC Projects',
    projects: 'Projects',
    back: 'Back to top'
  };

  // ── 0. Fallback CSS (only when octen.css is not loaded) ──
  // Ensures the global nav + utilities render on pages that use a different
  // design system (e.g. MRRC docs pages with style.css, EFHW with Tailwind).
  var hasOcten = Array.prototype.some.call(
    document.querySelectorAll('link[rel="stylesheet"]'),
    function (l) { return /octen\.css(\?|$)/.test(l.getAttribute('href') || ''); }
  );
  if (!hasOcten) {
    var fb = document.createElement('style');
    // Self-contained: hardcoded colors so it never clashes with a page's own
    // design tokens. Only --gn-h/--nav-h are exposed as variables.
    fb.textContent =
      ':root{--gn-h:36px;--nav-h:64px;}' +
      '.vlsc-gn{position:fixed;top:0;left:0;right:0;z-index:120;height:var(--gn-h);' +
      'background:linear-gradient(180deg,rgba(0,0,0,0.96),rgba(10,10,10,0.92));' +
      'border-bottom:1px solid #262626;backdrop-filter:blur(16px);' +
      '-webkit-backdrop-filter:blur(16px);font-size:0.8125rem;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}' +
      '.vlsc-gn-inner{max-width:1200px;margin:0 auto;padding:0 2rem;height:var(--gn-h);' +
      'display:flex;align-items:center;justify-content:space-between;gap:1rem;}' +
      '.vlsc-gn-brand{display:inline-flex;align-items:center;gap:0.4rem;font-weight:700;color:#fff;text-decoration:none;white-space:nowrap;}' +
      '.vlsc-gn-brand i{color:#f39c12;font-size:0.85rem;}' +
      '.vlsc-gn-brand .dot{color:#f39c12;margin:0 0.35rem;opacity:0.5;}' +
      '.vlsc-gn-links{display:flex;align-items:center;gap:1.25rem;list-style:none;margin:0;padding:0;}' +
      '.vlsc-gn-links a{color:#a3a3a3;font-weight:500;text-decoration:none;position:relative;padding:0.25rem 0;transition:color 0.2s;white-space:nowrap;}' +
      '.vlsc-gn-links a::after{content:"";position:absolute;left:0;right:0;bottom:-3px;height:2px;background:#f39c12;transform:scaleX(0);transform-origin:center;transition:transform 0.25s;border-radius:2px;}' +
      '.vlsc-gn-links a:hover{color:#fff;}' +
      '.vlsc-gn-links a:hover::after,.vlsc-gn-links a.is-active::after{transform:scaleX(1);}' +
      '.vlsc-gn-links a.is-active{color:#f39c12;}' +
      '.vlsc-gn-gh{color:#a3a3a3;text-decoration:none;transition:color 0.2s;display:inline-flex;align-items:center;}' +
      '.vlsc-gn-gh:hover{color:#fff;}' +
      'body.vlsc-gn-on{padding-top:var(--gn-h)!important;}' +
      'body.vlsc-gn-on .navbar,body.vlsc-gn-on .header{top:var(--gn-h)!important;}' +
      '.vlsc-scroll-progress{position:fixed;top:0;left:0;z-index:200;height:2px;width:0%;' +
      'background:linear-gradient(90deg,#f39c12,#e67e22);pointer-events:none;}' +
      '.vlsc-to-top{position:fixed;right:1.5rem;bottom:1.5rem;z-index:110;width:44px;height:44px;' +
      'border-radius:50%;background:#1a1a1a;border:1px solid #262626;color:#f39c12;' +
      'display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:1rem;' +
      'opacity:0;visibility:hidden;transform:translateY(12px);transition:all 0.3s;' +
      'backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);}' +
      '.vlsc-to-top.show{opacity:1;visibility:visible;transform:translateY(0);}' +
      '.vlsc-to-top:hover{border-color:#f39c12;background:rgba(243,156,18,0.15);}' +
      '@media(max-width:768px){.vlsc-gn-links{gap:0.85rem;}.vlsc-gn-inner{padding:0 1.25rem;}.vlsc-to-top{right:1rem;bottom:1rem;}}';
    (document.head || document.documentElement).appendChild(fb);
  }

  // ── 1. Global top nav ──
  var gn = document.createElement('div');
  gn.className = 'vlsc-gn';
  gn.innerHTML =
    '<div class="vlsc-gn-inner container">' +
      '<a class="vlsc-gn-brand" href="' + PATHS.portal + '" title="' + L.brand + '">' +
        '<i class="fas fa-satellite-dish"></i>' +
        '<span>VLSC<span class="dot">·</span>Projects</span>' +
      '</a>' +
      '<nav class="vlsc-gn-links">' +
        siteLink('fde', 'FDE') +
        siteLink('mrrc', 'MRRC') +
        siteLink('mrrc_ft710', 'FT-710') +
        siteLink('sunmrrc', 'SunMRRC') +
        siteLink('sunsdrmobile', 'SunsdrMobile') +
        siteLink('blog', 'Blog') +
      '</nav>' +
      '<a class="vlsc-gn-gh" href="https://github.com/cheenle" target="_blank" rel="noopener" title="GitHub">' +
        '<i class="fab fa-github"></i>' +
      '</a>' +
    '</div>';

  function siteLink(key, label) {
    var active = (key === SITE) ? ' is-active' : '';
    return '<a href="' + PATHS[key] + '" data-site="' + key + '" class="' + active.trim() + '">' + label + '</a>';
  }

  // Insert as first child of body
  document.body.insertBefore(gn, document.body.firstChild);
  document.body.classList.add('vlsc-gn-on');

  // ── 2. Scroll progress bar ──
  var sp = document.createElement('div');
  sp.className = 'vlsc-scroll-progress';
  document.body.appendChild(sp);

  // ── 3. Back-to-top ──
  var bt = document.createElement('button');
  bt.type = 'button';
  bt.className = 'vlsc-to-top';
  bt.setAttribute('aria-label', L.back);
  bt.title = L.back;
  bt.innerHTML = '<i class="fas fa-arrow-up"></i>';
  bt.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  document.body.appendChild(bt);

  // ── Scroll handler (throttled via rAF) ──
  var navbar = document.querySelector('.navbar, .header');
  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var st = window.scrollY || document.documentElement.scrollTop;
      var h = document.documentElement.scrollHeight - window.innerHeight;
      sp.style.width = (h > 0 ? (st / h * 100) : 0) + '%';
      if (navbar) navbar.classList.toggle('scrolled', st > 40);
      bt.classList.toggle('show', st > 600);
      ticking = false;
    });
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ── 4. Scroll reveal ──
  var revealSel = '.section .card, .section .demo-link, .section .feature-card, ' +
    '.section .eco-track, .section .arch-card, .section .step, ' +
    '.section .perf-card, .section .screen-card, .section .doc-card, ' +
    '.section .role-card, .section .card-lite, .section .tech-tag';
  var revealEls = document.querySelectorAll(revealSel);
  if ('IntersectionObserver' in window && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    revealEls.forEach(function (el) { el.classList.add('vlsc-reveal'); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    revealEls.forEach(function (el) { io.observe(el); });
  }

  // ── 5. Active section highlighting in site nav (anchor links) ──
  try {
    var anchorLinks = Array.prototype.slice.call(document.querySelectorAll('.nav-links a[href^="#"]'));
    if (anchorLinks.length) {
      var sections = anchorLinks.map(function (a) {
        return { link: a, el: document.querySelector(a.getAttribute('href')) };
      }).filter(function (o) { return o.el; });
      var navIO = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            anchorLinks.forEach(function (a) { a.classList.remove('active'); });
            var match = sections.filter(function (o) { return o.el === e.target; })[0];
            if (match) match.link.classList.add('active');
          }
        });
      }, { threshold: 0.4 });
      sections.forEach(function (o) { navIO.observe(o.el); });
    }
  } catch (e) {}

  // ── 6. Mobile menu: toggle + close-on-link-click ──
  // Some pages define onclick="toggleMobileMenu()" inline; only attach our
  // own toggle when the button has no inline handler (avoids double-toggle).
  var toggle = document.querySelector('.mobile-menu-toggle');
  var navLinks = document.querySelector('.nav-links');
  if (toggle && navLinks) {
    if (!toggle.getAttribute('onclick')) {
      toggle.addEventListener('click', function () {
        navLinks.classList.toggle('active');
        toggle.classList.toggle('is-open');
      });
    }
    // Always close the menu after a link is clicked (enhancement)
    navLinks.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', function () {
        navLinks.classList.remove('active');
        toggle.classList.remove('is-open');
      });
    });
  }

  // ── 7. Keep global-nav links in sync if injected on a /zh/ page ──
  // (links point to absolute site roots, which always show EN; that's fine
  //  — the per-site 中文 toggle lives in each site's own navbar.)

  // ── 8. Google AdSense ──
  if (location.hostname === 'www.vlsc.net' && !document.querySelector('script[src*="adsbygoogle"]')) {
    var adsenseScript = document.createElement('script');
    adsenseScript.async = true;
    adsenseScript.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7442510147240155';
    adsenseScript.crossOrigin = 'anonymous';
    document.head.appendChild(adsenseScript);
  }

})();
