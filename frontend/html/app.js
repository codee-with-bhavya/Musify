const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
        const API_BASE = IS_LOCAL
            ? 'http://localhost:8000'
            : 'https://musify-08mg.onrender.com';

        const audio = document.getElementById('audioEl');
        const searchInput = document.getElementById('search-input');
        let queue = [], queueIndex = -1, shuffle = false, repeat = false, currentFilter = '';
        let debounceTimer;
        let lastSearchResults = [];
        let homeSectionData = [];
        let exploreSectionData = [];
        let detailSectionData = [];
        let currentViewTracks = [];
        let currentDetailBrowseId = null;
        let currentContinuation = null;
        let detailVersion = 0;
        let likedTracks = [];
        let sessionRecentlyPlayed = [];
        let panelContext = { kind: 'home', title: 'Musify', subtitle: 'Pick a song to start listening', thumbnail: '' };
        let queueExpanded = false;
        let navigationVersion = 0;
        let playbackFailures = new Set();

        function refreshSidebarLibrary() {
            const el = document.getElementById('sidebar-liked-count');
            if (el) { const n = getLiked().length; el.textContent = `Playlist · ${n} song${n === 1 ? '' : 's'}`; }
            const box = document.getElementById('sidebar-playlists');
            if (box) {
                box.innerHTML = getPlaylists().map(pl => `
                    <div class="sidebar-row" onclick="openLocalPlaylist('${esc(pl.id)}')">
                        <div class="sidebar-row-icon" id="sidebar-icon-pl-${pl.id}">${pl.tracks[0] && pl.tracks[0].thumbnail ? `<img src="${srcUrl(pl.tracks[0].thumbnail)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:6px">` : `<i class="ti ti-playlist"></i>`}</div>
                        <div class="sidebar-row-text">
                            <div class="sidebar-row-name" id="sidebar-name-pl-${pl.id}">${esc(pl.name)}</div>
                            <div class="sidebar-row-meta">${pl.tracks.length} song${pl.tracks.length === 1 ? '' : 's'}</div>
                        </div>
                        <button class="sidebar-row-menu" onclick="event.stopPropagation();showPlaylistMenu(event,'${esc(pl.id)}')" title="Options"><i class="ti ti-dots-vertical"></i></button>
                        <div class="sidebar-row-underline" id="sidebar-underline-pl-${pl.id}"></div>
                    </div>`).join('');
            }
            const libView = document.getElementById('library-view');
            const sec = libView ? libView.dataset.section : '';
            if (sec.startsWith('pl:')) setActiveLibraryIcon('pl:' + sec.slice(3));
        }

        function setPanelContext(context) {
            panelContext = { ...panelContext, ...context };
            renderNowPlaying();
        }

        function panelQueueTracks() {
            if (!queue.length || queueIndex < 0) return [];
            const after = queue.slice(queueIndex + 1);
            const before = queue.slice(0, queueIndex);
            return [...after, ...before];
        }

        function renderNowPlaying() {
            const active = queueIndex >= 0 && queue[queueIndex] ? queue[queueIndex] : null;
            const context = panelContext || {};
            const isLiked = context.kind === 'liked';
            const display = (context.kind === 'track' || !context.title) && active ? active : context;

            const artImg = document.getElementById('np-art-img');
            const artIcon = document.getElementById('np-art-icon');
            if (display.thumbnail) {
                artImg.src = srcUrl(hiResThumb(display.thumbnail));
                artImg.style.display = '';
                artIcon.style.display = 'none';
            } else {
                artImg.style.display = 'none';
                artIcon.style.display = '';
            }

            document.getElementById('np-track-name').textContent = display.title || 'Nothing playing';
            document.getElementById('np-hint').textContent = display.subtitle || (isLiked ? `${getLiked().length} saved songs` : 'Pick a song to start listening');

            updateLikeBtn();

            const nextTracks = panelQueueTracks();
            const shown = queueExpanded ? nextTracks : nextTracks.slice(0, 4);
            const listEl = document.getElementById('np-queue-list');
            const label = document.querySelector('.np-queue-label');
            const linkBtn = document.querySelector('.np-queue-link');

            if (label) label.textContent = queueExpanded ? 'Queue' : 'Next in queue';
            if (linkBtn) linkBtn.textContent = queueExpanded ? 'Close' : 'Open queue';

            if (shown.length) {
                listEl.innerHTML = shown.map(track => {
                    const i = queue.indexOf(track);
                    return `<div class="np-queue-item ${i === queueIndex ? 'current' : ''}" onclick="playQueueItem(${i})">
                        <img class="np-q-thumb" src="${srcUrl(track.thumbnail)}" alt="">
                        <div class="np-q-info">
                            <div class="np-q-title">${esc(track.title)}</div>
                            <div class="np-q-artist">${esc(track.subtitle)}</div>
                        </div>
                    </div>`;
                }).join('');
            } else {
                listEl.innerHTML = '<div class="np-queue-empty">Your next songs will appear here.</div>';
            }

            const moreEl = document.querySelector('.np-queue-more');
            if (moreEl) moreEl.remove();
            if (!queueExpanded && nextTracks.length > shown.length) {
                const more = document.createElement('div');
                more.className = 'np-queue-more';
                more.textContent = `+ ${nextTracks.length - shown.length} more`;
                listEl.after(more);
            }
        }

        function toggleQueueExpand() {
            queueExpanded = !queueExpanded;
            renderNowPlaying();
        }

        function playQueueItem(index) {
            if (index >= 0 && index < queue.length) { queueIndex = index; playCurrent(); }
        }

        const _controllers = {};
        function abortAndNew(slot) {
            if (_controllers[slot]) { try { _controllers[slot].abort(); } catch(e) {} }
            _controllers[slot] = new AbortController();
            return _controllers[slot];
        }
        async function afetch(url, slot, opts = {}) {
            const ctrl = abortAndNew(slot);
            return await fetch(url, { ...opts, signal: ctrl.signal });
        }

        const LIKED_KEY = 'musify_liked_songs';
        function getLiked() { try { return JSON.parse(localStorage.getItem(LIKED_KEY) || '[]'); } catch(e) { return []; } }
        function saveLiked(arr) { localStorage.setItem(LIKED_KEY, JSON.stringify(arr)); }
        function isLiked(videoId) { return getLiked().some(t => t.videoId === videoId); }

        const RECENT_KEY = 'musify_recently_played';
        function getStoredRecent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch(e) { return []; } }
        function saveStoredRecent(arr) { try { localStorage.setItem(RECENT_KEY, JSON.stringify(arr)); } catch(e) {} }

        function toggleLike(track, btn) {
            let liked = getLiked();
            const wasLiked = liked.some(t => t.videoId === track.videoId);
            if (wasLiked) {
                liked = liked.filter(t => t.videoId !== track.videoId);
                if (btn) { btn.classList.remove('liked'); }
            } else {
                liked.unshift(track);
                if (btn) { btn.classList.add('liked'); }
            }
            saveLiked(liked);
            updateLikeBtn();
            refreshSidebarLibrary();
            renderNowPlaying();
            const libView = document.getElementById('library-view');
            if (libView && libView.style.display !== 'none' && libView.dataset.section === 'Liked Songs') {
                loadLibrary('Liked Songs');
            }
        }

        function toggleLikeById(videoId, btn) {
            const track = getLiked().find(t => t.videoId === videoId);
            if (!track) return;
            toggleLike(track, btn);
        }

        function likeCurrentTrack() {
            if (queueIndex < 0 || queueIndex >= queue.length) return;
            toggleLike(queue[queueIndex], document.getElementById('np-like-btn'));
        }

        function updateLikeBtn() {
            const btn = document.getElementById('np-like-btn');
            if (!btn) return;
            if (queueIndex >= 0 && queueIndex < queue.length) {
                btn.classList.toggle('liked', isLiked(queue[queueIndex].videoId));
            } else {
                btn.classList.remove('liked');
            }
        }

        function esc(s) {
            if (!s) return '';
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }
        function srcUrl(s) {
            if (!s) return '';
            return String(s).replace(/"/g, '%22').replace(/</g, '%3C').replace(/>/g, '%3E');
        }
        function hiResThumb(url, size = 544) {
            if (!url) return url;
            // YouTube/Google thumbnail CDN encodes requested size as =w###-h### in
            // the URL itself — bump it up so cards that render large (like the
            // spotlight cards) aren't stretching a tiny thumbnail and look blurry.
            return String(url).replace(/=w\d+-h\d+/, `=w${size}-h${size}`);
        }

        window.onload = () => {
            sessionRecentlyPlayed = getStoredRecent();
            initRouter();
            setVolume(100);
            refreshSidebarLibrary();
            renderNowPlaying();
        };

        function initRouter() {
            window.onpopstate = (e) => {
                const state = e.state || { view: 'home' };
                navigate(state.view, state.id, false);
            };
            const params = new URLSearchParams(window.location.search);
            const view = params.get('view') || 'home';
            const id = params.get('id');
            navigate(view, id, false);
        }

        function navigate(view, id = null, push = true) {
            const version = ++navigationVersion;
            if (push) {
                try {
                    const url = new URL(window.location);
                    url.searchParams.set('view', view);
                    if (id) url.searchParams.set('id', id);
                    else url.searchParams.delete('id');
                    window.history.pushState({ view, id }, '', url);
                } catch(e) {}
            }
            if (view === 'home') loadHome(version);
            else if (view === 'explore') loadExplore(version);
            else if (view === 'search') focusSearch(version);
            else if (view === 'artist') loadArtist(id, version);
            else if (view === 'album') loadAlbum(id, version);
            else if (view === 'playlist') loadPlaylist(id, version);
            else if (view === 'library') loadLibrary(id);
        }

        function showView(id) {
            hidePlaylistMenu();
            ['home-view', 'explore-view', 'search-view', 'library-view', 'detail-view'].forEach(v => {
                const el = document.getElementById(v);
                if (el) el.classList.toggle('active', v === id);
            });
            const filterTabs = document.getElementById('filter-tabs');
            if (filterTabs) filterTabs.style.display = (id === 'library-view') ? 'none' : '';
            document.querySelectorAll('.nav-link, .tab-link').forEach(l => l.classList.remove('active'));
            const map = { 'home-view': ['nav-home', 'tab-home'], 'search-view': ['nav-search', 'tab-search'], 'explore-view': ['nav-explore', 'tab-explore'] };
            if (map[id]) map[id].forEach(cid => document.getElementById(cid)?.classList.add('active'));
            if (id !== 'library-view') setActiveLibraryIcon(null);
            if (id === 'home-view' || id === 'explore-view' || id === 'search-view') {
                const active = queueIndex >= 0 ? queue[queueIndex] : null;
                setPanelContext(active ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' } : { kind: 'home', title: 'Musify', subtitle: 'Pick a song to start listening', thumbnail: '' });
            }
        }

        function setActiveLibraryIcon(key) {
            ['liked'].forEach(k => {
                const el = document.getElementById(`sidebar-icon-${k}`);
                if (el) el.classList.toggle('active', k === key);
                const line = document.getElementById(`sidebar-underline-${k}`);
                if (line) line.classList.toggle('active', k === key);
            });
            document.querySelectorAll('[id^="sidebar-icon-pl-"], [id^="sidebar-underline-pl-"]').forEach(el => el.classList.remove('active'));
            if (key && key.startsWith('pl:')) {
                const plId = key.slice(3);
                const icon = document.getElementById('sidebar-icon-pl-' + plId);
                const line = document.getElementById('sidebar-underline-pl-' + plId);
                if (icon) icon.classList.add('active');
                if (line) line.classList.add('active');
            }
        }

        async function loadHome(version = navigationVersion) {
            showView('home-view');
            const container = document.getElementById('home-view');
            container.innerHTML = renderSkeletons(4);
            try {
                const res = await afetch(`${API_BASE}/api/home`, 'home');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion) return;
                homeSectionData = data.sections || [];
                if (homeSectionData.length === 0) {
                    container.innerHTML = '<div class="placeholder"><i class="ti ti-music"></i><h2>Nothing to show</h2><p>The home feed returned no sections.</p></div>';
                } else {
                    container.innerHTML = renderQuickAccess() + renderFeaturedRow() + renderShelves(homeSectionData, 'home', true);
                }
            } catch(e) {
                if (e.name === 'AbortError') return;
                container.innerHTML = `<div class="placeholder"><i class="ti ti-alert-triangle"></i><h2>Error loading home</h2><p>${esc(e.message)}</p><button class="detail-play" style="margin-top:16px" onclick="loadHome()">Retry</button></div>`;
            }
        }

        function renderQuickAccess() {
            const liked = getLiked();
            return `<div class="quick-grid">
                <div class="quick-card" onclick="navigate('library','Liked Songs')"><i class="ti ti-heart"></i><span>Liked Songs</span></div>
                <div class="quick-card" onclick="navigate('search')"><i class="ti ti-search"></i><span>Search your favourites</span></div>
            </div>`;
        }

        function renderFeaturedRow() {
            if (!homeSectionData.length) return '';
            const first = homeSectionData[0];
            const spotlightItem = (first && first.items && first.items[0]) || null;

            const resumeItems = sessionRecentlyPlayed.slice(0, 3);
            const resumeHtml = resumeItems.length ? resumeItems.map(t => `
                <div class="quick-resume-item" onclick="resumeSessionTrack('${esc(t.videoId)}')">
                    <img class="quick-resume-thumb" src="${srcUrl(t.thumbnail)}" alt="">
                    <div class="quick-resume-info">
                        <div class="quick-resume-title">${esc(t.title) || 'Unknown'}</div>
                        <div class="quick-resume-sub">${esc(t.subtitle) || ''}</div>
                    </div>
                    <div class="quick-resume-play"><i class="ti ti-player-play-filled"></i></div>
                </div>`).join('') : '<div class="quick-resume-empty">Songs you play will show up here so you can pick right back up.</div>';

            const moods = [
                { title: 'FOCUS', sub: 'INSTRUMENTAL', color: '#2f5fa8', icon: 'ti-brain', query: 'focus instrumental study music' },
                { title: 'LATE NIGHT', sub: 'VIBES', color: '#2b2b2b', icon: 'ti-moon-stars', query: 'late night vibes chill songs' },
                { title: 'CHILL', sub: 'ACOUSTIC', color: '#c17a2e', icon: 'ti-guitar-pick', query: 'chill acoustic songs' },
                { title: 'ENERGY', sub: 'BOOST', color: '#3a3a3a', icon: 'ti-bolt', query: 'energy boost workout music' },
            ];
            const moodHtml = moods.map(m => `
                <div class="mood-tile" style="background:${m.color}" onclick="playMood('${esc(m.query)}','${esc(m.title + ' ' + m.sub)}')">
                    <i class="ti ${m.icon} mood-tile-icon"></i>
                    <div class="mood-tile-title">${esc(m.title)}</div>
                    <div class="mood-tile-sub">${esc(m.sub)}</div>
                </div>`).join('');

            const spotlightHtml = spotlightItem ? `
                <div class="spotlight spotlight-single" style="${spotlightItem.thumbnail ? `background-image:url('${srcUrl(hiResThumb(spotlightItem.thumbnail))}')` : ''}" onclick="handleCardClick('home', 0, 0)">
                    <div class="spotlight-overlay"></div>
                    ${!spotlightItem.thumbnail ? `<div class="spotlight-icon"><i class="ti ti-music"></i></div>` : ''}
                    <div class="spotlight-content">
                        <div class="spotlight-badge">${esc(spotlightItem.badge) || 'Featured'}</div>
                        <div class="spotlight-bottom">
                            <div class="spotlight-title">${esc(spotlightItem.title) || 'Unknown'}</div>
                            <div class="spotlight-sub">${esc(spotlightItem.subtitle) || ''}</div>
                            <button class="spotlight-cta" onclick="event.stopPropagation();playFromShelf('home', 0, 0)"><i class="ti ti-player-play"></i> Listen now</button>
                        </div>
                    </div>
                </div>` : '<div class="quick-resume-empty" style="height:210px">Nothing to feature yet.</div>';

            return `<div class="section">
                <div class="featured-row">
                    <div class="featured-col">
                        <div class="quick-resume-card">${resumeHtml}</div>
                        <div class="featured-col-label">Quick Resume</div>
                    </div>
                    <div class="featured-col" style="flex:1;min-width:320px">
                        ${spotlightHtml}
                        <div class="featured-col-label">Featured Spotlight</div>
                    </div>
                    <div class="featured-col">
                        <div class="mood-grid">${moodHtml}</div>
                        <div class="featured-col-label">Quick Mood Mixes</div>
                    </div>
                </div>
            </div>`;
        }

        function resumeSessionTrack(videoId) {
            const track = sessionRecentlyPlayed.find(t => t.videoId === videoId);
            if (!track) return;
            const idxInQueue = queue.findIndex(t => t.videoId === videoId);
            if (idxInQueue >= 0) {
                queueIndex = idxInQueue;
            } else {
                queue = sessionRecentlyPlayed.slice();
                queueIndex = queue.findIndex(t => t.videoId === videoId);
            }
            if (queueIndex === -1) return;
            playbackFailures.clear();
            playCurrent();
        }

        async function playMood(query, label) {
            showToast(`Finding ${label} mix\u2026`);
            try {
                const res = await afetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}&filter=songs`, 'mood');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                const results = (data.results || []).filter(i => i.videoId);
                if (!results.length) { showToast('No tracks found for this mix', true); return; }
                queue = results;
                queueIndex = 0;
                playbackFailures.clear();
                setPanelContext({ kind: 'collection', title: `${label} Mix`, subtitle: `${results.length} songs`, thumbnail: results[0].thumbnail || '', tracks: results });
                playCurrent();
                showToast(`Playing ${label} mix`);
            } catch(e) {
                if (e.name !== 'AbortError') showToast('Failed to load mix: ' + e.message, true);
            }
        }

        async function loadExplore(version = navigationVersion) {
            showView('explore-view');
            const container = document.getElementById('explore-view');
            container.innerHTML = renderSkeletons(4);
            try {
                const res = await afetch(`${API_BASE}/api/explore`, 'explore');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion) return;
                exploreSectionData = data.sections || [];
                container.innerHTML = renderShelves(exploreSectionData, 'explore');
            } catch(e) { if (e.name !== 'AbortError') showToast("Failed to load explore: " + e.message, true); }
        }

        function renderShelves(sections, viewKey = 'home', skipFirst = false) {
            if (!sections || !Array.isArray(sections)) return '';
            const startIdx = skipFirst ? 1 : 0;
            try {
                return sections.slice(startIdx).map((s, i) => {
                    const sIdx = startIdx + i;
                    if (!s || !s.items) return '';
                    const rowId = `shelf-row-${viewKey}-${sIdx}`;
                    return `
                    <div class="section">
                        <div class="section-header">
                            <span class="section-title">${esc(s.title) || 'Recommended'}</span>
                        </div>
                        <div class="card-row" id="${rowId}">
                            ${s.items.map((item, i) => {
                                if (!item) return '';
                                return `
                                <div class="card" onclick="handleCardClick('${viewKey}', ${sIdx}, ${i})">
                                    <div class="card-thumb">
                                        <img src="${srcUrl(item.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display=''">
                                        <i class="ti ti-music card-thumb-icon" style="display:none"></i>
                                    </div>
                                    <div class="card-title">${esc(item.title) || 'Unknown'}</div>
                                    <div class="card-sub">${esc(item.subtitle) || ''}</div>
                                </div>`;
                            }).join('')}
                        </div>
                    </div>`;
                }).join('');
            } catch (err) {
                return '';
            }
        }

        function renderSkeletons(count) {
            return Array(count).fill(`<div class="section"><div class="skeleton" style="width:160px;height:18px;margin-bottom:12px"></div><div class="card-row">${Array(6).fill(`<div class="card"><div class="skeleton" style="width:145px;height:145px;border-radius:6px;margin-bottom:7px"></div><div class="skeleton" style="width:80px;height:10px;margin-bottom:4px"></div><div class="skeleton" style="width:50px;height:8px"></div></div>`).join('')}</div></div>`).join('');
        }

        function loadLibrary(title) {
            showView('library-view');
            const container = document.getElementById('library-view');
            container.dataset.section = title;
            document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
            const navDownloads = document.getElementById('nav-downloads');
            if (title === 'Downloads' && navDownloads) navDownloads.classList.add('active');
            const iconKey = title === 'Liked Songs' ? 'liked' : null;
            setActiveLibraryIcon(iconKey);

            // If a track is actively playing, the Now Playing panel should always
            // keep showing that track's own art/title — never the browsed section's
            // placeholder — so the cover doesn't vanish just because a library tab
            // was opened.
            const active = queueIndex >= 0 ? queue[queueIndex] : null;

            if (title === 'Liked Songs') {
                const liked = getLiked();
                setPanelContext(active
                    ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' }
                    : { kind: 'liked', title: 'Liked Songs', subtitle: `${liked.length} saved song${liked.length === 1 ? '' : 's'}`, thumbnail: '', tracks: liked });
                if (liked.length === 0) {
                    container.innerHTML = '<div class="placeholder"><i class="ti ti-heart"></i><h2>Liked Songs</h2><p>Songs you like will appear here.<br>Tap the heart on any track to save it.</p></div>';
                    return;
                }
                container.innerHTML = `
                    <div class="detail-header">
                        <div class="detail-art" style="display:flex;align-items:center;justify-content:center;background:var(--accent)">
                            <i class="ti ti-heart" style="font-size:60px;color:#fff"></i>
                        </div>
                        <div class="detail-meta">
                            <div class="detail-type">Playlist</div>
                            <div class="detail-name">Liked Songs</div>
                            <div class="detail-sub">${liked.length} song${liked.length !== 1 ? 's' : ''}</div>
                            <button class="detail-play" onclick="playFromLiked(0)">Play</button>
                        </div>
                    </div>
                    <div class="track-list">
                        ${liked.map((t, idx) => `
                        <div class="list-item" onclick="playFromLiked(${idx})">
                            <div class="list-num">${idx + 1}</div>
                            <img src="${srcUrl(t.thumbnail)}" class="list-img" alt="">
                            <div class="list-info">
                                <div class="list-title">${esc(t.title)}</div>
                                <div class="list-sub">${esc(t.subtitle) || ''}</div>
                            </div>
                            <button class="list-like liked" onclick="event.stopPropagation();toggleLikeById('${esc(t.videoId)}', this)" title="Unlike"><i class="ti ti-heart-filled"></i></button>
                        </div>`).join('')}
                    </div>`;
                likedTracks = liked;
            } else if (title === 'Playlists') {
                setPanelContext(active
                    ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' }
                    : { kind: 'library', title, subtitle: '', thumbnail: '' });
                renderPlaylistsList();
            } else if (title === 'Artists') {
                setPanelContext(active
                    ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' }
                    : { kind: 'library', title, subtitle: '', thumbnail: '' });
                renderArtistsList();
            } else if (title === 'Albums') {
                setPanelContext(active
                    ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' }
                    : { kind: 'library', title, subtitle: '', thumbnail: '' });
                container.innerHTML = '<div class="placeholder"><i class="ti ti-album"></i><h2>Albums</h2><p>Albums you save will appear here.</p></div>';
            } else {
                setPanelContext(active
                    ? { kind: 'track', title: active.title, subtitle: active.subtitle, thumbnail: active.thumbnail || '' }
                    : { kind: 'library', title, subtitle: '', thumbnail: '' });
                container.innerHTML = `<div class="placeholder"><i class="ti ti-download"></i><h2>${esc(title)}</h2><p>Coming soon! Your ${esc(title.toLowerCase())} will appear here.</p></div>`;
            }
        }

        const PLAYLISTS_KEY = 'musify_playlists';
        function getPlaylists() { try { return JSON.parse(localStorage.getItem(PLAYLISTS_KEY) || '[]'); } catch(e) { return []; } }
        function savePlaylists(arr) { localStorage.setItem(PLAYLISTS_KEY, JSON.stringify(arr)); }

        function setLibraryPill(label) {
            document.querySelectorAll('.sidebar-pill').forEach(p => p.classList.toggle('active', p.textContent.trim() === label));
        }

        function nextPlaylistName() {
            const names = getPlaylists().map(p => p.name);
            if (!names.includes('New Playlist')) return 'New Playlist';
            let n = 1;
            while (names.includes(`New Playlist ${n}`)) n++;
            return `New Playlist ${n}`;
        }

        function createPlaylist() {
            const playlists = getPlaylists();
            const id = 'pl_' + Date.now().toString(36);
            playlists.push({ id, name: nextPlaylistName(), tracks: [] });
            savePlaylists(playlists);
            refreshSidebarLibrary();
            const libView = document.getElementById('library-view');
            if (libView && libView.dataset.section === 'Playlists') renderPlaylistsList();
            showToast('New playlist — type a name');
            startRename(id);
        }

        function startRename(id) {
            const el = document.getElementById('sidebar-name-pl-' + id);
            if (!el) return;
            const original = el.textContent;
            el.contentEditable = 'true';
            el.classList.add('renaming');
            el.scrollIntoView({ block: 'nearest' });
            el.addEventListener('mousedown', e => e.stopPropagation());
            el.addEventListener('click', e => e.stopPropagation());
            el.focus();
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            el.onblur = () => commitRename(id, el, original);
            el.onkeydown = e => {
                if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
                else if (e.key === 'Escape') { el.textContent = original; el.blur(); }
            };
        }

        function commitRename(id, el, original) {
            el.contentEditable = 'false';
            el.classList.remove('renaming');
            const name = el.textContent.trim() || original;
            el.textContent = name;
            const playlists = getPlaylists();
            const pl = playlists.find(p => p.id === id);
            if (pl && pl.name !== name) {
                pl.name = name;
                savePlaylists(playlists);
            }
            refreshSidebarLibrary();
            const libView = document.getElementById('library-view');
            if (libView && libView.dataset.section === 'Playlists') renderPlaylistsList();
            else if (libView && libView.dataset.section === 'pl:' + id) openLocalPlaylist(id);
            showToast(`Playlist "${name}"`);
        }

        function deletePlaylist(id) {
            savePlaylists(getPlaylists().filter(p => p.id !== id));
            refreshSidebarLibrary();
            const libView = document.getElementById('library-view');
            if (libView && libView.dataset.section === 'pl:' + id) {
                loadLibrary('Playlists');
                setLibraryPill('Playlists');
            } else if (libView && libView.dataset.section === 'Playlists') {
                renderPlaylistsList();
            }
            showToast('Playlist deleted');
        }

        function deletePlaylistConfirm(id, btn) {
            if (!btn || btn.dataset.armed !== '1') {
                if (btn) { btn.dataset.armed = '1'; btn.classList.add('armed'); }
                showToast('Click again to confirm delete');
                return;
            }
            deletePlaylist(id);
        }

        let _menuPlaylistId = null;
        function showPlaylistMenu(e, id) {
            e.stopPropagation();
            const pl = getPlaylists().find(p => p.id === id);
            if (!pl) return;
            _menuPlaylistId = id;
            const menu = document.getElementById('playlist-menu');
            menu.innerHTML = `
                <div class="popup-key">${esc(pl.name)}</div>
                <div class="popup-item" onclick="hidePlaylistMenu();openLocalPlaylist('${esc(id)}')"><i class="ti ti-folder-open"></i>Open</div>
                <div class="popup-item" onclick="hidePlaylistMenu();renamePlaylist('${esc(id)}')"><i class="ti ti-pencil"></i>Rename</div>
                <div class="popup-item" onclick="hidePlaylistMenu();addToPlaylist('${esc(id)}')"><i class="ti ti-plus"></i>Add current song</div>
                <div class="popup-sep"></div>
                <div class="popup-item danger" id="menu-delete" onclick="menuDeletePlaylist(this)"><i class="ti ti-trash"></i><span>Delete playlist</span></div>`;
            menu.style.display = 'block';
            const mw = menu.offsetWidth;
            const mh = menu.offsetHeight;
            let x = e.clientX;
            let y = e.clientY;
            if (x + mw > window.innerWidth - 8) x = window.innerWidth - mw - 8;
            if (y + mh > window.innerHeight - 8) y = window.innerHeight - mh - 8;
            menu.style.left = x + 'px';
            menu.style.top = y + 'px';
        }

        function hidePlaylistMenu() {
            const menu = document.getElementById('playlist-menu');
            if (menu) menu.style.display = 'none';
            _menuPlaylistId = null;
        }

        function renamePlaylist(id) {
            hidePlaylistMenu();
            startRename(id);
        }

        function menuDeletePlaylist(el) {
            if (el.dataset.armed !== '1') {
                el.dataset.armed = '1';
                el.querySelector('span').textContent = 'Click again to confirm';
                return;
            }
            const id = _menuPlaylistId;
            hidePlaylistMenu();
            deletePlaylist(id);
        }

        function addToPlaylist(id) {
            if (queueIndex < 0 || queueIndex >= queue.length) return showToast('Nothing playing', true);
            const playlists = getPlaylists();
            const pl = playlists.find(p => p.id === id);
            if (!pl) return;
            const t = queue[queueIndex];
            if (pl.tracks.some(x => x.videoId === t.videoId)) return showToast('Already in playlist');
            pl.tracks.push({ videoId: t.videoId, title: t.title, subtitle: t.subtitle, thumbnail: t.thumbnail || '' });
            savePlaylists(playlists);
            refreshSidebarLibrary();
            showToast(`Added to "${pl.name}"`);
            const libView = document.getElementById('library-view');
            if (libView && libView.dataset.section === 'pl:' + id) openLocalPlaylist(id);
            else if (libView && libView.dataset.section === 'Playlists') renderPlaylistsList();
        }

        function removeFromPlaylist(id, videoId) {
            const playlists = getPlaylists();
            const pl = playlists.find(p => p.id === id);
            if (pl) {
                pl.tracks = pl.tracks.filter(t => t.videoId !== videoId);
                savePlaylists(playlists);
                refreshSidebarLibrary();
            }
            openLocalPlaylist(id);
        }

        function playLocalPlaylist(id) {
            const pl = getPlaylists().find(p => p.id === id);
            const tracks = (pl ? pl.tracks : []).filter(t => t.videoId);
            if (!tracks.length) return showToast('Playlist is empty', true);
            queue = tracks;
            queueIndex = 0;
            playbackFailures.clear();
            setPanelContext({ kind: 'collection', title: pl.name, subtitle: `${tracks.length} songs`, thumbnail: '', tracks });
            playCurrent();
        }

        function renderPlaylistsList() {
            const container = document.getElementById('library-view');
            const playlists = getPlaylists();
            if (!playlists.length) {
                container.innerHTML = '<div class="placeholder"><i class="ti ti-playlist"></i><h2>Playlists</h2><p>No playlists yet.<br>Create one from the + button in your library.</p></div>';
                return;
            }
            container.innerHTML = `
                <div class="detail-header">
                    <div class="detail-art" style="display:flex;align-items:center;justify-content:center;background:var(--surface)">
                        <i class="ti ti-playlist" style="font-size:60px;color:var(--text-ghost)"></i>
                    </div>
                    <div class="detail-meta">
                        <div class="detail-type">Your Library</div>
                        <div class="detail-name">Playlists</div>
                        <div class="detail-sub">${playlists.length} playlist${playlists.length === 1 ? '' : 's'}</div>
                        <button class="detail-play" onclick="createPlaylist()">New playlist</button>
                    </div>
                </div>
                <div class="track-list">
                    ${playlists.map(pl => `
                    <div class="list-item" onclick="openLocalPlaylist('${esc(pl.id)}')">
                        ${pl.tracks[0] && pl.tracks[0].thumbnail ? `<img src="${srcUrl(pl.tracks[0].thumbnail)}" class="list-img" alt="">` : `<div class="list-num"><i class="ti ti-playlist"></i></div>`}
                        <div class="list-info">
                            <div class="list-title">${esc(pl.name)}</div>
                            <div class="list-sub">${pl.tracks.length} song${pl.tracks.length === 1 ? '' : 's'}</div>
                        </div>
                        <button class="list-like" onclick="event.stopPropagation();playLocalPlaylist('${esc(pl.id)}')" title="Play"><i class="ti ti-player-play"></i></button>
                        <button class="list-like" onclick="event.stopPropagation();addToPlaylist('${esc(pl.id)}')" title="Add current song"><i class="ti ti-plus"></i></button>
                        <button class="list-like" onclick="event.stopPropagation();deletePlaylistConfirm('${esc(pl.id)}', this)" title="Delete"><i class="ti ti-trash"></i></button>
                    </div>`).join('')}
                </div>`;
        }

        function renderArtistsList() {
            const container = document.getElementById('library-view');
            const groups = {};
            getLiked().forEach(t => {
                const a = (t.subtitle || '').trim() || 'Unknown artist';
                (groups[a] = groups[a] || []).push(t);
            });
            const artists = Object.keys(groups).sort((a, b) => a.localeCompare(b));
            if (!artists.length) {
                container.innerHTML = '<div class="placeholder"><i class="ti ti-users"></i><h2>Artists</h2><p>Artists from your liked songs will appear here.</p></div>';
                return;
            }
            container.innerHTML = `
                <div class="detail-header">
                    <div class="detail-art artist-art" style="display:flex;align-items:center;justify-content:center;background:var(--surface)">
                        <i class="ti ti-users" style="font-size:60px;color:var(--text-ghost)"></i>
                    </div>
                    <div class="detail-meta">
                        <div class="detail-type">Your Library</div>
                        <div class="detail-name">Artists</div>
                        <div class="detail-sub">${artists.length} artist${artists.length === 1 ? '' : 's'} from liked songs</div>
                    </div>
                </div>
                <div class="track-list">
                    ${artists.map(a => `
                    <div class="list-item" onclick="openArtistFrom(this)" data-artist="${esc(a)}">
                        <div class="list-num"><i class="ti ti-user"></i></div>
                        <div class="list-info">
                            <div class="list-title">${esc(a)}</div>
                            <div class="list-sub">${groups[a].length} song${groups[a].length === 1 ? '' : 's'}</div>
                        </div>
                    </div>`).join('')}
                </div>`;
        }

        function openArtistFrom(el) {
            openArtist(el.dataset.artist || '');
        }

        function openArtist(name) {
            const liked = getLiked().filter(t => (t.subtitle || '').trim() === name);
            if (!liked.length) return;
            showView('library-view');
            const container = document.getElementById('library-view');
            container.dataset.section = 'artist:' + name;
            setActiveLibraryIcon(null);
            currentViewTracks = liked.filter(t => t.videoId);
            container.innerHTML = `
                <div class="detail-header">
                    <div class="detail-art artist-art" style="display:flex;align-items:center;justify-content:center;background:var(--surface)">
                        <i class="ti ti-user" style="font-size:60px;color:var(--text-ghost)"></i>
                    </div>
                    <div class="detail-meta">
                        <div class="detail-type">Artist</div>
                        <div class="detail-name">${esc(name)}</div>
                        <div class="detail-sub">${currentViewTracks.length} song${currentViewTracks.length === 1 ? '' : 's'}</div>
                        <button class="detail-play" onclick="playFromCurrentView(0)">Play</button>
                    </div>
                </div>
                <div class="track-list">
                    ${currentViewTracks.map((t, idx) => `
                    <div class="list-item" onclick="playFromCurrentView(${idx})">
                        <div class="list-num">${idx + 1}</div>
                        <img src="${srcUrl(t.thumbnail)}" class="list-img" alt="">
                        <div class="list-info">
                            <div class="list-title">${esc(t.title)}</div>
                            <div class="list-sub">${esc(t.subtitle) || ''}</div>
                        </div>
                    </div>`).join('')}
                </div>`;
        }

        function openLocalPlaylist(id) {
            const pl = getPlaylists().find(p => p.id === id);
            if (!pl) return;
            showView('library-view');
            const container = document.getElementById('library-view');
            container.dataset.section = 'pl:' + id;
            document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
            setActiveLibraryIcon('pl:' + id);
            currentViewTracks = pl.tracks.filter(t => t.videoId);
            if (!currentViewTracks.length) {
                container.innerHTML = `<div class="placeholder"><i class="ti ti-playlist"></i><h2>${esc(pl.name)}</h2><p>No songs yet.<br>Play a song, then use the + button on the Playlists page to add it.</p></div>`;
                return;
            }
            container.innerHTML = `
                <div class="detail-header">
                    ${pl.tracks[0] && pl.tracks[0].thumbnail
                        ? `<img src="${srcUrl(pl.tracks[0].thumbnail)}" class="detail-art" alt="">`
                        : `<div class="detail-art" style="display:flex;align-items:center;justify-content:center;background:var(--surface)"><i class="ti ti-playlist" style="font-size:60px;color:var(--text-ghost)"></i></div>`}
                    <div class="detail-meta">
                        <div class="detail-type">Playlist</div>
                        <div class="detail-name">${esc(pl.name)}</div>
                        <div class="detail-sub">${currentViewTracks.length} song${currentViewTracks.length === 1 ? '' : 's'}</div>
                        <button class="detail-play" onclick="playLocalPlaylist('${esc(pl.id)}')">Play</button>
                        <button class="detail-play" style="background:none;border:0.5px solid var(--border);color:var(--text-secondary)" onclick="loadLibrary('Playlists');setLibraryPill('Playlists')">Back to playlists</button>
                    </div>
                </div>
                <div class="track-list">
                    ${currentViewTracks.map((t, idx) => `
                    <div class="list-item" onclick="playFromCurrentView(${idx})">
                        <div class="list-num">${idx + 1}</div>
                        <img src="${srcUrl(t.thumbnail)}" class="list-img" alt="">
                        <div class="list-info">
                            <div class="list-title">${esc(t.title)}</div>
                            <div class="list-sub">${esc(t.subtitle) || ''}</div>
                        </div>
                        <button class="list-like" onclick="event.stopPropagation();removeFromPlaylist('${esc(pl.id)}','${esc(t.videoId)}')" title="Remove"><i class="ti ti-x"></i></button>
                    </div>`).join('')}
                </div>`;
        }

        function playFromLiked(idx) {
            const fresh = getLiked();
            if (!fresh.length) return;
            queue = fresh;
            queueIndex = Math.min(idx, fresh.length - 1);
            playbackFailures.clear();
            playCurrent();
        }

        function focusSearch(version = null) {
            if (version === null) { navigate('search'); return; }
            showView('search-view');
            searchInput.focus();
            setTimeout(() => searchInput.focus(), 50);
        }

        function setFilter(filter, el) {
            currentFilter = filter;
            document.querySelectorAll('.filter-tab').forEach(c => c.classList.remove('active'));
            el.classList.add('active');
            if (searchInput.value.trim()) runSearch();
        }

        searchInput.onmousedown = e => e.stopPropagation();
        searchInput.oninput = e => {
            clearTimeout(debounceTimer);
            const q = e.target.value.trim();
            if (!q) return document.getElementById('suggestions').style.display = 'none';
            debounceTimer = setTimeout(async () => {
                try {
                    const res = await afetch(`${API_BASE}/api/suggestions?q=${encodeURIComponent(q)}`, 'suggestions');
                    const data = await res.json();
                    const div = document.getElementById('suggestions');
                    if (!data.suggestions || !data.suggestions.length) return div.style.display = 'none';
                    div.innerHTML = data.suggestions.map(s =>
                        `<div class="suggestion-item" onclick="doSearchFrom(this)" data-q="${esc(s)}"><i class="ti ti-search"></i>${esc(s)}</div>`
                    ).join('');
                    div.style.display = 'block';
                } catch(err) { if (err.name !== 'AbortError') {} }
            }, 250);
        };
        searchInput.onkeydown = e => { if (e.key === 'Enter') runSearch(); };

        function doSearchFrom(el) {
            doSearch(el.dataset.q || '');
        }

        function doSearch(q) {
            searchInput.value = q;
            document.getElementById('suggestions').style.display = 'none';
            runSearch();
        }

        async function runSearch() {
            const q = searchInput.value.trim();
            if (!q) return;
            showView('search-view');
            document.getElementById('suggestions').style.display = 'none';
            const container = document.getElementById('search-results');
            container.innerHTML = renderSkeletons(2);
            try {
                const res = await afetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}${currentFilter ? `&filter=${currentFilter}` : ''}`, 'search');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                lastSearchResults = data.results || [];
                container.innerHTML = lastSearchResults.length ? lastSearchResults.map((item, idx) => `
                    <div class="list-item" onclick="handleSearchClick(${idx})">
                        <img src="${srcUrl(item.thumbnail)}" class="list-img" alt="">
                        <div class="list-info">
                            <div class="list-title">${esc(item.title)}</div>
                            <div class="list-sub">${esc(item.subtitle)}</div>
                        </div>
                        <div class="list-badge">${esc(item.type)}</div>
                    </div>`).join('') : '<div class="placeholder"><i class="ti ti-search"></i><h2>No results found</h2></div>';
            } catch(e) { if (e.name !== 'AbortError') showToast("Search failed", true); }
        }

        function handleSearchClick(idx) {
            const item = lastSearchResults[idx];
            if (item.videoId) playFromList(lastSearchResults, idx);
            else if (item.type === 'artist') navigate('artist', item.browseId);
            else if (item.type === 'album') navigate('album', item.browseId);
            else if (item.type === 'playlist') navigate('playlist', item.browseId);
        }

        async function loadArtist(id, version = navigationVersion) {
            showView('detail-view');
            const container = document.getElementById('detail-view');
            container.innerHTML = '<div class="skeleton" style="width:100%;height:200px;border-radius:8px"></div>';
            try {
                const res = await afetch(`${API_BASE}/api/artist/${id}`, 'detail');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion) return;
                detailSectionData = data.sections || [];
                container.innerHTML = `
                    <div class="detail-header">
                        <img src="${srcUrl(data.thumbnail)}" class="detail-art artist-art" alt="">
                        <div class="detail-meta">
                            <div class="detail-type">Artist</div>
                            <div class="detail-name">${esc(data.title)}</div>
                        </div>
                    </div>
                    ${renderShelves(detailSectionData, 'detail')}`;
            } catch(e) { if (e.name !== 'AbortError') showToast("Failed to load artist: " + e.message, true); }
        }

        async function loadAlbum(id, version = navigationVersion) {
            showView('detail-view');
            const container = document.getElementById('detail-view');
            container.innerHTML = '<div class="skeleton" style="width:100%;height:200px;border-radius:8px"></div>';
            try {
                const res = await afetch(`${API_BASE}/api/album/${id}`, 'detail');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion) return;
                renderDetailView(data, 'Album');
            } catch(e) { if (e.name !== 'AbortError') showToast("Failed to load album: " + e.message, true); }
        }

        async function loadPlaylist(id, version = navigationVersion) {
            showView('detail-view');
            const container = document.getElementById('detail-view');
            container.innerHTML = '<div class="skeleton" style="width:100%;height:200px;border-radius:8px"></div>';
            try {
                const res = await afetch(`${API_BASE}/api/playlist/${id}`, 'playlist');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion) return;
                if (!data.title && !data.tracks?.length) {
                    container.innerHTML = '<div class="placeholder"><i class="ti ti-alert-triangle"></i><h2>Playlist unavailable</h2><p>The parser returned no data.</p></div>';
                    return;
                }
                data.browseId = id;
                renderDetailView(data, 'Playlist');
            } catch(e) {
                if (e.name !== 'AbortError') {
                    showToast("Failed to load playlist: " + e.message, true);
                    container.innerHTML = `<div class="placeholder"><i class="ti ti-alert-triangle"></i><h2>Failed to load playlist</h2><p>${esc(e.message)}</p></div>`;
                }
            }
        }

        async function loadMoreTracks() {
            const browseId = currentDetailBrowseId;
            const continuation = currentContinuation;
            const version = detailVersion;
            if (!browseId || !continuation) return;
            const btn = document.getElementById('load-more-btn');
            if (btn) { btn.textContent = 'Loading...'; btn.disabled = true; }
            try {
                const res = await afetch(`${API_BASE}/api/playlist/${browseId}/more?continuation=${encodeURIComponent(continuation)}`, 'loadmore');
                if (!res.ok) throw new Error(`Server error ${res.status}`);
                const data = await res.json();
                if (version !== navigationVersion || browseId !== currentDetailBrowseId || continuation !== currentContinuation) return;
                const newTracks = data.tracks || [];
                const startIdx = currentViewTracks.length;
                currentViewTracks = currentViewTracks.concat(newTracks);
                currentContinuation = data.continuation || null;

                const list = document.getElementById('track-list');
                if (list) {
                    list.insertAdjacentHTML('beforeend', newTracks.map((t, i) => `
                        <div class="list-item" onclick="playFromCurrentView(${startIdx + i})">
                            <div class="list-num">${startIdx + i + 1}</div>
                            <img src="${srcUrl(t.thumbnail)}" class="list-img" alt="">
                            <div class="list-info">
                                <div class="list-title">${esc(t.title)}</div>
                                <div class="list-sub">${esc(t.subtitle) || ''}</div>
                            </div>
                        </div>`).join(''));
                }

                const freshBtn = document.getElementById('load-more-btn');
                if (freshBtn) {
                    if (data.continuation) { freshBtn.textContent = 'Load more'; freshBtn.disabled = false; }
                    else { freshBtn.parentElement.remove(); }
                }
            } catch(e) {
                if (e.name === 'AbortError') return;
                if (version !== navigationVersion || browseId !== currentDetailBrowseId || continuation !== currentContinuation) return;
                showToast('Failed to load more: ' + e.message, true);
                const errBtn = document.getElementById('load-more-btn');
                if (errBtn) { errBtn.textContent = 'Load more'; errBtn.disabled = false; }
            }
        }

        function renderDetailView(data, typeLabel) {
            const container = document.getElementById('detail-view');
            currentViewTracks = data.tracks || [];
            currentDetailBrowseId = data.browseId || null;
            currentContinuation = data.continuation || null;
            detailVersion = navigationVersion;
            setPanelContext({ kind: 'collection', title: data.title || typeLabel, subtitle: data.subtitle || typeLabel, thumbnail: data.thumbnail || '', tracks: currentViewTracks });

            container.innerHTML = `
                <div class="detail-header">
                    <img src="${srcUrl(data.thumbnail)}" class="detail-art" alt="" onerror="this.style.background='var(--surface)'">
                    <div class="detail-meta">
                        <div class="detail-type">${esc(typeLabel)}</div>
                        <div class="detail-name">${esc(data.title)}</div>
                        <div class="detail-sub">${esc(data.subtitle)}</div>
                        <button class="detail-play" onclick="playFromCurrentView(0)">Play</button>
                    </div>
                </div>
                <div class="track-list" id="track-list">
                    ${(data.tracks || []).map((t, idx) => `
                        <div class="list-item" onclick="playFromCurrentView(${idx})">
                            <div class="list-num">${idx + 1}</div>
                            <img src="${srcUrl(t.thumbnail)}" class="list-img" alt="">
                            <div class="list-info">
                                <div class="list-title">${esc(t.title)}</div>
                                <div class="list-sub">${esc(t.subtitle)}</div>
                            </div>
                        </div>`).join('')}
                </div>
                ${data.continuation ? `<div style="text-align:center;margin:20px 0"><button class="detail-play" id="load-more-btn" onclick="loadMoreTracks()">Load more</button></div>` : ''}`;
        }

        function getSectionData(viewKey) {
            if (viewKey === 'explore') return exploreSectionData;
            if (viewKey === 'detail') return detailSectionData;
            return homeSectionData;
        }

        function handleCardClick(viewKey, sIdx, iIdx) {
            const data = getSectionData(viewKey);
            if (!data[sIdx] || !data[sIdx].items || !data[sIdx].items[iIdx]) return;
            const item = data[sIdx].items[iIdx];
            if (item.type === 'song' || item.videoId) playFromShelf(viewKey, sIdx, iIdx);
            else if (item.type === 'album') navigate('album', item.browseId);
            else if (item.type === 'playlist') navigate('playlist', item.browseId);
            else if (item.type === 'artist') navigate('artist', item.browseId);
            else if (item.browseId) {
                if (item.browseId.startsWith('MPRE')) navigate('album', item.browseId);
                else if (item.browseId.startsWith('VL')) navigate('playlist', item.browseId);
                else navigate('artist', item.browseId);
            }
        }

        function playFromShelf(viewKey, sIdx, iIdx) {
            const data = getSectionData(viewKey);
            const list = data[sIdx].items;
            const clickedItem = list[iIdx];
            const startItem = clickedItem.videoId ? clickedItem : list.find(i => i.videoId);
            if (!startItem) return;
            queue = list.filter(i => i.videoId);
            queueIndex = queue.findIndex(i => i.videoId === startItem.videoId);
            if (queueIndex === -1) return;
            playbackFailures.clear();
            playCurrent();
        }

        function playFromCurrentView(idx) {
            const allTracks = currentViewTracks || [];
            const clickedTrack = allTracks[idx];
            queue = allTracks.filter(t => t.videoId);
            if (!clickedTrack || !clickedTrack.videoId) queueIndex = 0;
            else queueIndex = queue.findIndex(t => t.videoId === clickedTrack.videoId);
            if (queue.length === 0 || queueIndex === -1) return;
            playbackFailures.clear();
            playCurrent();
        }

        function playFromList(list, idx) {
            queue = list.filter(i => i.videoId);
            queueIndex = queue.findIndex(i => i.videoId === list[idx].videoId);
            if (queueIndex === -1) return;
            playbackFailures.clear();
            playCurrent();
        }

        async function playCurrent() {
            if (queueIndex < 0 || queueIndex >= queue.length) return;
            const track = queue[queueIndex];

            if (!sessionRecentlyPlayed.find(t => t.videoId === track.videoId)) {
                sessionRecentlyPlayed.unshift(track);
                if (sessionRecentlyPlayed.length > 12) sessionRecentlyPlayed.pop();
                saveStoredRecent(sessionRecentlyPlayed);
            }

            document.getElementById('np-track-name').textContent = track.title;
            document.getElementById('np-hint').textContent = track.subtitle;
            const artImg = document.getElementById('np-art-img');
            const artIcon = document.getElementById('np-art-icon');
            if (track.thumbnail) {
                artImg.src = srcUrl(hiResThumb(track.thumbnail));
                artImg.style.display = '';
                artIcon.style.display = 'none';
            } else {
                artImg.style.display = 'none';
                artIcon.style.display = '';
            }

            updateLikeBtn();
            if (panelContext.kind !== 'collection' && panelContext.kind !== 'liked') {
                setPanelContext({ kind: 'track', title: track.title, subtitle: track.subtitle, thumbnail: track.thumbnail || '', tracks: [] });
            } else {
                renderNowPlaying();
            }

            try {
                audio.src = `${API_BASE}/api/stream/${track.videoId}`;
                audio.load();
                const playPromise = audio.play();
                if (playPromise !== undefined) {
                    playPromise.catch(e => { setPlayIcon('play'); });
                }
            } catch(e) {
                showToast("Error fetching stream", true);
            }
        }

        function togglePlay() {
            if (queueIndex < 0 || queueIndex >= queue.length) return;
            if (audio.paused) {
                const playPromise = audio.play();
                if (playPromise !== undefined) {
                    playPromise.catch(e => {
                        if (e.name !== 'AbortError') {}
                        setPlayIcon('play');
                    });
                }
            } else {
                audio.pause();
            }
        }

        function prev() {
            if (!queue.length || queueIndex < 0) return;
            if (!audio.error && audio.currentTime > 3) { audio.currentTime = 0; }
            else { queueIndex = (queueIndex - 1 + queue.length) % queue.length; playCurrent(); }
        }

        function next() {
            if (!queue.length || queueIndex < 0) return;
            if (shuffle && queue.length > 1) { let i = queueIndex; while (i === queueIndex) i = Math.floor(Math.random() * queue.length); queueIndex = i; }
            else queueIndex = (queueIndex + 1) % queue.length;
            playCurrent();
        }

        function toggleShuffle() { shuffle = !shuffle; document.getElementById('shuffle-btn').classList.toggle('active', shuffle); }
        function toggleRepeat() { repeat = !repeat; audio.loop = repeat; document.getElementById('repeat-btn').classList.toggle('active', repeat); }

        function seek(e) {
            if (!audio.duration || !isFinite(audio.duration)) return;
            audio.currentTime = (e.clientX - e.currentTarget.getBoundingClientRect().left) / e.currentTarget.offsetWidth * audio.duration;
        }

        let _mutedVol = 100;
        function setVolume(v) {
            audio.volume = v / 100;
            const slider = document.getElementById('np-vol-slider');
            if (slider) slider.value = v;
            const icon = document.getElementById('np-vol-icon');
            if (icon) {
                if (v == 0) icon.className = 'ti ti-volume-3';
                else if (v < 50) icon.className = 'ti ti-volume-2';
                else icon.className = 'ti ti-volume';
            }
        }

        function toggleMute() {
            const slider = document.getElementById('np-vol-slider');
            if (!slider) return;
            if (audio.volume > 0) { _mutedVol = slider.value; slider.value = 0; setVolume(0); }
            else { slider.value = _mutedVol || 100; setVolume(slider.value); }
        }

        function setPlayIcon(state) {
            const icon = document.getElementById('np-play-icon');
            if (!icon) return;
            icon.className = state === 'pause' ? 'ti ti-player-pause' : 'ti ti-player-play';
        }

        audio.onplay = () => setPlayIcon('pause');
        audio.onpause = () => setPlayIcon('play');
        audio.onended = () => !repeat && next();
        audio.ontimeupdate = () => {
            if (!audio.duration) return;
            document.getElementById('np-bar-fill').style.width = (audio.currentTime / audio.duration * 100) + '%';
            document.getElementById('np-elapsed').textContent = formatTime(audio.currentTime);
            document.getElementById('np-duration').textContent = formatTime(audio.duration);
        };
        audio.onerror = async () => {
            const err = audio.error;
            let msg = "Playback failed";
            if (err) {
                if (err.code === MediaError.MEDIA_ERR_NETWORK) msg = "Network error.";
                else if (err.code === MediaError.MEDIA_ERR_DECODE) msg = "Audio decode error.";
                else if (err.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) msg = "Stream unavailable.";
                else if (err.code === MediaError.MEDIA_ERR_ABORTED) return;
            }
            const failedTrack = queue[queueIndex];
            if (!failedTrack || !failedTrack.videoId) { showToast(msg, true); return; }
            playbackFailures.add(failedTrack.videoId);
            const availableIndexes = queue.map((track, index) => ({ track, index })).filter(({ track }) => !playbackFailures.has(track.videoId));
            if (!availableIndexes.length) { showToast(`${msg} No tracks left.`, true); return; }
            const nextTrack = shuffle ? availableIndexes[Math.floor(Math.random() * availableIndexes.length)] : availableIndexes.find(({ index }) => index > queueIndex) || availableIndexes[0];
            queueIndex = nextTrack.index;
            showToast(`${msg} Skipping to next.`, true);
            playCurrent();
        };

        function formatTime(s) { const m = Math.floor(s / 60); const sc = Math.floor(s % 60); return `${m}:${sc < 10 ? '0' : ''}${sc}`; }
        function showToast(m, e = false) { const t = document.getElementById('toast'); t.innerText = m; t.className = `toast show ${e ? 'error' : ''}`; setTimeout(() => t.classList.remove('show'), 3000); }
        window.onclick = e => {
            const menu = document.getElementById('playlist-menu');
            if (menu && menu.style.display !== 'none' && !e.target.closest('.popup-menu') && !e.target.closest('.sidebar-row-menu')) hidePlaylistMenu();
            if (!e.target.closest('#search-box')) document.getElementById('suggestions').style.display = 'none';
        };
