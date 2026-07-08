document.addEventListener('DOMContentLoaded', () => {
    const recommendationForm = document.getElementById('recommendationForm'); // grab all the dom bits we poke at a lot
    const librarySelect = document.getElementById('librarySelect');
    const customSubdomainGroup = document.getElementById('customSubdomainGroup');
    const customSubdomainInput = document.getElementById('customSubdomainInput');
    const apiKeyInput = document.getElementById('apiKeyInput');
    const apiProviderSelect = document.getElementById('apiProviderSelect');
    const testKeyBtn = document.getElementById('testKeyBtn');
    const testKeyResult = document.getElementById('testKeyResult');

    const loadingState = document.getElementById('loadingState'); // loading ui bits
    const progressBar = document.getElementById('progressBar');
    const loadingTitle = document.getElementById('loadingTitle');
    const loadingMsg = document.getElementById('loadingMsg');

    const stepAi = document.getElementById('step-ai'); // stepper ui bits
    const stepCovers = document.getElementById('step-covers');
    const stepCatalog = document.getElementById('step-catalog');

    const resultsSection = document.getElementById('resultsSection'); // results ui bits
    const resultsGrid = document.getElementById('resultsGrid');
    const resultsCount = document.getElementById('resultsCount');

    const clarificationSection = document.getElementById('clarificationSection'); // clarification ui bits
    const clarificationOptions = document.getElementById('clarificationOptions');
    const clarificationOther = document.getElementById('clarificationOther');
    const clarificationOtherBtn = document.getElementById('clarificationOtherBtn');

    let _lastSearchState = null; // stash last search so if user clarifies, we can re-run w/o retyping everything

    const savedProvider = localStorage.getItem('api_provider') || 'gemini'; // load saved provider + its key (each provider gets its own saved key)
    apiProviderSelect.value = savedProvider;

    const savedKey = localStorage.getItem(`${savedProvider}_api_key`);
    if (savedKey) apiKeyInput.value = savedKey;

    apiProviderSelect.addEventListener('change', () => { // when provider changes, swap in the saved key for that provider
        const key = localStorage.getItem(`${apiProviderSelect.value}_api_key`);
        apiKeyInput.value = key || '';
        testKeyResult.classList.add('hidden');
    });

    testKeyBtn.addEventListener('click', async () => { // "test" button: hit backend w/ a tiny prompt to validate key works
        const key = apiKeyInput.value.trim();
        const provider = apiProviderSelect.value;

        if (!key) { // no key = nothing to test, an api key is required now
            testKeyResult.className = 'test-key-result failure';
            testKeyResult.innerHTML =
                '<i class="fa-solid fa-circle-exclamation"></i> Please enter an API key.';
            testKeyResult.classList.remove('hidden');
            return;
        }

        testKeyBtn.disabled = true; // ui: disable + spinner
        testKeyBtn.classList.add('testing');
        testKeyBtn.innerHTML = '<i class="fa-solid fa-spinner"></i> Testing';
        testKeyResult.classList.add('hidden');

        try {
            const resp = await fetch('/api/test-key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_key: key, api_provider: provider })
            });

            const data = await resp.json();

            if (data.ok) {
                testKeyResult.className = 'test-key-result success';
                testKeyResult.innerHTML =
                    `<i class="fa-solid fa-circle-check"></i> Key works! ${provider} responded successfully.`;
            } else {
                testKeyResult.className = 'test-key-result failure';
                testKeyResult.innerHTML =
                    `<i class="fa-solid fa-circle-exclamation"></i> ${escapeHtml(data.error || 'Key test failed.')}`;
            }
        } catch (error) { // network/server down/etc
            testKeyResult.className = 'test-key-result failure';
            testKeyResult.innerHTML =
                `<i class="fa-solid fa-circle-exclamation"></i> Could not reach the server: ${escapeHtml(error.message)}`;
        } finally {
            testKeyResult.classList.remove('hidden'); // ui: restore button
            testKeyBtn.disabled = false;
            testKeyBtn.classList.remove('testing');
            testKeyBtn.innerHTML = '<i class="fa-solid fa-plug-circle-check"></i> Test';
        }
    });

    librarySelect.addEventListener('change', () => { // show/hide custom domain input
        if (librarySelect.value === 'custom') {
            customSubdomainGroup.classList.remove('hidden');
            customSubdomainInput.setAttribute('required', 'true');
            customSubdomainInput.focus();
        } else {
            customSubdomainGroup.classList.add('hidden');
            customSubdomainInput.removeAttribute('required');
        }
    });

    recommendationForm.addEventListener('submit', async (e) => { // main form submit
        e.preventDefault();

        const bookTitle = document.getElementById('bookInput').value.trim(); // collect inputs
        const apiKey = apiKeyInput.value.trim();
        const apiProvider = apiProviderSelect.value;

        let libraryDomain = librarySelect.value; // resolve library domain (preset or custom)
        if (libraryDomain === 'custom') {
            let customVal = customSubdomainInput.value.trim().toLowerCase();

            customVal = customVal.replace(/^(https?:\/\/)?(www\.)?/, '').split('/')[0]; // normalize: strip protocol/www, strip path

            if (!customVal.includes('bibliocommons.com')) { // if they typed just "nypl", tack on the base domain
                customVal = customVal + '.bibliocommons.com';
            }
            libraryDomain = customVal;
        }

        const checkedFormats = Array.from(document.querySelectorAll('input[name="formats"]:checked')) // formats = checked checkboxes
            .map(cb => cb.value);

        if (checkedFormats.length === 0) {
            alert('Please select at least one preferred book format.');
            return;
        }

        localStorage.setItem('api_provider', apiProvider); // persist provider + key (per provider)
        if (apiKey) localStorage.setItem(`${apiProvider}_api_key`, apiKey);
        else localStorage.removeItem(`${apiProvider}_api_key`);

        _lastSearchState = { bookTitle, libraryDomain, checkedFormats, apiKey, apiProvider }; // save state for clarification rerun

        clarificationSection.classList.add('hidden'); // ui: reset + show loading
        resultsSection.classList.add('hidden');
        loadingState.classList.remove('hidden');
        resetLoadingProgress();

        let progressPercent = 5; // fake progress so it feels alive
        progressBar.style.width = `${progressPercent}%`;

        loadingTitle.textContent = "Checking Book Title";
        loadingMsg.textContent = `Verifying "${bookTitle}" with the AI...`;

        const clarifyInterval = setInterval(() => {
            if (progressPercent < 25) {
                progressPercent += 3;
                progressBar.style.width = `${progressPercent}%`;
            }
        }, 300);

        try {
            const clarifResp = await fetch('/api/clarify', { // step 0: ask backend if title is ambiguous
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ book_input: bookTitle, api_key: apiKey, api_provider: apiProvider })
            });

            clearInterval(clarifyInterval);

            if (!clarifResp.ok) throw new Error('Clarification request failed');
            const clarifData = await clarifResp.json();

            if (clarifData.needs_clarification && clarifData.candidates && clarifData.candidates.length > 0) { // if ambiguous, show picker ui and stop here
                loadingState.classList.add('hidden');
                renderClarificationMenu(clarifData.candidates, bookTitle);
                return;
            }

            const resolvedTitle = clarifData.resolved_title || bookTitle; // otherwise proceed w/ resolved title (or original)
            await runRecommendations(resolvedTitle, libraryDomain, checkedFormats, apiKey, apiProvider, progressPercent);

        } catch (error) {
            clearInterval(clarifyInterval);
            loadingState.classList.add('hidden');
            alert(`Error: ${error.message}`);
            console.error('Error:', error);
        }
    });

    clarificationOtherBtn.addEventListener('click', () => { // clarification "other" path: user types exact title
        const custom = clarificationOther.value.trim();
        if (!custom) { clarificationOther.focus(); return; }

        const s = _lastSearchState;
        if (!s) return;

        clarificationSection.classList.add('hidden');
        runRecommendationsWithLoading(custom, s.libraryDomain, s.checkedFormats, s.apiKey, s.apiProvider);
    });

    clarificationOther.addEventListener('keydown', (e) => { // enter key triggers the same as clicking the arrow btn
        if (e.key === 'Enter') clarificationOtherBtn.click();
    });

    function renderClarificationMenu(candidates, originalInput) {
        clarificationOptions.innerHTML = ''; // build clickable cards for each candidate

        candidates.forEach((book, idx) => {
            const card = document.createElement('div');
            card.className = 'clarification-option-card';

            card.style.animation = `fadeInUp 0.4s ease ${idx * 0.08}s both`; // stagger animation so it feels fancy

            card.innerHTML = `
                <div class="clarif-option-icon"><i class="fa-solid fa-book"></i></div>
                <div class="clarif-option-info">
                    <div class="clarif-option-title" title="${escapeHtml(book.title)}">${escapeHtml(book.title)}</div>
                    <div class="clarif-option-author">${escapeHtml(book.author)}</div>
                    ${book.year ? `<div class="clarif-option-year">${book.year}</div>` : ''}
                    ${book.description ? `<div class="clarif-option-year" style="margin-top:0.35rem;white-space:normal;font-style:italic;">${escapeHtml(book.description)}</div>` : ''}
                </div>
            `;

            card.addEventListener('click', () => {
                document.querySelectorAll('.clarification-option-card') // ui: mark selected
                    .forEach(c => c.classList.remove('selected'));
                card.classList.add('selected');

                setTimeout(() => { // tiny delay so selection feels responsive
                    clarificationSection.classList.add('hidden');
                    const s = _lastSearchState;

                    runRecommendationsWithLoading( // pass "title by author" back to backend for better disambiguation
                        `${book.title} by ${book.author}`,
                        s.libraryDomain,
                        s.checkedFormats,
                        s.apiKey,
                        s.apiProvider
                    );
                }, 300);
            });

            clarificationOptions.appendChild(card);
        });

        clarificationOther.value = '';
        document.getElementById('clarificationMsg').textContent =
            `The AI found multiple books matching "${originalInput}". Please pick the one you meant:`;

        clarificationSection.classList.remove('hidden');
        clarificationSection.scrollIntoView({ behavior: 'smooth' });
    }

    async function runRecommendationsWithLoading(resolvedTitle, libraryDomain, checkedFormats, apiKey, apiProvider) {
        resultsSection.classList.add('hidden'); // reset ui then run the real call
        loadingState.classList.remove('hidden');
        resetLoadingProgress();
        await runRecommendations(resolvedTitle, libraryDomain, checkedFormats, apiKey, apiProvider, 5);
    }

    async function runRecommendations(resolvedTitle, libraryDomain, checkedFormats, apiKey, apiProvider, startProgress) {
        let progressPercent = startProgress || 5; // progress bar starts wherever clarify left off
        progressBar.style.width = `${progressPercent}%`;

        loadingTitle.textContent = "Consulting AI Assistant";
        loadingMsg.textContent = `Generating smart recommendations for "${resolvedTitle}"...`;

        const aiInterval = setInterval(() => { // fake progress while waiting on backend
            if (progressPercent < 45) {
                progressPercent += 3;
                progressBar.style.width = `${progressPercent}%`;
            }
        }, 300);

        try {
            const payload = {
                book_title: resolvedTitle,
                library_domain: libraryDomain,
                formats: checkedFormats,
                api_key: apiKey,
                api_provider: apiProvider
            };

            const response = await fetch('/api/recommend', { // backend does: ai recs + covers + catalog scrape
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            clearInterval(aiInterval);

            if (!response.ok) { // if backend returns error json, surface it
                const errData = await response.json();
                throw new Error(errData.error || 'Server error occurred');
            }

            setStepCompleted(stepAi); // ui: stepper updates (note: backend already did covers/catalog, this is just ux)
            setStepActive(stepCovers);

            progressPercent = 55;
            progressBar.style.width = `${progressPercent}%`;
            loadingTitle.textContent = "Fetching Book Details";
            loadingMsg.textContent = "Retrieving cover images from Open Library...";

            const coverInterval = setInterval(() => {
                if (progressPercent < 75) {
                    progressPercent += 4;
                    progressBar.style.width = `${progressPercent}%`;
                }
            }, 250);

            await sleep(800);
            clearInterval(coverInterval);

            setStepCompleted(stepCovers);
            setStepActive(stepCatalog);

            progressPercent = 80;
            progressBar.style.width = `${progressPercent}%`;
            loadingTitle.textContent = "Checking Library Availability";

            const libraryName = librarySelect.options[librarySelect.selectedIndex].text;
            loadingMsg.textContent = `Searching ${libraryName} for available formats...`;

            const catalogInterval = setInterval(() => {
                if (progressPercent < 95) {
                    progressPercent += 2;
                    progressBar.style.width = `${progressPercent}%`;
                }
            }, 300);

            const data = await response.json(); // now actually read the response body

            clearInterval(catalogInterval);
            progressBar.style.width = '100%';
            setStepCompleted(stepCatalog);

            await sleep(400);

            loadingState.classList.add('hidden'); // ui: show results
            renderRecommendations(data.recommendations, resolvedTitle);

        } catch (error) {
            clearInterval(aiInterval);
            loadingState.classList.add('hidden');
            alert(`Error: ${error.message}`);
            console.error('Error fetching recommendations:', error);
        }
    }

    function sleep(ms) { // tiny promise-based sleep for ui pacing
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function resetLoadingProgress() {
        progressBar.style.width = '0%'; // reset bar + stepper icons

        stepAi.className = 'step active';
        stepAi.querySelector('i').className = 'fa-regular fa-circle-play';

        stepCovers.className = 'step';
        stepCovers.querySelector('i').className = 'fa-regular fa-circle-dot';

        stepCatalog.className = 'step';
        stepCatalog.querySelector('i').className = 'fa-regular fa-circle-dot';
    }

    function setStepCompleted(stepElem) {
        stepElem.className = 'step completed';
        stepElem.querySelector('i').className = 'fa-solid fa-circle-check';
    }

    function setStepActive(stepElem) {
        stepElem.className = 'step active';
        stepElem.querySelector('i').className = 'fa-regular fa-circle-play';
    }

    function renderRecommendations(recommendations, sourceBook) {
        resultsGrid.innerHTML = ''; // wipe old results

        if (!recommendations || recommendations.length === 0) { // no recs? show a friendly msg
            resultsCount.textContent = `No recommendations generated for "${sourceBook}".`;
            resultsSection.classList.remove('hidden');
            return;
        }

        resultsCount.innerHTML =
            `Showing smart recommendations based on <strong>${escapeHtml(sourceBook)}</strong>`;

        recommendations.forEach((rec, idx) => {
            const card = document.createElement('div');
            card.className = 'recommend-card card';
            card.style.animation = `fadeInUp 0.6s ease ${idx * 0.1}s both`;

            let coverHtml = ''; // cover img if we have it
            if (rec.cover_url) {
                // onerror: hide broken img + show placeholder div
                coverHtml = `<img src="${escapeHtml(rec.cover_url)}" alt="${escapeHtml(rec.title)} Cover" class="book-cover-img" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">`;
            }

            // placeholder cover (also used when img fails)
            const coverPlaceholder = `
                <div class="cover-placeholder" style="display: ${rec.cover_url ? 'none' : 'flex'}">
                    <i class="fa-solid fa-book-open cover-placeholder-icon"></i>
                    <span class="cover-placeholder-title">${escapeHtml(rec.title)}</span>
                </div>
            `;

            let catalogHtml = ''; // catalog matches (up to 3 from backend scrape)
            if (rec.catalog_items && rec.catalog_items.length > 0) {
                catalogHtml = `
                    <div class="catalog-status-title">
                        <i class="fa-solid fa-square-poll-horizontal"></i> Library Catalog Matches
                    </div>
                    <div class="catalog-status-list">
                        ${rec.catalog_items.map(item => {
                            const badgeClass = item.is_available ? 'available' : 'unavailable';
                            const statusText = item.status || (item.is_available ? 'Available' : 'Check Catalog');
                            const iconClass = getFormatIcon(item.format);

                            return `
                                <div class="catalog-item-row">
                                    <div class="format-label">
                                        <i class="${iconClass}"></i>
                                        <span>${escapeHtml(item.format)}</span>
                                    </div>
                                    <div class="status-badge-container">
                                        <span class="status-badge ${badgeClass}">
                                            <span class="status-dot"></span>
                                            ${escapeHtml(statusText)}
                                        </span>
                                        ${item.link ? `
                                            <a href="${escapeHtml(item.link)}" target="_blank" class="btn-catalog-link">
                                                <span>Check out</span>
                                                <i class="fa-solid fa-arrow-up-right-from-square"></i>
                                            </a>
                                        ` : ''}
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                `;
            } else {
                // no matches: give user a direct search link
                const searchLink =
                    `https://${librarySelect.value === 'custom'
                        ? customSubdomainInput.value.trim()
                        : librarySelect.value
                    }/v2/search?query=${encodeURIComponent(rec.title)}&searchType=keyword`;

                catalogHtml = `
                    <div class="no-match-alert">
                        <i class="fa-solid fa-circle-exclamation"></i>
                        <span>No matched items found in library catalog.</span>
                        <a href="${searchLink}" target="_blank" class="btn-catalog-link" style="margin-left: auto;">
                            <span>Search catalog</span>
                            <i class="fa-solid fa-arrow-up-right-from-square"></i>
                        </a>
                    </div>
                `;
            }

            // final card markup
            card.innerHTML = `
                <div class="book-cover-wrapper">
                    ${coverHtml}
                    ${coverPlaceholder}
                </div>
                <div class="book-info-panel">
                    <div class="book-title-row">
                        <h3>${escapeHtml(rec.title)}</h3>
                        <div class="book-author">by ${escapeHtml(rec.author)}</div>
                    </div>
                    <div class="ai-reason-box">
                        <div class="ai-badge">
                            <i class="fa-solid fa-robot"></i> AI Reason
                        </div>
                        <div>"${escapeHtml(rec.reason)}"</div>
                    </div>
                    <div class="catalog-status-section">
                        ${catalogHtml}
                    </div>
                </div>
            `;

            resultsGrid.appendChild(card);
        });

        resultsSection.classList.remove('hidden');
        resultsSection.scrollIntoView({ behavior: 'smooth' });
    }

    function getFormatIcon(format) { // map format string -> fontawesome icon class
        format = format.toLowerCase();
        if (format.includes('ebook')) return 'fa-solid fa-tablet-screen-button';
        if (format.includes('audiobook')) return 'fa-solid fa-headphones';
        if (format.includes('dvd') || format.includes('video') || format.includes('bluray')) return 'fa-solid fa-compact-disc';
        if (format.includes('music') || format.includes('sound')) return 'fa-solid fa-music';
        return 'fa-solid fa-book';
    }

    function escapeHtml(str) { // basic xss guard for anything we inject into innerHTML
        if (!str) return '';
        return str
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }
});
