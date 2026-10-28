// An eye on every password field: tap to show what was typed, tap again to hide it.
(function () {
    function labels() {
        const tag = document.querySelector('script[data-pw-show]');
        return {
            show: (tag && tag.getAttribute('data-pw-show')) || 'Show password',
            hide: (tag && tag.getAttribute('data-pw-hide')) || 'Hide password'
        };
    }

    function attach(input) {
        if (input.dataset.pwEye) return;
        input.dataset.pwEye = '1';
        const text = labels();
        const wrap = document.createElement('span');
        wrap.className = 'pw-wrap';
        // margins set on the field (mb-3 ...) belong to the wrapper that now takes its place
        Array.prototype.slice.call(input.classList).forEach(function (name) {
            if (/^m[tby]-\d$/.test(name)) { wrap.classList.add(name); input.classList.remove(name); }
        });
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pw-eye';
        button.setAttribute('aria-label', text.show);
        button.setAttribute('aria-pressed', 'false');
        button.innerHTML = '<i class="bi bi-eye"></i>';
        button.addEventListener('mousedown', function (e) { e.preventDefault(); });  // keep focus (and the keyboard) in the field
        button.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            const shown = input.type === 'text';
            input.type = shown ? 'password' : 'text';
            button.setAttribute('aria-pressed', shown ? 'false' : 'true');
            button.setAttribute('aria-label', shown ? text.show : text.hide);
            button.firstChild.className = shown ? 'bi bi-eye' : 'bi bi-eye-slash';
        });
        wrap.appendChild(button);
    }

    function run() { document.querySelectorAll('input[type="password"]').forEach(attach); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
