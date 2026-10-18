// Tag-style input for banwords. Each field is found by the prefix passed to the
// banwords_field macro in queries.html: <prefix>-banword-input, -banword-add,
// -banword-tags and -banwords (the hidden input that is submitted as "banwords").
function initBanwordsField(prefix) {
    const input = document.getElementById(`${prefix}-banword-input`);
    const addButton = document.getElementById(`${prefix}-banword-add`);
    const container = document.getElementById(`${prefix}-banword-tags`);
    const hiddenInput = document.getElementById(`${prefix}-banwords`);

    function updateHiddenInput() {
        const tags = container.querySelectorAll('.banword-tag');
        hiddenInput.value = Array.from(tags).map(tag => tag.dataset.word).join('|||');
    }

    function addTag(word) {
        const trimmed = word.trim();
        if (!trimmed) return;

        const exists = Array.from(container.querySelectorAll('.banword-tag'))
            .some(tag => tag.dataset.word.toLowerCase() === trimmed.toLowerCase());
        if (exists) return;

        const tag = document.createElement('span');
        tag.className = 'badge bg-secondary me-1 mb-1 banword-tag';
        tag.dataset.word = trimmed;

        const label = document.createElement('span');
        label.textContent = trimmed;

        const removeIcon = document.createElement('i');
        removeIcon.className = 'bi bi-x-circle ms-1';
        removeIcon.style.cursor = 'pointer';
        removeIcon.addEventListener('click', function () {
            tag.remove();
            updateHiddenInput();
        });

        tag.append(label, removeIcon);
        container.appendChild(tag);
        updateHiddenInput();
    }

    addButton.addEventListener('click', function () {
        addTag(input.value);
        input.value = '';
        input.focus();
    });

    // Enter adds the word instead of submitting the form
    input.addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            addTag(input.value);
            input.value = '';
        }
    });

    hiddenInput.value.split('|||').forEach(addTag);
}
