/*
 * WispAyr Design System v1.0
 * Shared JavaScript for all WispAyr Ayrshire apps
 */

(function() {
  'use strict';

  const WISPAYR = {
    apps: [
      { name: 'Hub', url: 'https://ayrshire.wispayr.online', key: 'hub' },
      { name: 'News', url: 'https://news.ayrshire.wispayr.online', key: 'news' },
      { name: 'Fuel', url: 'https://fuel.wispayr.online', key: 'fuel' },
      { name: 'Trains', url: 'https://trains.wispayr.online', key: 'trains' },
      { name: 'Weather', url: 'https://weather.ayrshire.wispayr.online', key: 'weather' },
      { name: 'Traffic', url: 'https://traffic.ayrshire.wispayr.online', key: 'traffic' },
    ],

    // Detect which app we're on
    detectApp() {
      const host = window.location.hostname;
      if (host.includes('news')) return 'news';
      if (host.includes('fuel')) return 'fuel';
      if (host.includes('trains')) return 'trains';
      if (host.includes('weather')) return 'weather';
      if (host.includes('traffic')) return 'traffic';
      if (host.includes('ayrshire')) return 'hub';
      return null;
    },

    // Dark mode
    initTheme() {
      const saved = localStorage.getItem('wispayr-theme');
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      const theme = saved || (prefersDark ? 'dark' : 'light');
      document.documentElement.setAttribute('data-theme', theme);
      this.updateToggleIcon(theme);
    },

    toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('wispayr-theme', next);
      this.updateToggleIcon(next);
    },

    updateToggleIcon(theme) {
      const btn = document.querySelector('.w-theme-toggle');
      if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
    },

    // Inject header
    injectHeader(activeKey) {
      const currentApp = activeKey || this.detectApp();
      const nav = this.apps.map(app => {
        const cls = app.key === currentApp ? ' class="active"' : '';
        return `<a href="${app.url}"${cls}>${app.name}</a>`;
      }).join('\n        ');

      const html = `
    <div class="w-header-inner">
      <a class="w-logo" href="https://ayrshire.wispayr.online">
        <div class="w-logo-mark">W</div>
        <div class="w-logo-text">Wisp<span>Ayr</span></div>
      </a>
      <nav class="w-nav" id="wNav">
        ${nav}
      </nav>
      <div class="w-header-actions">
        <button class="w-menu-toggle" id="wMenuToggle" aria-label="Menu">☰</button>
        <button class="w-theme-toggle" id="wThemeToggle" aria-label="Toggle theme">🌙</button>
      </div>
    </div>`;

      const header = document.querySelector('.w-header');
      if (header) {
        header.innerHTML = html;
      } else {
        const el = document.createElement('header');
        el.className = 'w-header';
        el.innerHTML = html;
        document.body.insertBefore(el, document.body.firstChild);
      }

      // Bind events
      const toggle = document.getElementById('wThemeToggle');
      if (toggle) toggle.addEventListener('click', () => this.toggleTheme());

      const menuToggle = document.getElementById('wMenuToggle');
      if (menuToggle) {
        menuToggle.addEventListener('click', () => {
          document.getElementById('wNav').classList.toggle('open');
        });
      }
    },

    // Inject footer
    injectFooter() {
      const existing = document.querySelector('.w-footer');
      if (existing) return; // Don't double-inject

      const footer = document.createElement('footer');
      footer.className = 'w-footer';
      footer.innerHTML = `
    <div class="w-container">
      <p>© ${new Date().getFullYear()} <a href="https://wispayr.online">WispAyr</a> · Ayr, Scotland · Open data, open tools</p>
    </div>`;
      document.body.appendChild(footer);
    },

    // Full init
    init(activeKey) {
      this.initTheme();
      this.injectHeader(activeKey);
      this.injectFooter();
    }
  };

  // Expose globally
  window.WispAyr = WISPAYR;

  // Auto-init when DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => WISPAYR.init());
  } else {
    WISPAYR.init();
  }
})();
