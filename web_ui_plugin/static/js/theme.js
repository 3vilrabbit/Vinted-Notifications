/* Appearance: System, Light or Dark.
 * The choice is kept per device (localStorage) and applied before first paint, so there is no
 * flash of the wrong theme. "System" follows the device setting through prefers-color-scheme. */
(function () {
    var KEY = 'vn-theme';
    var root = document.documentElement;

    function stored() {
        try { return localStorage.getItem(KEY) || 'system'; } catch (e) { return 'system'; }
    }

    function metas(mode) {
        document.querySelectorAll('meta[name="theme-color"]').forEach(function (meta) {
            if (!meta.dataset.original) meta.dataset.original = meta.getAttribute('content');
            meta.setAttribute('content', mode === 'dark' ? '#000000' : mode === 'light' ? '#F2F2F7' : meta.dataset.original);
        });
    }

    function sync(mode) {
        document.querySelectorAll('[data-theme-set]').forEach(function (button) {
            var on = button.dataset.themeSet === mode;
            button.classList.toggle('active', on);
            button.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    function apply(mode) {
        if (mode === 'light' || mode === 'dark') root.setAttribute('data-theme', mode);
        else root.removeAttribute('data-theme');
        metas(mode);
        sync(mode);
    }

    window.vnTheme = {
        get: stored,
        set: function (mode) {
            try { localStorage.setItem(KEY, mode); } catch (e) { /* private mode: keep it for this page only */ }
            apply(mode);
        }
    };

    apply(stored());
    document.addEventListener('DOMContentLoaded', function () {
        apply(stored());
        document.addEventListener('click', function (event) {
            var button = event.target.closest('[data-theme-set]');
            if (button) window.vnTheme.set(button.dataset.themeSet);
        });
    });
})();
