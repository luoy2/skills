(function () {
  'use strict';

  var root = document.documentElement;
  var STORE_KEY = 'agent-retro-lang';

  var TEXT = {
    en: {
      title: 'Agent Retro: test new models on your own problems',
      'lang-group': 'Language',
      github: 'luoy2/skills on GitHub',
      video: 'Agent Retro intro video',
      'mock-q': 'Example questionnaire',
      'mock-ocd': 'Example store and reply ending',
      copied: 'Copied to the clipboard.',
      copyFailed: 'Copy failed. Select the command and copy it by hand.'
    },
    zh: {
      title: 'Agent Retro：用你自己的问题测新模型',
      'lang-group': '语言',
      github: 'GitHub 上的 luoy2/skills',
      video: 'Agent Retro 介绍视频',
      'mock-q': '问卷示例',
      'mock-ocd': '记录文件与回复结尾示例',
      copied: '已复制到剪贴板。',
      copyFailed: '复制失败，请手动选中命令复制。'
    }
  };

  function current() { return root.lang === 'zh-CN' ? 'zh' : 'en'; }

  function apply(lang, persist) {
    root.lang = lang === 'zh' ? 'zh-CN' : 'en';
    var t = TEXT[lang];
    document.title = t.title;
    var labelled = document.querySelectorAll('[data-i18n-aria]');
    for (var i = 0; i < labelled.length; i++) {
      var key = labelled[i].getAttribute('data-i18n-aria');
      if (t[key]) labelled[i].setAttribute('aria-label', t[key]);
    }
    var buttons = document.querySelectorAll('.lang-toggle button');
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].setAttribute('aria-pressed', String(buttons[j].getAttribute('data-lang') === lang));
    }
    if (persist) {
      try { localStorage.setItem(STORE_KEY, lang); } catch (e) {}
      try {
        // Keep a shared ?lang= link consistent with the reader's choice.
        var url = new URL(location.href);
        if (url.searchParams.has('lang')) {
          url.searchParams.set('lang', lang);
          history.replaceState(null, '', url.toString());
        }
      } catch (e) {}
    }
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

  function copied(btn) {
    btn.classList.add('is-copied');
    announce(TEXT[current()].copied);
    window.clearTimeout(btn._copyTimer);
    btn._copyTimer = window.setTimeout(function () { btn.classList.remove('is-copied'); }, 2000);
  }

  var copyButtons = document.querySelectorAll('button.copy[data-copy]');
  for (var k = 0; k < copyButtons.length; k++) {
    copyButtons[k].addEventListener('click', function () {
      var btn = this;
      var text = btn.getAttribute('data-copy');
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { copied(btn); }, function () {
          if (fallbackCopy(text)) copied(btn); else announce(TEXT[current()].copyFailed);
        });
      } else if (fallbackCopy(text)) {
        copied(btn);
      } else {
        announce(TEXT[current()].copyFailed);
      }
    });
  }

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
