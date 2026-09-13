(function () {
  'use strict';

  if (document.body.classList.contains('scope-js-loaded')) return;
  document.body.classList.add('scope-js-loaded');

  var SITE = document.body.getAttribute('data-site') || 'portal';
  var isCN = /^zh/.test(document.documentElement.lang || '') || /\/zh\//.test(location.pathname);

  var PATHS = {
    portal: '/',
    agentic: '/agentic.html',
    mrrc: '/mrrc/',
    mrrc_modern: '/mrrc_modern/',
    mrrc_ft8: '/mrrc_ft8/',
    sunmrrc: '/sunmrrc/',
    efhw: '/efhw/',
    blog: '/blog/'
  };

  var LABELS = isCN ? {
    brand: 'VLSC 项目',
    projects: '项目',
    back: '返回顶部'
  } : {
    brand: 'VLSC Projects',
    projects: 'Projects',
    back: 'Back to top'
  };

  function siteLink(key, label) {
    var active = key === SITE ? ' is-active' : '';
    return '<a href="' + PATHS[key] + '" data-site="' + key + '" class="' + active.trim() + '">' + label + '</a>';
  }

  var gn = document.createElement('div');
  gn.className = 'scope-gn';
  gn.innerHTML =
    '<div class="scope-gn-inner container">' +
      '<a class="scope-gn-brand" href="/">' +
        '<i class="fas fa-satellite-dish"></i>' +
        '<span>VLSC<span class="dot">·</span>Projects</span>' +
      '</a>' +
      '<nav class="scope-gn-links">' +
        siteLink('agentic', 'Agentic') +
        siteLink('mrrc', 'MRRC') +
        siteLink('mrrc_modern', 'Modern') +
        siteLink('mrrc_ft8', 'FT-8') +
        siteLink('sunmrrc', 'SunMRRC') +
        siteLink('efhw', 'EFHW') +
        siteLink('blog', 'Blog') +
      '</nav>' +
      '<a class="scope-gn-gh" href="https://github.com/cheenle" target="_blank" rel="noopener">' +
        '<i class="fab fa-github"></i>' +
      '</a>' +
    '</div>';
  document.body.insertBefore(gn, document.body.firstChild);
  document.body.classList.add('scope-gn-on');

  var sp = document.createElement('div');
  sp.className = 'scope-scroll-progress';
  document.body.appendChild(sp);

  var bt = document.createElement('button');
  bt.type = 'button';
  bt.className = 'scope-to-top';
  bt.setAttribute('aria-label', LABELS.back);
  bt.innerHTML = '<i class="fas fa-arrow-up"></i>';
  bt.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: 'smooth' }); });
  document.body.appendChild(bt);

  function supportsPassive() {
    var supports = false;
    try {
      var opts = Object.defineProperty({}, 'passive', {
        get: function () { supports = true; }
      });
      window.addEventListener('test', null, opts);
      window.removeEventListener('test', null, opts);
    } catch (e) {}
    return supports;
  }

  var navbar = document.querySelector('.scope-site-nav, .navbar, .header');
  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var st = window.scrollY || document.documentElement.scrollTop;
      var h = document.documentElement.scrollHeight - window.innerHeight;
      sp.style.transform = 'scaleX(' + (h > 0 ? (st / h) : 0) + ')';
      if (navbar) {
        if (st > 40) navbar.classList.add('scrolled');
        else navbar.classList.remove('scrolled');
      }
      if (st > 600) bt.classList.add('show');
      else bt.classList.remove('show');
      ticking = false;
    });
  }
  window.addEventListener('scroll', onScroll, supportsPassive() ? { passive: true } : false);
  onScroll();

  if ('IntersectionObserver' in window) {
    var reveal = document.querySelectorAll('.scope-reveal');
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    Array.prototype.forEach.call(reveal, function (el) { io.observe(el); });
  }

  var toggle = document.querySelector('.scope-site-nav-toggle, .mobile-menu-toggle');
  var navLinks = document.querySelector('.scope-site-nav-links, .nav-links');
  if (toggle && navLinks) {
    toggle.addEventListener('click', function () {
      navLinks.classList.toggle('active');
      toggle.classList.toggle('is-open');
    });
    Array.prototype.forEach.call(navLinks.querySelectorAll('a'), function (a) {
      a.addEventListener('click', function () {
        navLinks.classList.remove('active');
        toggle.classList.remove('is-open');
      });
    });
  }

  if (location.hostname === 'www.vlsc.net') {
    if (!document.querySelector('script[src*="adsbygoogle"]')) {
      var ads = document.createElement('script');
      ads.async = true;
      ads.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7442510147240155';
      ads.crossOrigin = 'anonymous';
      document.head.appendChild(ads);
    }
    if (!/\/feedback\/(admin|api)/.test(location.pathname)) {
      if (!document.querySelector('link[href*="feedback.css"]')) {
        var fbCss = document.createElement('link');
        fbCss.rel = 'stylesheet';
        fbCss.href = '/feedback/static/feedback.css?v=1';
        document.head.appendChild(fbCss);
      }
      if (!document.querySelector('script[src*="feedback.js"]')) {
        var fbJs = document.createElement('script');
        fbJs.src = '/feedback/static/feedback.js?v=1';
        document.body.appendChild(fbJs);
      }
    }
  }

  // ── Google Analytics (GA4) ──
  // 注入而非逐页内联：SDD 页由 build_sdd.py 生成，改 HTML 会在下次 rebuild 时静默丢失。
  // vlsc.net 后缀守卫使 file:// / localhost / 其他域名不上报；dataLayer 存在性检查
  // 兼作幂等守卫（同页同时加载 scope.js + global-nav.js 时只注入一次），并保证
  // 将来某页若内联官方片段，这里自动让位、不会重复计数。
  if (/(^|\.)vlsc\.net$/.test(location.hostname) && !window.dataLayer) {
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    var gaScript = document.createElement('script');
    gaScript.async = true;
    gaScript.src = 'https://www.googletagmanager.com/gtag/js?id=G-JQLSKV1MCT';
    document.head.appendChild(gaScript);
    window.gtag('js', new Date());
    window.gtag('config', 'G-JQLSKV1MCT');
  }
})();
