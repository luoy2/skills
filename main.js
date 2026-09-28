(function () {
  'use strict';

  var root = document.documentElement;
  var STORE_KEY = 'agent-retro-lang';

  var TEXT = {
    en: {
      'lang-group': 'Language',
      github: 'luoy2/skills on GitHub',
      'mock-q': 'Example questionnaire',
      'mock-ocd': 'Example store and reply ending',
      copiedInstall: 'Copied. Paste it into Claude Code or Codex.',
      copied: 'Copied to the clipboard.',
      copyFailed: 'Copy failed. Select the text and copy it by hand.'
    },
    zh: {
      'lang-group': '语言',
      github: 'GitHub 上的 luoy2/skills',
      'mock-q': '问卷示例',
      'mock-ocd': '记录文件与回复结尾示例',
      copiedInstall: '已复制。粘到 Claude Code 或 Codex 里。',
      copied: '已复制到剪贴板。',
      copyFailed: '复制失败，请手动选中文字复制。'
    }
  };

  function current() { return root.lang === 'zh-CN' ? 'zh' : 'en'; }

  function pageHasLangParam() {
    try { return new URL(location.href).searchParams.has('lang'); } catch (e) { return false; }
  }

  // A shared ?lang= link keeps its language on every page it leads to, even where storage is blocked.
  function syncLinks(lang) {
    if (!pageHasLangParam()) return;
    var links = document.querySelectorAll('a[href]');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var base = a.getAttribute('data-base-href');
      if (base === null) {
        base = a.getAttribute('href');
        if (!base || base.charAt(0) === '#' || /^[a-z][a-z0-9+.-]*:/i.test(base)) continue;
        a.setAttribute('data-base-href', base);
      }
      try {
        var u = new URL(base, location.href);
        u.searchParams.set('lang', lang);
        a.setAttribute('href', u.pathname + u.search + u.hash);
      } catch (e) {}
    }
  }

  function apply(lang, persist) {
    root.lang = lang === 'zh' ? 'zh-CN' : 'en';
    var t = TEXT[lang];
    var title = root.getAttribute('data-title-' + lang);
    if (title) document.title = title;
    var labelled = document.querySelectorAll('[data-i18n-aria]');
    for (var i = 0; i < labelled.length; i++) {
      var key = labelled[i].getAttribute('data-i18n-aria');
      if (t[key]) labelled[i].setAttribute('aria-label', t[key]);
    }
    var own = document.querySelectorAll('[data-aria-' + lang + ']');
    for (var o = 0; o < own.length; o++) {
      own[o].setAttribute('aria-label', own[o].getAttribute('data-aria-' + lang));
    }
    var buttons = document.querySelectorAll('.lang-toggle button');
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].setAttribute('aria-pressed', String(buttons[j].getAttribute('data-lang') === lang));
    }
    if (persist) {
      try { localStorage.setItem(STORE_KEY, lang); } catch (e) {}
      try {
        var url = new URL(location.href);
        if (url.searchParams.has('lang')) {
          url.searchParams.set('lang', lang);
          history.replaceState(null, '', url.toString());
        }
      } catch (e) {}
    }
    syncLinks(lang);
  }

  apply(current(), false);

  var toggle = document.querySelector('.lang-toggle');
  if (toggle) {
    toggle.addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-lang]');
      if (!btn) return;
      apply(btn.getAttribute('data-lang'), true);
    });
  }

  /* ---------- Copy buttons ---------- */
  var status = document.getElementById('copy-status');

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function announce(msg) {
    if (!status) return;
    status.textContent = '';
    window.setTimeout(function () { status.textContent = msg; }, 30);
  }

  function copied(btn, msgKey) {
    btn.classList.add('is-copied');
    announce(TEXT[current()][msgKey]);
    window.clearTimeout(btn._copyTimer);
    btn._copyTimer = window.setTimeout(function () { btn.classList.remove('is-copied'); }, 2200);
  }

  // The install button copies the <pre> shown under it for the current language, so what
  // the reader sees is exactly what lands on the clipboard.
  function textFor(btn) {
    if (btn.hasAttribute('data-copy-install')) {
      var card = btn.closest('.install-card');
      var pre = card && card.querySelector('.install-text.' + current());
      return pre ? pre.textContent : '';
    }
    return btn.getAttribute('data-copy') || '';
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-copy], button[data-copy-install]');
    if (!btn) return;
    var text = textFor(btn);
    if (!text) return;
    var msgKey = btn.hasAttribute('data-copy-install') ? 'copiedInstall' : 'copied';
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { copied(btn, msgKey); }, function () {
        if (fallbackCopy(text)) copied(btn, msgKey); else announce(TEXT[current()].copyFailed);
      });
    } else if (fallbackCopy(text)) {
      copied(btn, msgKey);
    } else {
      announce(TEXT[current()].copyFailed);
    }
  });

  /* ---------- Chart entrance: bars grow from the baseline once, when they scroll in ---------- */
  var reduce = false;
  try { reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
  if (!reduce && 'IntersectionObserver' in window) {
    var charts = document.querySelectorAll('[data-animate]');
    var io = new IntersectionObserver(function (entries) {
      for (var n = 0; n < entries.length; n++) {
        if (entries[n].isIntersecting) {
          entries[n].target.classList.add('in');
          io.unobserve(entries[n].target);
        }
      }
    }, { rootMargin: '0px 0px -12% 0px', threshold: 0.2 });
    for (var m = 0; m < charts.length; m++) {
      var r = charts[m].getBoundingClientRect();
      // Only hold back charts that start below the fold; anything already visible stays drawn.
      if (r.top > window.innerHeight) {
        charts[m].classList.add('will-animate');
        io.observe(charts[m]);
      }
    }
  }
})();
