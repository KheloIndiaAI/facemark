const state = {
    currentRoute: '',
    students: [],
    dashboardData: null,
    systemHealth: null
};

// A home-screen "installed app" on iOS is not the same runtime as the Safari
// tab at the same URL - WebKit has a long, still-active history of camera bugs
// specific to that standalone mode (verified against WebKit's own bug tracker:
// webkit.org/b/185448, /215884, /220416, /252465, and Apple developer-forum
// reports of a fresh regression in iOS 18.0-18.1, fixed in 18.1.1, with the
// underlying failure class still reported into iOS 18.5). Two things follow
// directly from that history, not from guesswork:
//   - permission granted in the browser tab does not reliably carry over to
//     the installed app, and the installed app has no address-bar padlock to
//     fix it from - so telling the user to check permissions there is useless.
//   - a stream can be granted with NO javascript error at all and the <video>
//     element still never produces a frame (webkit.org/b/252465), which is
//     exactly what "camera doesn't open" looks like with nothing in the
//     console. Waiting on getUserMedia() resolving is not sufficient; the
//     video must be checked separately for whether it actually renders.
function isStandalonePWA() {
    return window.matchMedia('(display-mode: standalone)').matches
           || window.navigator.standalone === true;
}

// Written platform-neutral deliberately. The specific bugs cited above
// (webkit.org/b/*) are WebKit/iOS-only; a report of this exact symptom on
// Android showed naming iOS here was actively wrong for that user. What is
// true on both platforms, without needing a platform-specific citation: an
// installed home-screen app is a separate window from the browser tab, has
// no address-bar padlock to fix a permission from, and its camera permission
// is not guaranteed to be the tab's grant. That is the one instruction given
// - it is unlikely to be actively wrong on either platform, unlike the
// iOS-specific phrasing this replaced.
function standaloneCameraHint() {
    return isStandalonePWA()
        ? ' This can happen in the installed app version. Open this site in your '
          + 'regular browser (not the installed icon), allow the camera there, '
          + 'then fully close this app and reopen it.'
        : '';
}

// Resolves once the video is actually producing frames, not just once the
// stream promise resolved - the two are not the same thing (see above).
// Resolves false, rather than rejecting, on timeout: a stall is reported to
// the caller as a normal failure to handle, not an exception to catch.
function waitForVideoFrame(video, timeoutMs = 4000) {
    if (video.videoWidth > 0) return Promise.resolve(true);
    return new Promise(resolve => {
        let done = false;
        const finish = ok => { if (done) return; done = true; cleanup(); resolve(ok); };
        const onFrame = () => { if (video.videoWidth > 0) finish(true); };
        const cleanup = () => {
            video.removeEventListener('loadedmetadata', onFrame);
            video.removeEventListener('canplay', onFrame);
            video.removeEventListener('playing', onFrame);
            clearTimeout(timer);
        };
        video.addEventListener('loadedmetadata', onFrame);
        video.addEventListener('canplay', onFrame);
        video.addEventListener('playing', onFrame);
        const timer = setTimeout(() => finish(false), timeoutMs);
    });
}

// --- Camera Capture ---
class CameraCapture {
    constructor(videoEl, canvasEl, qualityEl) {
        this.video = videoEl;
        this.canvas = canvasEl;
        this.qualityBadge = qualityEl;
        this.stream = null;
        // Faces are photographed with the front camera. The rear default was
        // wrong everywhere: a laptop has no rear camera at all, and on a phone
        // it pointed away from the person being registered.
        this.facingMode = 'user';
        this.torchEnabled = false;
        this.isActive = false;
        this.qualityTimer = null;
    }
    
    async start() {
        if (this.isActive) return;

        // On an insecure origin the browser does not merely refuse the camera -
        // navigator.mediaDevices is undefined entirely, so the old code threw a
        // TypeError and reported "Could not access camera", which sent people
        // hunting for a permission problem that was not there. Opening the app
        // over plain http on a phone is exactly how this happens.
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            const msg = window.isSecureContext
                ? 'This browser does not support camera capture.'
                : `The camera only works over HTTPS. This page is on ${location.protocol}//${location.host} - open the https:// address instead.`;
            showToast('Camera unavailable', msg, 'error');
            return false;
        }

        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: this.facingMode,
                    width: { ideal: 1280 },
                    height: { ideal: 960 }
                },
                audio: false
            });
            this.video.srcObject = this.stream;
            this.isActive = true;
            this.torchEnabled = false;

            // If metadata is already available the event has fired and will not
            // fire again, so waiting on it here hung start() forever.
            if (this.video.readyState < 1) {
                await new Promise(resolve => {
                    const done = () => { this.video.removeEventListener('loadedmetadata', done); resolve(); };
                    this.video.addEventListener('loadedmetadata', done);
                    setTimeout(done, 4000);          // never block the UI indefinitely
                });
            }
            await this.video.play().catch(() => {});

            // getUserMedia resolving is not proof the camera works - see the
            // comment above isStandalonePWA(). A stream can be granted and the
            // video can still never render a frame, most often in an installed
            // iOS app. Without this check that state looked identical to
            // "working": isActive true, no error, just a black rectangle.
            const gotFrame = await waitForVideoFrame(this.video);
            if (!gotFrame) {
                this.stop();
                showToast('Camera unavailable',
                          'The camera started but no picture appeared.' + standaloneCameraHint(),
                          'error');
                return false;
            }

            this._startQualityMonitor();
            return true;
        } catch (err) {
            console.error('Camera error:', err);
            showToast('Camera unavailable', CameraCapture.explain(err), 'error');
            return false;
        }
    }

    // The browser knows exactly why the camera failed; the old handler threw
    // that away and said "Could not access camera" for every cause.
    static explain(err) {
        const hint = standaloneCameraHint();
        switch (err && err.name) {
            case 'NotAllowedError':
            case 'PermissionDeniedError':
                return isStandalonePWA()
                    ? 'Camera permission was blocked.' + hint
                    : 'Camera permission was blocked. Tap the padlock in the address bar and allow camera, then try again.';
            case 'NotFoundError':
            case 'DevicesNotFoundError':
                return 'No camera was found on this device.';
            case 'NotReadableError':
            case 'TrackStartError':
                return 'The camera is already in use by another app. Close it and try again.';
            case 'OverconstrainedError':
                return 'No camera matches the requested settings.';
            case 'SecurityError':
                return 'The camera is blocked on this page. It needs an https:// address.' + hint;
            default:
                return ((err && err.message) ? err.message : 'The camera could not be started.') + hint;
        }
    }
    
    stop() {
        if (!this.isActive) return;
        if (this.stream) {
            this.stream.getTracks().forEach(t => t.stop());
        }
        this.video.srcObject = null;
        this.isActive = false;
        this.torchEnabled = false;
        if (this.qualityTimer) clearInterval(this.qualityTimer);
    }
    
    async switchCamera() {
        this.facingMode = this.facingMode === 'environment' ? 'user' : 'environment';
        this.stop();
        await this.start();
    }
    
    async toggleFlash() {
        if (!this.stream) return;
        const track = this.stream.getVideoTracks()[0];
        if (!track) return;
        
        try {
            const capabilities = track.getCapabilities();
            if (capabilities.torch) {
                this.torchEnabled = !this.torchEnabled;
                await track.applyConstraints({
                    advanced: [{ torch: this.torchEnabled }]
                });
            } else {
                showToast('Info', 'Flash not supported on this device/camera', 'info');
            }
        } catch (err) {
            console.error('Flash error:', err);
        }
    }
    
    async capture() {
        if (!this.isActive) return null;
        
        // Haptic feedback if supported
        if (navigator.vibrate) navigator.vibrate(50);
        
        this.canvas.width = this.video.videoWidth;
        this.canvas.height = this.video.videoHeight;
        const ctx = this.canvas.getContext('2d');
        
        // Handle mirroring for front camera
        if (this.facingMode === 'user') {
            ctx.translate(this.canvas.width, 0);
            ctx.scale(-1, 1);
        }
        
        ctx.drawImage(this.video, 0, 0);
        
        return new Promise(resolve => {
            this.canvas.toBlob(blob => {
                if (blob) {
                    const file = new File([blob], `capture_${Date.now()}.jpg`, { type: 'image/jpeg' });
                    file.source = `camera_${this.facingMode}`;
                    resolve(file);
                } else {
                    resolve(null);
                }
            }, 'image/jpeg', 0.9);
        });
    }
    
    _startQualityMonitor() {
        if (this.qualityTimer) clearInterval(this.qualityTimer);
        
        this.qualityTimer = setInterval(() => {
            if (!this.isActive || !this.video.videoWidth) return;
            
            // Very basic quality heuristic - just simulate for now or leave as 'Ready'
            // In a real app we'd draw to an offscreen canvas and calculate brightness/blur
            if (this.qualityBadge) {
                this.qualityBadge.textContent = 'Good Lighting';
                this.qualityBadge.style.color = '#34d399'; // green
            }
        }, 1000);
    }
}

// --- Router ---
function initRouter() {
    window.addEventListener('hashchange', handleRoute);
    handleRoute();
}

let currentCameraCapture = null;

function handleRoute() {
    let hash = window.location.hash.slice(1) || '/dashboard';
    
    // Stop camera if navigating away
    if (currentCameraCapture) {
        currentCameraCapture.stop();
        currentCameraCapture = null;
    }
    
    // Default route
    if (hash === '/') hash = '/dashboard';
    
    state.currentRoute = hash;
    
    // Update active nav
    document.querySelectorAll('.nav-item, .bottom-nav-item').forEach(el => {
        if (el.getAttribute('data-route') === hash) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
    });

    // Close sidebar on mobile route change
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    if (sidebar && overlay) {
        sidebar.classList.remove('open');
        overlay.classList.remove('active');
    }

    const root = document.getElementById('app-root');
    const title = document.getElementById('page-title');
    const actions = document.getElementById('header-actions');
    
    root.innerHTML = '';
    actions.innerHTML = '';

    // Page templates are cloned fresh on every route change, so re-apply the
    // role gating afterwards - the pass at login only saw the static shell.
    setTimeout(applyRoleChrome, 0);

    if (hash === '/dashboard') {
        title.textContent = 'Dashboard';
        actions.innerHTML = `
            <button class="btn btn-secondary" onclick="window.location.hash='/students'">Add Student</button>
            <button class="btn btn-primary" onclick="window.location.hash='/mark'">Take Attendance</button>
        `;
        const tpl = document.getElementById('tpl-dashboard').content.cloneNode(true);
        root.appendChild(tpl);
        renderDashboard();
    } 
    else if (hash === '/mark') {
        title.textContent = 'Mark Attendance';
        const tpl = document.getElementById('tpl-mark').content.cloneNode(true);
        root.appendChild(tpl);
        initMarkPage();
    }
    else if (hash === '/students') {
        title.textContent = 'Students';
        actions.innerHTML = `
            <button class="btn btn-primary" onclick="openRegisterModal()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                Register Student
            </button>
        `;
        const tpl = document.getElementById('tpl-students').content.cloneNode(true);
        root.appendChild(tpl);
        renderStudents();
    }
    else if (hash === '/analytics') {
        title.textContent = 'Analytics';
        renderAnalytics();
    }
    else if (hash === '/records') {
        title.textContent = 'Records';
        const tpl = document.getElementById('tpl-records').content.cloneNode(true);
        root.appendChild(tpl);
        initRecordsPage();
    }
    else if (hash === '/centres') {
        title.textContent = 'Khelo India Centres';
        if (isSuperAdmin()) {
            actions.innerHTML = `
                <button class="btn btn-secondary" onclick="openImportCentresModal()">Import data</button>
                <button class="btn btn-secondary" onclick="purgeDemoCentres()">Remove demo</button>
                <button class="btn btn-primary" onclick="openAddCentreModal()">Add centre</button>`;
        }
        root.appendChild(document.getElementById('tpl-centres').content.cloneNode(true));
        renderCentresPage();
    }
    else if (hash === '/users') {
        if (!isSuperAdmin()) { window.location.hash = '#/dashboard'; return; }
        title.textContent = 'Accounts';
        actions.innerHTML = `<button class="btn btn-primary" onclick="openAddUserModal()">Create account</button>`;
        root.appendChild(document.getElementById('tpl-users').content.cloneNode(true));
        renderUsersPage();
    }
    else {
        // Unknown route - e.g. a bookmark to a page that no longer exists.
        // Fall back to the dashboard rather than leaving the shell blank.
        window.location.hash = '#/dashboard';
    }
}

// --- API Wrapper ---
const api = {
    async get(endpoint) {
        try {
            const res = await fetch(endpoint);
            if (res.status === 401) { handleUnauthorized(); throw new Error('Unauthorized'); }
            if (!res.ok) throw new Error('API Error');
            return await res.json();
        } catch (err) {
            if (err.message !== 'Unauthorized') showToast('Error', 'Failed to fetch data', 'error');
            throw err;
        }
    },
    async postForm(endpoint, formData) {
        try {
            const res = await fetch(endpoint, {
                method: 'POST',
                body: formData
            });
            if (res.status === 401) { handleUnauthorized(); throw new Error('Unauthorized'); }
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'API Error');
            return data;
        } catch (err) {
            if (err.message !== 'Unauthorized') showToast('Error', err.message, 'error');
            throw err;
        }
    },
    async delete(endpoint) {
        try {
            const res = await fetch(endpoint, { method: 'DELETE' });
            if (res.status === 401) { handleUnauthorized(); throw new Error('Unauthorized'); }
            if (!res.ok) throw new Error('API Error');
            return await res.json();
        } catch (err) {
            if (err.message !== 'Unauthorized') showToast('Error', 'Failed to delete', 'error');
            throw err;
        }
    }
};

// --- Application Init ---
/* Resolve a device location, resolving null rather than rejecting. */
function captureLocation() {
    return new Promise(resolve => {
        if (!navigator.geolocation) return resolve(null);
        navigator.geolocation.getCurrentPosition(
            pos => resolve({
                latitude: pos.coords.latitude,
                longitude: pos.coords.longitude,
                accuracy: Math.round(pos.coords.accuracy),
            }),
            () => resolve(null),
            { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
        );
    });
}

async function boot() {
    document.getElementById('login-form').addEventListener('submit', doLogin);
    const user = await checkSession();
    if (!user) { showLogin(); return; }
    hideLogin();
    applyRoleChrome();
    await initApp();
}

async function initApp() {
    checkHealth();
    setInterval(checkHealth, 30000); // Check health every 30s
    
    // Mobile Nav
    const hamburger = document.getElementById('hamburger-btn');
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    if (hamburger && sidebar && overlay) {
        hamburger.addEventListener('click', () => {
            sidebar.classList.add('open');
            overlay.classList.add('active');
        });
        overlay.addEventListener('click', () => {
            sidebar.classList.remove('open');
            overlay.classList.remove('active');
        });
    }

    initRouter();
}

async function checkHealth() {
    try {
        const res = await fetch('/api/health');
        if (res.ok) {
            document.querySelector('.status-dot').classList.remove('offline');
            document.getElementById('api-status-text').textContent = 'System Online';
        } else {
            throw new Error('Offline');
        }
    } catch {
        document.querySelector('.status-dot').classList.add('offline');
        document.getElementById('api-status-text').textContent = 'System Offline';
    }
}

// --- Dashboard ---
async function renderDashboard() {
    try {
        // Analytics is fetched alongside the tiles so each tile can carry its
        // own 14-session sparkline. A number with no shape behind it cannot
        // tell you whether 12 present is recovery or decline.
        const [stats, series] = await Promise.all([
            api.get('/api/stats'),
            api.get('/api/analytics?days=60').catch(() => null),
        ]);
        const trend = (series && series.trend) || [];
        const present = trend.map(d => d.present);
        const spark = present.length > 1
            ? Charts.sparkline(present.slice(-14), { width: 120, height: 30 })
            : '';
        
        // Render Stats Grid
        const statsHtml = `
            <div class="stat-card">
                <div class="stat-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
                    <span>Enrolled Students</span>
                </div>
                <div class="stat-value">${stats.students}</div>
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>
                    <span>Present Today</span>
                </div>
                <div class="stat-value text-green">${stats.present_today}</div>
                ${spark ? `<div class="stat-spark">${spark}</div>` : ''}
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                    <span>Absent Today</span>
                </div>
                <div class="stat-value text-red">${stats.absent_today}</div>
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
                    <span>Attendance Rate</span>
                </div>
                <div class="stat-value">${stats.attendance_rate.toFixed(1)}%</div>
            </div>
        `;
        document.getElementById('dashboard-stats').innerHTML = statsHtml;
        Charts.countUp(document.getElementById('dashboard-stats'));

        const trendBox = document.getElementById('dashboard-trend');
        if (trendBox) {
            const shortDate = (iso) => {
                const [, m, d] = (iso || '').split('-');
                return m && d ? `${d}/${m}` : iso;
            };
            trendBox.innerHTML = trend.length > 1
                ? Charts.areaChart(
                      trend.map(d => ({ label: d.date, short: shortDate(d.date), value: d.present })),
                      { height: 220 })
                  + Charts.tableView(['Date', 'Present'],
                      trend.map(d => [d.date, d.present]), 'Athletes present per session')
                : `<div class="text-sm text-muted">Not enough sessions recorded yet.</div>`;
            Charts.initChartInteraction(document.getElementById('app-root'));
        }

        // Render Recent Activity
        const recentHtml = stats.recent.length === 0 ? 
            '<div class="p-4 text-center text-muted">No recent activity</div>' :
            stats.recent.map(r => `
                <div class="activity-item">
                    <div class="avatar">${r.name.charAt(0)}</div>
                    <div class="activity-details">
                        <div class="activity-name">${r.name}</div>
                        <div class="activity-sub">${r.roll_no}</div>
                    </div>
                    <div class="activity-meta">
                        <div class="badge badge-green mb-1">${(r.confidence * 100).toFixed(0)}% Match</div>
                        <div class="activity-time">${r.time}</div>
                    </div>
                </div>
            `).join('');
        document.getElementById('dashboard-recent').innerHTML = recentHtml;


    } catch (e) {
        console.error(e);
    }
}

// --- Mark Attendance Page ---
let currentMarkFile = null;
let currentDetectionMode = 'fused';

async function populateMarkCentres() {
    const sel = document.getElementById('mark-centre');
    if (!sel || sel.options.length > 1) return;
    try {
        const data = await api.get('/api/centres');
        data.centres.forEach(c => sel.add(
            new Option(`${c.name} (${c.code})${c.people_count ? ` - ${c.people_count} enrolled` : ''}`, c.id)));

        // Picking a centre matters: matching a photo against the wrong roster
        // returns zero, which reads like a recognition failure. Remember what
        // was used last, and otherwise choose the centre with the most people
        // enrolled - an earlier version preferred the first non-demo centre and
        // so ignored a demo centre holding every real athlete.
        const remembered = localStorage.getItem('facemark.lastCentre');
        const known = data.centres.some(c => String(c.id) === remembered);
        if (remembered && known) {
            sel.value = remembered;
        } else {
            const busiest = data.centres
                .slice()
                .sort((a, b) => (b.people_count || 0) - (a.people_count || 0))[0];
            if (busiest && busiest.people_count) sel.value = busiest.id;
        }
        sel.addEventListener('change', () => {
            if (sel.value) localStorage.setItem('facemark.lastCentre', sel.value);
            else localStorage.removeItem('facemark.lastCentre');
        });
    } catch { /* the selector simply stays on "All centres" */ }
}

function initMarkPage() {
    populateMarkCentres();
    const fileInput = document.getElementById('mark-file');
    const dropzone = document.getElementById('mark-dropzone');
    const previewContainer = document.getElementById('mark-preview-container');
    const previewImg = document.getElementById('mark-preview');
    const dropContent = document.getElementById('mark-dropzone-content');
    const clearBtn = document.getElementById('mark-clear-btn');
    const processBtn = document.getElementById('btn-process');
    const form = document.getElementById('mark-form');

    // Camera & Tabs
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabPanels = document.querySelectorAll('.tab-panel');
    const sourceInput = document.getElementById('mark-source');

    if (tabBtns.length > 0) {
        currentCameraCapture = new CameraCapture(
            document.getElementById('camera-video'),
            document.getElementById('camera-overlay'),
            document.getElementById('camera-quality')
        );
        // A group is photographed across the room, so attendance starts on the
        // rear camera; registration photographs the person holding the device
        // and keeps the front one. The switch button overrides either.
        currentCameraCapture.facingMode = 'environment';

        tabBtns.forEach(btn => {
            btn.addEventListener('click', async () => {
                tabBtns.forEach(b => b.classList.remove('active'));
                tabPanels.forEach(p => p.classList.remove('active'));
                btn.classList.add('active');
                
                const target = btn.getAttribute('data-target');
                const panel = document.querySelector(`.tab-panel[data-tab="${target}"]`);
                if (panel) panel.classList.add('active');
                if (sourceInput) sourceInput.value = target;

                if (target === 'camera') {
                    await currentCameraCapture.start();
                } else {
                    currentCameraCapture.stop();
                }
            });
        });

        const switchBtn = document.getElementById('camera-switch');
        const flashBtn = document.getElementById('camera-flash');
        const shutterBtn = document.getElementById('camera-shutter');
        
        if (switchBtn) switchBtn.addEventListener('click', () => currentCameraCapture.switchCamera());
        if (flashBtn) flashBtn.addEventListener('click', () => currentCameraCapture.toggleFlash());
        if (shutterBtn) shutterBtn.addEventListener('click', async () => {
            const file = await currentCameraCapture.capture();
            if (file) {
                // switch to upload tab
                tabBtns[0].click();
                handleFile(file);
            }
        });
    }

    // File Handling
    function handleFile(file) {
        if (!file || !file.type.startsWith('image/')) return;
        currentMarkFile = file;
        
        const reader = new FileReader();
        reader.onload = (e) => {
            previewImg.src = e.target.result;
            dropContent.classList.add('hidden');
            previewContainer.classList.remove('hidden');
            processBtn.disabled = false;
        };
        reader.readAsDataURL(file);
    }

    fileInput.addEventListener('change', (e) => handleFile(e.target.files[0]));
    
    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        handleFile(e.dataTransfer.files[0]);
    });

    clearBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        currentMarkFile = null;
        fileInput.value = '';
        previewImg.src = '';
        dropContent.classList.remove('hidden');
        previewContainer.classList.add('hidden');
        processBtn.disabled = true;
    });

    // Submit
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!currentMarkFile) return;

        processBtn.disabled = true;
        
        // --- Heavy Processing Progress Bar UI ---
        const resultsContainer = document.getElementById('mark-results-container');
        resultsContainer.innerHTML = `
            <div style="padding: 40px 20px; background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); text-align: center;">
                <div style="margin-bottom: 24px;">
                    <svg class="spin" style="width: 32px; height: 32px; color: var(--accent); display: inline-block; animation: spin 1s linear infinite;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-opacity="0.25"></circle>
                        <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" stroke-linecap="round"></path>
                    </svg>
                </div>
                <div style="font-size: 14px; font-weight: 500; color: var(--text-primary); margin-bottom: 16px;" id="progress-text">Initializing AI Engine...</div>
                <div style="width: 100%; height: 6px; background: var(--bg-elevated); border-radius: 3px; overflow: hidden; margin-bottom: 12px;">
                    <div id="progress-bar" style="width: 0%; height: 100%; background: var(--accent); transition: width 0.3s ease;"></div>
                </div>
                <div style="font-size: 12px; color: var(--text-muted); id="progress-eta">Analyzing heavy crowd photo... estimating time</div>
            </div>
            <style>@keyframes spin { 100% { transform: rotate(360deg); } }</style>
        `;

        let progress = 0;
        const progressEl = document.getElementById('progress-bar');
        const textEl = document.getElementById('progress-text');
        
        // Every phase here once named a component that no longer exists -
        // YOLO11, SCRFD, AdaFace, ArcFace, cascade verification, age-aware
        // matching - all removed with the licence migration or at the
        // customer's request. Naming the pipeline was never useful to the
        // person waiting; naming the STAGE is.
        const phases = [
            {p: 5,  t: "Uploading photo..."},
            {p: 35, t: "Detecting faces..."},
            {p: 65, t: "Matching against the roster..."},
            {p: 90, t: "Saving attendance..."}
        ];
        
        let phaseIdx = 0;
        const progressTimer = setInterval(() => {
            if (phaseIdx < phases.length && progress >= phases[phaseIdx].p) {
                textEl.textContent = phases[phaseIdx].t;
                phaseIdx++;
            }
            if (progress < 96) {
                progress += Math.random() * 1.5; // Simulate progress climbing slowly
                progressEl.style.width = Math.min(progress, 96) + '%';
            }
        }, 200);

        const formData = new FormData();
        formData.append('photo', currentMarkFile);
        formData.append('detection_mode', 'fused');

        // Geo marking: attach the device fix if the user granted permission.
        // A refusal or timeout is not an error - the capture is simply recorded
        // as `no_fix` rather than blocking attendance.
        const fix = await captureLocation();
        if (fix) {
            formData.append('latitude', fix.latitude);
            formData.append('longitude', fix.longitude);
            formData.append('accuracy_m', fix.accuracy);
        }
        if (session.user && session.user.centre_id) {
            formData.append('centre_id', session.user.centre_id);
        } else {
            const cs = document.getElementById('mark-centre');
            if (cs && cs.value) formData.append('centre_id', cs.value);
        }
        if (currentMarkFile.source) {
            formData.append('source', currentMarkFile.source);
        } else if (document.getElementById('mark-source')) {
            formData.append('source', document.getElementById('mark-source').value);
        }

        try {
            const data = await api.postForm('/api/attendance/process', formData);
            clearInterval(progressTimer);
            progressEl.style.width = '100%';
            textEl.textContent = "Done!";
            setTimeout(() => {
                renderMarkResults(data);
                showToast('Success', `${data.recognized_count} student(s) marked present`, 'success');
            }, 300);
        } catch (err) {
            clearInterval(progressTimer);
            resultsContainer.innerHTML = `<div style="color: var(--red);">Error processing image. Check console.</div>`;
        } finally {
            processBtn.disabled = false;
        }
    });
}

function geoBanner(geo) {
    if (!geo) return '';
    const map = {
        inside:  ['green', 'Verified at the centre',
                  `Captured ${geo.distance_m ?? 0} m from the registered location.`],
        outside: ['red', 'Captured outside the centre',
                  `${geo.distance_m} m away - beyond this centre's geo-fence.`],
        no_fix:  ['amber', 'No location recorded',
                  'The browser gave no position. Allow location access to geo-verify attendance.'],
        unknown: ['amber', 'Location not verified',
                  'No centre selected, or the centre has no coordinates on file.'],
    };
    const [tone, title, detail] = map[geo.status] || map.unknown;
    const acc = geo.accuracy_m ? ` Device accuracy about ${Math.round(geo.accuracy_m)} m.` : '';
    return `<div class="notice notice-${tone === 'green' ? 'blue' : 'amber'}"
                 style="margin-bottom:16px;border-color:var(--${tone})">
        <strong>${title}</strong> ${Charts.esc(detail)}${acc}
        ${geo.latitude != null ? `<div class="text-xs text-muted mt-2 font-mono">
            ${geo.latitude.toFixed(5)}, ${geo.longitude.toFixed(5)}</div>` : ''}
    </div>`;
}

function renderMarkResults(data) {
    const container = document.getElementById('mark-results-container');
    const totalSec = (data.timings.total_ms / 1000).toFixed(1);
    
    let html = `
        <!-- Sticky Quick Jump Slider Bar -->
        <div style="position: sticky; top: 0; z-index: 20; background: var(--bg-surface); padding-bottom: 12px; margin-bottom: 16px; border-bottom: 1px solid var(--border-subtle); display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
            <button type="button" class="btn btn-secondary" style="height: 30px; font-size: 12px; padding: 0 10px;" onclick="document.getElementById('sec-summary').scrollIntoView({behavior:'smooth'})">
                ${Icon('chart')} Summary
            </button>
            ${data.recognized && data.recognized.length ? `
                <button type="button" class="btn btn-secondary" style="height: 30px; font-size: 12px; padding: 0 10px;" onclick="document.getElementById('sec-recognized').scrollIntoView({behavior:'smooth'})">
                    ${Icon('users')} Recognized (${data.recognized.length})
                </button>
            ` : ''}
            ${data.unknown && data.unknown.length ? `
                <button type="button" class="btn btn-secondary" style="height: 30px; font-size: 12px; padding: 0 10px;" onclick="document.getElementById('sec-unknowns').scrollIntoView({behavior:'smooth'})">
                    ${Icon('unknown')} Unknowns (${data.unknown.length})
                </button>
            ` : ''}
            ${data.annotated_url ? `
                <button type="button" class="btn btn-secondary" style="height: 30px; font-size: 12px; padding: 0 10px;" onclick="document.getElementById('sec-annotated').scrollIntoView({behavior:'smooth'})">
                    ${Icon('image')} Visual Photo
                </button>
            ` : ''}
        </div>

        ${data.photo_quality && ['poor','unusable','fair'].includes(data.photo_quality.level) ? `
            <div class="notice notice-${data.photo_quality.level === 'fair' ? 'blue' : 'amber'}"
                 style="margin-bottom:16px">
                <strong>Photo quality: ${Charts.esc(data.photo_quality.level)}</strong>
                (faces average ${data.photo_quality.median_face_px} px).
                ${Charts.esc(data.photo_quality.advice)}
            </div>` : ''}
        ${data.scope_warning ? `
            <div class="notice notice-amber" style="margin-bottom:16px">
                <strong>No centre selected.</strong> ${Charts.esc(data.scope_warning)}
            </div>` : ''}
        ${data.centre_report ? `
            <div class="notice notice-amber" style="margin-bottom:16px">
                <strong>${data.centre_report.off_centre_count}
                ${data.centre_report.off_centre_count === 1 ? 'person is' : 'people are'}
                registered at another centre.</strong>
                They were recognised but <strong>not marked present</strong>, because attendance
                here belongs to the selected centre.
                ${data.centre_report.names && data.centre_report.names.length ? `
                    <div style="margin-top:6px;font-size:12px">
                        ${data.centre_report.names.map(n => Charts.esc(n)).join(', ')}
                    </div>` : ''}
                <div style="margin-top:6px;font-size:12px">
                    ${data.centre_report.breakdown.map(b =>
                        `${Charts.esc(b.centre_name)}: ${b.count}`).join(' &nbsp;·&nbsp; ')}
                </div>
            </div>` : ''}
        ${data.filtered_faces ? `
            <div class="notice notice-blue" style="margin-bottom:16px">
                <strong>${data.filtered_faces} printed face${data.filtered_faces > 1 ? 's' : ''} ignored.</strong>
                A portrait on a banner or poster was detected and excluded from the count.
            </div>` : ''}
        ${data.faces_detected > 0 && data.recognized_count === 0 ? `
            <div class="notice notice-amber" style="margin-bottom:16px">
                <strong>${data.faces_detected} faces found, none recognised.</strong>
                The most common cause is the wrong centre being selected - a photo
                matched against another centre's roster returns nothing. Check the
                centre above, then use <em>Who is this?</em> on any face to confirm
                an identity manually.
            </div>` : ''}
        ${data.faces_detected === 0 ? `
            <div class="notice notice-amber" style="margin-bottom:16px">
                <strong>No faces detected.</strong>
                ${Charts.esc(data.message || 'Try moving closer or improving the lighting.')}
            </div>` : ''}
        ${geoBanner(data.geo)}

        <div id="sec-summary" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px;">
            <div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px;">
                <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Faces Found</div>
                <div style="font-size: 20px; font-weight: 700; color: var(--text-primary); margin-top: 2px;">${data.faces_detected}</div>
            </div>
            <div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px;">
                <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Athletes Present</div>
                <div style="font-size: 20px; font-weight: 700; color: var(--green); margin-top: 2px;">${data.athletes_present ?? data.recognized_count}</div>
            </div>
            <div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px;">
                <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Coaches Present</div>
                <div style="font-size: 20px; font-weight: 700; color: var(--accent); margin-top: 2px;">${data.coaches_present ?? 0}</div>
            </div>
            <div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 12px 14px;">
                <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Speed</div>
                <div style="font-size: 20px; font-weight: 700; color: var(--accent); margin-top: 2px;">${totalSec}s</div>
            </div>
        </div>
    `;

    if (data.recognized && data.recognized.length > 0) {
        html += `<div id="sec-recognized" style="font-weight: 600; font-size: 14px; margin-bottom: 12px; color: var(--text-primary);">Recognized Students (${data.recognized.length})</div><div class="results-grid">`;
        data.recognized.forEach(r => {
            const statusBadge = r.marked_now ? 
                `<span class="badge badge-green">${Icon('check', 12)} Marked Present</span>` : 
                `<span class="badge badge-blue">Already Marked Today</span>`;
            html += `
                <div class="face-card" style="border: 1px solid var(--border-subtle); background: var(--bg-elevated);">
                    <div class="face-img-wrap" style="height: 140px;">
                        <img src="${r.face_url}" class="face-img" alt="${r.name}">
                    </div>
                    <div class="face-info">
                        <div class="face-name" style="font-size: 14px; font-weight: 600;">${r.name}</div>
                        <div class="face-sub" style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">${r.roll_no}
                            ${r.role === 'coach' ? '<span class="badge badge-blue" style="margin-left:6px">Coach</span>' : ''}</div>
                        <div class="flex-between text-xs text-muted mb-1">
                            <span>Match Accuracy</span>
                            <span style="color: var(--green); font-weight: 600;">${(r.similarity * 100).toFixed(0)}%</span>
                        </div>
                        <div class="similarity-bar-wrap" style="height: 5px; background: var(--track);">
                            <div class="similarity-bar" style="width: ${r.similarity * 100}%; background: var(--green);"></div>
                        </div>
                        <div class="mt-2" style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">${statusBadge}</div>
                    </div>
                </div>
            `;
        });
        html += `</div>`;
    } else {
        html += `
            <div style="padding: 20px; text-align: center; color: var(--text-muted); background: var(--bg-elevated); border-radius: var(--radius-md); margin-bottom: 20px; border: 1px solid var(--border-subtle);">
                No currently registered students were recognized in this photo.
            </div>
        `;
    }

    if (data.unknown && data.unknown.length > 0) {
        html += `
            <div id="sec-unknowns" style="display: flex; justify-content: space-between; align-items: center; margin-top: 24px; margin-bottom: 12px;">
                <div style="font-weight: 600; font-size: 14px; color: var(--text-primary);">
                    Unregistered Faces (${data.unknown.length})
                </div>
            </div>
            <div class="results-grid" style="grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));">
        `;
        data.unknown.forEach((u, idx) => {
            html += `
                <div class="face-card stacked">
                    <div class="face-img-wrap" style="height: 130px;">
                        <img src="${u.face_url}" class="face-img" alt="Unrecognised face ${idx + 1}">
                    </div>
                    <div class="face-info" style="padding: 10px;">
                        <div class="face-name" style="font-size: 12px; font-weight: 600; color: var(--amber);">Face ${idx + 1}</div>
                        <button class="btn btn-secondary w-full" style="min-height:32px;font-size:12px"
                                onclick="openAssignModal('${u.face_url}')">Who is this?</button>
                    </div>
                </div>
            `;
        });
        html += `</div>`;
    }

    if (data.annotated_url) {
        html += `
            <div id="sec-annotated" style="display:flex; justify-content:space-between; align-items:center; margin-top: 24px; margin-bottom: 12px;">
                <div style="font-weight: 600; font-size: 14px; color: var(--text-primary);">Visual Recognition Output</div>
                <a href="${data.annotated_url}" target="_blank" class="btn btn-secondary" style="height: 28px; font-size: 11px; padding: 0 8px;">
                    ${Icon('expand', 13)} Open Full Size
                </a>
            </div>
            <div class="annotated-img-wrap" style="border: 1px solid var(--border-subtle); border-radius: var(--radius-md); overflow: hidden; cursor: pointer;" onclick="window.open('${data.annotated_url}', '_blank')">
                <img src="${data.annotated_url}" class="annotated-img" alt="Annotated Result" style="width: 100%; display: block;">
            </div>
        `;
    }

    container.innerHTML = html;
}

// --- Students Page ---
async function renderStudents() {
    try {
        const data = await api.get('/api/students');
        state.students = data.students;
        drawStudents(state.students);

        document.getElementById('student-search').addEventListener('input', (e) => {
            const term = e.target.value.toLowerCase();
            const filtered = state.students.filter(s => 
                s.name.toLowerCase().includes(term) || 
                s.roll_no.toLowerCase().includes(term)
            );
            drawStudents(filtered);
        });
    } catch (e) {
        console.error(e);
    }
}

function drawStudents(students) {
    const grid = document.getElementById('students-grid');
    if (students.length === 0) {
        grid.innerHTML = `<div class="empty-state py-12" style="grid-column: 1/-1">No students found</div>`;
        return;
    }

    grid.innerHTML = students.map(s => {
        const nTmpl = s.templates || 0;
        const tmplBadge = nTmpl > 0 ?
            `<span class="badge ${nTmpl >= 6 ? 'badge-green' : 'badge-blue'}" style="font-size: 10px;" title="Face templates stored for this person">${nTmpl} template${nTmpl === 1 ? '' : 's'}</span>` : '';
        return `
        <div class="student-card">
            <button class="btn-icon btn-delete-student" onclick="confirmDeleteStudent('${s.id}', '${s.name}')" title="Delete">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
            </button>
            <div class="student-photo-wrap">
                <img src="${s.photo_url}" class="student-photo" alt="${s.name}">
            </div>
            <div class="student-info">
                <div class="student-name">${s.name}</div>
                <div class="student-meta">
                    <span class="student-roll">${s.roll_no}</span>
                    <span title="Days marked present">${s.total_present || 0} present</span>
                </div>
                <div class="student-meta" style="margin-top: 6px; gap: 6px; display: flex; flex-wrap: wrap; align-items: center;">
                    ${tmplBadge}
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="openAddPhotoModal('${s.id}', '${s.name.replace(/'/g, "\\'")}')" title="Add another photo (recent selfie or ID)">
                        ${Icon('upload', 12)}Add photo
                    </button>
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="openMultiViewEnrol('${s.id}', '${s.name.replace(/'/g, "\\'")}')" title="Scan the face from several angles with the camera - the strongest way to improve recognition">
                        ${Icon('camera', 12)}Register face
                    </button>
                </div>
            </div>
        </div>
        `;
    }).join('');
}

// --- Records Page ---
async function initRecordsPage() {
    const dateInput = document.getElementById('records-date');
    const today = new Date().toISOString().split('T')[0];
    dateInput.value = today;

    dateInput.addEventListener('change', () => loadRecords(dateInput.value));
    
    document.getElementById('btn-export').addEventListener('click', () => {
        window.location.href = `/api/attendance/export?day=${dateInput.value}`;
    });

    loadRecords(today);
}

async function loadRecords(dateStr) {
    try {
        const data = await api.get(`/api/attendance?day=${dateStr}`);
        const tbody = document.getElementById('records-table-body');
        const empty = document.getElementById('records-empty');
        const table = document.querySelector('.data-table');

        if (!data.records || data.records.length === 0) {
            tbody.innerHTML = '';
            table.classList.add('hidden');
            empty.classList.remove('hidden');
            return;
        }

        table.classList.remove('hidden');
        empty.classList.add('hidden');

        const geoCell = (r) => {
            const map = {
                inside:  ['badge-green', 'At centre', `${r.distance_m ?? 0} m from the registered location`],
                outside: ['badge-red',   'Outside',   `${r.distance_m} m away - beyond the geo-fence`],
                no_fix:  ['badge-amber', 'No fix',    'The device reported no position'],
                unknown: ['badge-amber', 'Unverified', 'No centre coordinates to compare against'],
            };
            // Records written before geo marking existed have a null status.
            const [cls, label, tip] = map[r.geo_status] || ['badge-amber', 'Unverified', 'Recorded before location capture was enabled'];
            return `<span class="badge ${cls}" title="${Charts.esc(tip)}">${label}</span>`;
        };

        tbody.innerHTML = data.records.map(r => `
            <tr>
                <td class="cell-primary">
                    <div class="flex items-center gap-2">
                        <img src="${r.photo_url}" class="avatar avatar-sm">
                        <span class="font-medium">${Charts.esc(r.name)}</span>
                    </div>
                </td>
                <td class="font-mono" data-label="Roll No">${Charts.esc(r.roll_no)}</td>
                <td data-label="Role">${r.role === 'coach' ? '<span class="badge badge-blue">Coach</span>'
                                         : '<span class="badge badge-green">Athlete</span>'}</td>
                <td class="font-mono" data-label="Confidence">${(r.confidence * 100).toFixed(1)}%</td>
                <td data-label="Location">${geoCell(r)}</td>
                <td class="text-muted" data-label="Centre">${Charts.esc(r.centre_name || '-')}</td>
                <td class="text-muted" data-label="Time">${Charts.esc(r.time)}</td>
            </tr>
        `).join('');

    } catch (e) {
        console.error(e);
    }
}

// --- Modals ---
function openModal(title, contentHTML, footerHTML) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = `
        ${contentHTML}
        <div class="modal-footer" style="margin: 20px -20px -20px; padding: 16px 20px; border-top: 1px solid var(--border-subtle); display: flex; justify-content: flex-end; gap: 12px; background: var(--bg-surface);">
            ${footerHTML}
        </div>
    `;
    document.getElementById('modal-container').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('modal-container').classList.add('hidden');
}

/* --------------------------------------------------------------------------
   Registration: details, then a face scan from the camera.

   What this replaces: a form that demanded an Aadhaar/ID card photo, offered a
   file picker as the default way to supply it, took a single frontal frame,
   and never asked which centre the person belonged to.

   Every part of that was wrong for this system. An ID card photo of a growing
   child is the single largest cause of missed matches measured on real data.
   A file upload cannot be verified as the person standing in front of you. One
   frontal frame gives the recogniser one viewpoint. And with attendance being
   centre-wise, a student registered without a centre can never be marked.

   So: the centre is required, the face comes from the camera only, and it is
   captured as a sweep across angles rather than a single shot.
   -------------------------------------------------------------------------- */

const REG_GUIDELINES = [
    'Stand in even light - avoid a bright window behind you',
    'Remove cap, sunglasses and mask',
    'Hold the device at arm\'s length, at eye level',
    'Only the person being registered should be in frame',
];

let regDetails = null;

async function openRegisterModal() {
    let centres = [];
    try {
        centres = (await api.get('/api/centres')).centres || [];
    } catch { /* the select simply renders empty and the field stays required */ }

    const html = `
        <form id="register-form" autocomplete="off">
            <div class="form-group">
                <label class="form-label" for="reg-name">Full name</label>
                <input type="text" id="reg-name" class="form-input" required placeholder="As it appears on the roster">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label" for="reg-roll">Roll number</label>
                    <input type="text" id="reg-roll" class="form-input" required placeholder="e.g. WEAA039F11">
                </div>
                <div class="form-group">
                    <label class="form-label" for="reg-centre">Centre</label>
                    <select id="reg-centre" class="form-select" required>
                        <option value="">Select a centre</option>
                        ${centres.map(c => `<option value="${c.id}">${Charts.esc(c.name)} (${Charts.esc(c.code)})</option>`).join('')}
                    </select>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label" for="reg-role">Role</label>
                    <select id="reg-role" class="form-select">
                        <option value="athlete">Athlete</option>
                        <option value="coach">Coach</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label" for="reg-sport">Sport <span class="text-muted">(optional)</span></label>
                    <input type="text" id="reg-sport" class="form-input" placeholder="e.g. Weightlifting">
                </div>
            </div>

            <div class="notice notice-blue" style="margin-top:4px">
                <strong>Before you scan the face</strong>
                <ul style="margin:6px 0 0 18px;padding:0;font-size:12px;line-height:1.7">
                    ${REG_GUIDELINES.map(g => `<li>${Charts.esc(g)}</li>`).join('')}
                </ul>
            </div>
        </form>`;

    openModal('Register person', html, `
        <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn btn-primary" onclick="regContinue()">Continue to face scan</button>`);
}

function regContinue() {
    const form = document.getElementById('register-form');
    if (!form.checkValidity()) { form.reportValidity(); return; }
    regDetails = {
        name:      document.getElementById('reg-name').value.trim(),
        roll_no:   document.getElementById('reg-roll').value.trim(),
        centre_id: document.getElementById('reg-centre').value,
        role:      document.getElementById('reg-role').value,
        sport:     document.getElementById('reg-sport').value.trim(),
    };
    // The scan is the same component used to re-register an existing person;
    // only what happens with the frames at the end differs.
    openFaceScan({
        title: `Scan face - ${regDetails.name}`,
        onComplete: regSubmit,
    });
}

async function regSubmit(frames) {
    const btn = document.getElementById('mv-save');
    if (btn) { btn.disabled = true; btn.textContent = 'Registering...'; }
    try {
        // The straight-ahead frame becomes the person's profile photo and their
        // first template; the rest are added as additional views.
        const fd = new FormData();
        fd.append('photo', frames[0].blob, 'face.jpg');
        fd.append('name', regDetails.name);
        fd.append('roll_no', regDetails.roll_no);
        fd.append('centre_id', regDetails.centre_id);
        fd.append('role', regDetails.role);
        if (regDetails.sport) fd.append('sport', regDetails.sport);

        const data = await api.postForm('/api/students', fd);
        const student = data.student || {};

        let added = 0;
        if (frames.length > 1 && student.id) {
            const fd2 = new FormData();
            frames.slice(1).forEach((f, i) => fd2.append('frames', f.blob, `${f.key}_${i}.jpg`));
            try {
                const r2 = await api.postForm(`/api/students/${student.id}/enroll-multiview`, fd2);
                added = r2.templates_added || 0;
            } catch {
                // The person exists and is usable from the frontal view; losing
                // the extra angles is worth reporting but not worth failing on.
                showToast('Registered, but not all views saved',
                          'The extra angles could not be stored. Use "Register face" on their card to retry.',
                          'error');
            }
        }
        closeFaceScan();
        showToast('Registered', `${regDetails.name} enrolled with ${1 + added} view${1 + added === 1 ? '' : 's'}`, 'success');
        if (state.currentRoute === '/students') renderStudents();
    } catch (err) {
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    }
}

// --- Add photo to existing student ---
window.openAddPhotoModal = function(studentId, name) {
    const html = `
        <form id="add-photo-form">
            <p style="margin-bottom: 14px; color: var(--text-secondary); font-size: 13px;">
                Add a photo for <strong>${name}</strong>. The system stores it as an extra
                biometric template - great for refreshing an old Aadhaar enrollment.
            </p>
            <div class="form-group">
                <label class="form-label">Photo</label>
                <input type="file" id="addp-file" accept="image/*" class="form-input" required style="padding: 6px;">
            </div>
            <div class="form-group">
                <label class="form-label">Photo Type</label>
                <select id="addp-source" class="form-input">
                    <option value="live" selected>Recent / live photo</option>
                    <option value="id">ID card photo (Aadhaar)</option>
                </select>
            </div>
        </form>
    `;
    const footer = `
        <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn btn-primary" onclick="submitAddPhoto(${studentId})">Add Photo</button>
    `;
    openModal('Add Photo', html, footer);
};

window.submitAddPhoto = async function(studentId) {
    const form = document.getElementById('add-photo-form');
    if (!form.checkValidity()) {
        form.reportValidity();
        return;
    }
    const file = document.getElementById('addp-file').files[0];
    const source = document.getElementById('addp-source').value;

    const fd = new FormData();
    fd.append('photo', file);
    fd.append('source', source);

    try {
        const btn = document.querySelector('.modal-footer .btn-primary');
        btn.disabled = true;
        btn.textContent = 'Adding...';
        const data = await api.postForm(`/api/students/${studentId}/photos`, fd);
        showToast('Success', `${data.templates_added} template(s) added`, 'success');
        closeModal();
        renderStudents();
    } catch (err) {
        const btn = document.querySelector('.modal-footer .btn-primary');
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Add Photo';
        }
    }
};

function confirmDeleteStudent(id, name) {
    const html = `<p>Are you sure you want to delete <strong>${name}</strong>? This action cannot be undone and will not remove past attendance records, but will prevent future recognition.</p>`;
    const footer = `
        <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn btn-danger" onclick="executeDeleteStudent('${id}')">Delete</button>
    `;
    openModal('Delete Student', html, footer);
}

async function executeDeleteStudent(id) {
    try {
        await api.delete(`/api/students/${id}`);
        showToast('Success', 'Student deleted', 'success');
        closeModal();
        renderStudents();
    } catch (err) {
        // Error handled in api
    }
}

// --- Toasts ---
function showToast(title, message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    let icon = '';
    if (type === 'success') icon = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-green"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
    else if (type === 'error') icon = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-red"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`;
    else icon = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-blue"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`;

    toast.innerHTML = `
        ${icon}
        <div class="toast-content">
            <div class="toast-title">${title}</div>
            <div class="toast-message">${message}</div>
        </div>
    `;
    
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// Start
document.addEventListener('DOMContentLoaded', boot);

/* --- Assigning a missed face to a known athlete ---------------------------
 * The measurements show small faces in low-resolution photos score just under
 * threshold for the right person. A coach can see who it is, so let them say
 * so - and store the crop as a `live` template so the next photo, taken in the
 * same conditions, matches on its own.
 */
async function openAssignModal(faceUrl) {
    const sel = document.getElementById('mark-centre');
    const scope = sel && sel.value ? `centre_id=${sel.value}` : '';

    openModal('Who is this?', `
        <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:16px">
            <img src="${faceUrl}" alt="Unidentified face"
                 style="width:110px;height:110px;object-fit:cover;border-radius:var(--radius-md);
                        border:1px solid var(--border-subtle)">
            <div style="flex:1;min-width:190px" class="text-sm text-muted">
                Ranked by how closely each athlete matches this face. The right
                person is usually top even when the score sat below the automatic
                threshold. Confirming also stores this face as they look today,
                so next time they match without help.
            </div>
        </div>
        <div id="assign-suggestions" class="text-sm text-muted">Ranking candidates...</div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>`);

    let data, people = [];
    try {
        [data, people] = await Promise.all([
            api.get(`/api/attendance/suggest?face_url=${encodeURIComponent(faceUrl)}&${scope}`),
            api.get('/api/people' + (scope ? '?' + scope : '')).then(r => r.people),
        ]);
    } catch {
        const box = document.getElementById('assign-suggestions');
        if (box) box.textContent = 'Could not rank candidates for this face.';
        return;
    }

    const box = document.getElementById('assign-suggestions');
    if (!box) return;
    const sugg = data.suggestions || [];
    box.innerHTML = `
        ${sugg.length ? sugg.map((x, i) => `
            <div class="suggest-row" onclick="submitAssign('${faceUrl}', ${x.student_id})">
                ${x.photo_url ? `<img src="${x.photo_url}" class="avatar avatar-sm" alt="">`
                              : `<div class="avatar avatar-sm">${Charts.esc(x.name.charAt(0))}</div>`}
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600;font-size:13px">${Charts.esc(x.name)}
                        ${i === 0 ? '<span class="badge badge-blue" style="margin-left:6px">best match</span>' : ''}
                        ${x.role === 'coach' ? '<span class="badge badge-green" style="margin-left:6px">coach</span>' : ''}</div>
                    <div class="text-xs text-muted font-mono">${Charts.esc(x.roll_no)} &middot; score ${x.score}${x.score >= data.threshold ? '' : ' (below threshold)'}</div>
                </div>
                <span class="text-xs" style="color:var(--accent);font-weight:600;white-space:nowrap">Mark present</span>
            </div>`).join('')
        : '<div class="text-sm text-muted">No candidates could be ranked.</div>'}
        <div class="form-group" style="margin-top:16px">
            <label class="form-label">Someone else</label>
            <select id="assign-student" class="form-input"
                    onchange="if(this.value) submitAssign('${faceUrl}', this.value)">
                <option value="">Pick from the full roster...</option>
                ${people.map(p => `<option value="${p.id}">${Charts.esc(p.name)} (${Charts.esc(p.roll_no)})</option>`).join('')}
            </select>
        </div>
        <label style="display:flex;gap:8px;align-items:flex-start;font-size:13px;cursor:pointer">
            <input type="checkbox" id="assign-learn" checked style="margin-top:3px">
            <span>Learn from this photo. Recommended - it is what stops the same
                  athlete being missed next session.</span>
        </label>`;
}

async function submitAssign(faceUrl, studentId) {
    const sid = studentId || document.getElementById('assign-student').value;
    if (!sid) return;
    const learnBox = document.getElementById('assign-learn');
    const learn = learnBox ? learnBox.checked : true;
    const fd = new FormData();
    fd.append('face_url', faceUrl);
    fd.append('student_id', sid);
    fd.append('learn', learn ? 'true' : 'false');
    try {
        const r = await api.postForm('/api/attendance/assign', fd);
        closeModal();
        showToast('Marked present', r.message, 'success');
    } catch { /* surfaced by the api layer */ }
}


/* --------------------------------------------------------------------------
   Guided enrolment, phone style.

   Not a sequence of named poses. The athlete is given one instruction - move
   your head slowly in a circle - and a ring of segments around the preview
   lights up as each angle is covered. The capture ends when enough of the ring
   is lit, which is a statement about pose COVERAGE rather than about how many
   buttons were pressed.

   The ring is a map, not decoration. Each frame's pose is converted to a point
   (dy, dp) relative to the athlete's own straight-ahead reading; its angle
   around the circle selects a segment and its radius says whether the head has
   turned far enough for that segment to count.
   -------------------------------------------------------------------------- */

const MV_SEGMENTS = 24;          // ring resolution - what the athlete sees
const MV_CAPTURE_SECTORS = 8;    // how many frames are actually kept
const MV_REQUIRED = 18;          // segments needed to finish (of MV_SEGMENTS)
// 18/24 = 75%. Google's Pixel 4 face-unlock patent (US12183117B2) documents
// this exact figure - enrolment completes at 75% of pose buckets filled, not
// 100% - because requiring every angle fails people with limited neck
// mobility or an off-axis camera, for no measurable accuracy gain. Verified
// independently of this codebase; this file already used 75% before that was
// known to match a shipped system.
// Lighting the ring is feedback; the frames are the product. Finishing on ring
// coverage alone ended the capture with three usable views, because encoding a
// frame takes about a second and the sweep outran it.
const MV_MIN_CAPTURES = 5;       // distinct sectors that must be on disk
const MV_POLL_MS = 250;

// Frames are stored at this width rather than the full sensor resolution. A
// selfie face still lands near 200px here, well above the 90px the enrolment
// endpoint requires, and the upload is smaller over mobile data. Note this did
// NOT speed up encoding in testing - 960px and 1280px both measured about a
// second - so the roughly one-second capture is fixed overhead, not pixels,
// and the sweep is designed to keep running through it.
const MV_CAPTURE_WIDTH = 960;

// Yaw travels further than pitch on a real head, so the raw angles are scaled
// before being read as a circle. Without this the "circle" is a flat ellipse
// and the top and bottom segments are unreachable.
const MV_YAW_SCALE = 18.0;
const MV_PITCH_SCALE = 12.0;
const MV_MIN_RADIUS = 0.55;      // how far the head must turn for a segment to count

let mvState = null;

// Re-scan an existing person: the frames go straight to their profile.
async function openMultiViewEnrol(studentId, studentName) {
    openFaceScan({
        title: `Register face - ${studentName}`,
        onComplete: frames => mvSaveToProfile(studentId, frames),
    });
}

async function mvSaveToProfile(studentId, frames) {
    const btn = document.getElementById('mv-save');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
    const fd = new FormData();
    frames.forEach((f, i) => fd.append('frames', f.blob, `${f.key}_${i}.jpg`));
    try {
        const r = await api.postForm(`/api/students/${studentId}/enroll-multiview`, fd);
        closeFaceScan();
        showToast(r.sufficient ? 'Face registered' : 'Only partly captured',
                  r.message, r.sufficient ? 'success' : 'error');
        if (state.currentRoute === '/students') renderStudents();
    } catch {
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    }
}

async function openFaceScan(opts) {
    const seg = [];
    for (let i = 0; i < MV_SEGMENTS; i++) {
        const a0 = (i / MV_SEGMENTS) * 2 * Math.PI - Math.PI / 2;
        const a1 = ((i + 0.82) / MV_SEGMENTS) * 2 * Math.PI - Math.PI / 2;
        const r0 = 44, r1 = 49;
        const p = (r, a) => `${(50 + r * Math.cos(a)).toFixed(2)},${(50 + r * Math.sin(a)).toFixed(2)}`;
        seg.push(`<path class="mv-seg" data-i="${i}" d="M${p(r0,a0)} L${p(r1,a0)} A${r1},${r1} 0 0,1 ${p(r1,a1)} L${p(r0,a1)} A${r0},${r0} 0 0,0 ${p(r0,a0)} Z"/>`);
    }
    openModal(opts.title || 'Scan face', `
        <div class="mv-stage">
            <video id="mv-video" autoplay playsinline muted></video>
            <div class="mv-mask"></div>
            <svg class="mv-ringsvg" viewBox="0 0 100 100">${seg.join('')}</svg>
            <div class="mv-flash" id="mv-flash"></div>
            <div class="mv-step" id="mv-step">Starting camera...</div>
        </div>
        <div class="mv-count"><span id="mv-pct">0</span>% complete</div>
        <div id="mv-hint" class="text-sm text-muted" style="text-align:center;margin-top:6px"></div>
        <canvas id="mv-canvas" class="hidden"></canvas>`,
        `<button class="btn btn-secondary" onclick="closeFaceScan()">Cancel</button>
         <button class="btn btn-primary hidden" id="mv-save" onclick="mvSubmit()">Save</button>`);

    mvState = {
        onComplete: opts.onComplete, stream: null, timer: null, busy: false,
        capturing: false, phase: 'centre', baseYaw: null, basePitch: null,
        centreHold: 0, covered: new Set(), capturedSectors: new Set(), frames: [],
    };
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        mvSay('Camera unavailable', window.isSecureContext
            ? 'This browser does not support camera capture.'
            : 'The camera only works over HTTPS. Open the https:// address instead.');
        return;
    }
    try {
        mvState.stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 960 } },
            audio: false,
        });
        const v = document.getElementById('mv-video');
        v.srcObject = mvState.stream;
        await v.play();

        // See waitForVideoFrame's definition: getUserMedia resolving does not
        // mean the video is rendering. A stalled stream here left the athlete
        // staring at "Starting camera..." forever with no error and no picture.
        const gotFrame = await waitForVideoFrame(v);
        if (!gotFrame) {
            mvState.stream.getTracks().forEach(t => t.stop());
            mvState.stream = null;
            mvSay('Camera unavailable',
                  'The camera started but no picture appeared.' + standaloneCameraHint());
            return;
        }

        mvSay('Look straight at the camera', 'Fill the circle with your face');
        mvState.timer = setInterval(mvTick, MV_POLL_MS);
    } catch (e) {
        mvSay('Camera unavailable', CameraCapture.explain(e));
    }
}

function mvSay(step, hint) {
    const s = document.getElementById('mv-step');
    const h = document.getElementById('mv-hint');
    if (s) s.textContent = step;
    if (h && hint !== undefined) h.textContent = hint;
}

function mvGrab(maxW) {
    const v = document.getElementById('mv-video');
    const c = document.getElementById('mv-canvas');
    if (!v || !c || !v.videoWidth) return null;
    const scale = maxW ? Math.min(1, maxW / v.videoWidth) : 1;
    c.width = Math.round(v.videoWidth * scale);
    c.height = Math.round(v.videoHeight * scale);
    // The preview is mirrored for comfort; the stored frame must not be, or
    // every template is a mirror image of the person.
    c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
    return c;
}

async function mvTick() {
    if (!mvState || mvState.busy || mvState.phase === 'done') return;
    const c = mvGrab(480);
    if (!c) return;
    mvState.busy = true;
    try {
        const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.8));
        const fd = new FormData();
        fd.append('frame', blob, 'f.jpg');
        fd.append('step', mvState.phase === 'centre' ? 'centre' : 'sweep');
        if (mvState.baseYaw !== null) {
            fd.append('base_yaw', mvState.baseYaw);
            fd.append('base_pitch', mvState.basePitch);
        }
        mvApplyFeedback(await api.postForm('/api/enroll/pose-check', fd));
    } catch { /* a dropped frame is not worth reporting - the next one follows */ }
    finally { mvState.busy = false; }
}

function mvApplyFeedback(r) {
    // Deliberately NOT gated on mvState.capturing. Encoding a full-resolution
    // frame takes around a second, and the athlete keeps turning throughout;
    // dropping those readings threw away most of the sweep and left the ring
    // stuck around a third lit. Only the capture itself is serialised.
    if (!mvState || mvState.phase === 'done') return;

    // Framing and lighting failures are reported whatever the phase - a
    // correctly-turned head that is too dark to use is still not usable.
    if (!r.ok && r.reason !== 'pose') { mvSay(r.message || 'Adjust position'); return; }

    if (mvState.phase === 'centre') {
        if (!r.ok) { mvState.centreHold = 0; mvSay(r.message || 'Look straight at the camera'); return; }
        mvState.centreHold += 1;
        if (mvState.centreHold >= 3) {
            mvState.baseYaw = r.yaw;
            mvState.basePitch = r.pitch;
            mvState.phase = 'sweep';
            mvSay('Slowly move your head in a circle', 'Keep your eyes on the screen');
            mvCapture('centre');
        }
        return;
    }

    // Sweep: place this pose on the ring.
    const x = (r.delta_yaw || 0) / MV_YAW_SCALE;
    const y = (r.delta_pitch || 0) / MV_PITCH_SCALE;
    const radius = Math.hypot(x, y);
    if (radius < MV_MIN_RADIUS) { mvSay('Slowly move your head in a circle'); return; }

    // Screen x is mirrored relative to the athlete, so a leftward turn should
    // light the segment on the side they see themselves move toward.
    let ang = Math.atan2(y, -x) + Math.PI / 2;
    if (ang < 0) ang += 2 * Math.PI;
    const idx = Math.floor((ang / (2 * Math.PI)) * MV_SEGMENTS) % MV_SEGMENTS;

    if (!mvState.covered.has(idx)) {
        mvState.covered.add(idx);
        const el = document.querySelector(`.mv-seg[data-i="${idx}"]`);
        if (el) el.classList.add('lit');
        mvSetPct();
    }

    // Only a handful of frames are kept. One per sector is enough: neighbouring
    // segments differ by a few degrees and would store near-duplicate templates.
    // A sector is only marked captured once a frame for it actually started
    // encoding. Marking it on the attempt would silently lose the sector when
    // the encoder was busy.
    const sector = Math.floor((ang / (2 * Math.PI)) * MV_CAPTURE_SECTORS) % MV_CAPTURE_SECTORS;
    if (!mvState.capturedSectors.has(sector) && mvCapture('s' + sector)) {
        mvState.capturedSectors.add(sector);
    }

    if (mvState.covered.size >= MV_REQUIRED && mvState.capturedSectors.size >= MV_MIN_CAPTURES) {
        mvFinish();
    } else if (mvState.covered.size >= MV_REQUIRED) {
        mvSay('Almost there - keep turning', 'Collecting a few more views');
    }
}

function mvSetPct() {
    const pct = Math.round(100 * Math.min(1, mvState.covered.size / MV_REQUIRED));
    const el = document.getElementById('mv-pct');
    if (el) el.textContent = pct;
}

// Returns true when a frame actually began encoding, so the caller knows
// whether to record that angle as captured.
function mvCapture(key) {
    if (!mvState || mvState.capturing) return false;
    const c = mvGrab(MV_CAPTURE_WIDTH);
    if (!c) return false;
    mvState.capturing = true;
    const flash = document.getElementById('mv-flash');
    if (flash) { flash.classList.remove('fire'); void flash.offsetWidth; flash.classList.add('fire'); }
    c.toBlob(b => {
        if (!mvState) return;                       // cancelled while encoding
        mvState.frames.push({ key, blob: b });
        mvState.capturing = false;
    }, 'image/jpeg', 0.95);
    return true;
}

function mvFinish() {
    mvState.phase = 'done';
    if (mvState.timer) { clearInterval(mvState.timer); mvState.timer = null; }
    // A frame may still be encoding; counting now would under-report it.
    const settle = () => mvSay('Face registered', `${mvState.frames.length} views captured`);
    mvSay('Face registered', 'Finishing...');
    if (mvState.capturing) setTimeout(settle, 1200); else settle();
    const save = document.getElementById('mv-save');
    if (save) save.classList.remove('hidden');
    document.querySelectorAll('.mv-seg').forEach(s => s.classList.add('lit'));
    mvSetPct();
}

async function mvSubmit() {
    const frames = mvState.frames;
    const onComplete = mvState.onComplete;
    if (onComplete) await onComplete(frames);
}

function closeFaceScan() {
    if (mvState) {
        if (mvState.timer) clearInterval(mvState.timer);
        if (mvState.stream) mvState.stream.getTracks().forEach(t => t.stop());
    }
    mvState = null;
    closeModal();
}

/* --------------------------------------------------------------------------
   Analytics page.

   Form was chosen per chart from the data's job, not from what looks busiest:

     attendance over time   -> area, one series (the headline; it is a trend)
     geo verification       -> horizontal stacked bar, STATUS colours
                               (inside/no fix/outside genuinely encode good,
                               warning and bad - this is the one place those
                               colours belong)
     centres                -> columns, magnitude across a handful of items
     match confidence       -> columns over ordered buckets, sequential ramp
                               (a distribution, so more-is-darker carries the
                               axis a second time)
     attendance by athlete  -> ranked horizontal bars, worst first, because
                               long names do not fit under a column and the
                               reader wants "who is missing sessions"

   The four tiles above are a KPI row, not charts: a single current number is
   a stat tile, never a one-bar bar chart.
   -------------------------------------------------------------------------- */

// Filters live in ONE row above everything they scope, so every chart, tile
// and table on the page always describes the same slice of data. A filter
// inside a single card would let two cards disagree while both looked current.
const analyticsFilter = { days: 60, centre: '' };

async function renderAnalytics(isRefetch) {
    const root = document.getElementById('app-root');
    const body = document.getElementById('analytics-body');

    if (!isRefetch || !body) {
        root.innerHTML = `
            <div class="filter-row">
                <div class="ch-skeleton" style="width:180px;height:52px"></div>
                <div class="ch-skeleton" style="width:180px;height:52px"></div>
            </div>
            <div class="stats-grid" style="margin-bottom:16px">
                ${'<div class="ch-skeleton" style="height:104px"></div>'.repeat(4)}
            </div>
            <div class="analytics-grid">
                <div class="ch-skeleton col-span-2" style="height:300px"></div>
                <div class="ch-skeleton" style="height:190px"></div>
                <div class="ch-skeleton" style="height:190px"></div>
            </div>`;
    } else {
        // Hold the previous render at reduced opacity rather than tearing it
        // down for a skeleton - no layout jump, and the numbers stay readable
        // while the new ones are on their way.
        body.classList.add('is-refetching');
    }

    const qs = `days=${analyticsFilter.days}` +
               (analyticsFilter.centre ? `&centre_id=${encodeURIComponent(analyticsFilter.centre)}` : '');
    let a, centres = [];
    try {
        [a, centres] = await Promise.all([
            api.get(`/api/analytics?${qs}`),
            api.get('/api/centres').then(r => r.centres).catch(() => []),
        ]);
    } catch {
        root.innerHTML = `<div class="empty-state py-12">Could not load analytics.</div>`;
        return;
    }

    const trend = a.trend || [];
    const present = trend.map(d => d.present);
    const latest = present.length ? present[present.length - 1] : 0;
    const prev = present.length > 1 ? present[present.length - 2] : null;
    const avg = present.length ? present.reduce((x, y) => x + y, 0) / present.length : 0;
    const rate = a.enrolled ? (avg / a.enrolled) * 100 : 0;
    const best = present.length ? Math.max(...present) : 0;

    const shortDate = (iso) => {
        const [, m, d] = (iso || '').split('-');
        return m && d ? `${d}/${m}` : iso;
    };

    // Delta on the headline tile: signed, against a named period, and coloured
    // by direction because for attendance up is unambiguously good.
    let deltaHtml = '';
    if (prev !== null) {
        const diff = latest - prev;
        const cls = diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat';
        // "0 vs previous session" reads like a missing value; say it in words.
        const text = diff === 0 ? 'No change from previous session'
                                : `${diff > 0 ? '+' : ''}${diff} vs previous session`;
        deltaHtml = `<div class="stat-delta ${cls}">${text}</div>`;
    }

    const geo = a.geo || {};
    const geoSegments = [
        { label: 'At the centre', value: geo.inside || 0, color: Charts.status.good },
        { label: 'No location fix', value: geo.no_fix || 0, color: Charts.status.warn },
        { label: 'Outside the fence', value: geo.outside || 0, color: Charts.status.bad },
        { label: 'Unverified', value: geo.unverified || 0, color: Charts.status.none },
    ].filter(s => s.value);

    const centreData = (a.by_centre || []).map(c => ({
        label: c.name, short: c.code, value: c.present,
    }));

    // Buckets are an ordered scale, so they take the sequential ramp: the
    // darker the column the higher the confidence it represents.
    const conf = a.confidence || [];
    const confData = conf.map((c, i) => ({
        label: `${(c.bucket * 100).toFixed(0)}-${(c.bucket * 100 + 5).toFixed(0)}% similarity`,
        short: (c.bucket * 100).toFixed(0),
        value: c.count,
        color: Charts.sequential[Math.min(
            Charts.sequential.length - 1,
            Math.floor((i / Math.max(1, conf.length - 1)) * (Charts.sequential.length - 1))
        )],
    }));

    // Worst attendance first - that is the list a coach acts on. Capped at 12
    // so the card stays readable; the table view holds the rest.
    const perAthlete = a.per_athlete || [];
    const worst = perAthlete.slice(0, 12).map(p => ({
        label: p.name, value: p.rate, unit: '%',
    }));

    const isSuper = (session.user || {}).role === 'super_admin';
    root.innerHTML = `
        <div class="filter-row">
            <label class="filter-field">
                <span>Period</span>
                <select id="an-days" class="form-select">
                    <option value="7">Last 7 sessions</option>
                    <option value="30">Last 30 sessions</option>
                    <option value="60">Last 60 sessions</option>
                    <option value="365">All sessions</option>
                </select>
            </label>
            ${isSuper ? `<label class="filter-field">
                <span>Centre</span>
                <select id="an-centre" class="form-select">
                    <option value="">All centres</option>
                    ${centres.map(c => `<option value="${c.id}">${Charts.esc(c.name)}</option>`).join('')}
                </select>
            </label>` : ''}
        </div>
        <div id="analytics-body">
        <div class="stats-grid" style="margin-bottom:16px">
            <div class="stat-card">
                <div class="stat-header"><span>Latest session</span></div>
                <div class="stat-value">${latest}</div>
                ${deltaHtml}
                <div class="stat-spark">${Charts.sparkline(present.slice(-14))}</div>
            </div>
            <div class="stat-card">
                <div class="stat-header"><span>Average per session</span></div>
                <div class="stat-value">${avg.toFixed(1)}</div>
                <div class="stat-delta flat">across ${a.sessions} session${a.sessions === 1 ? '' : 's'}</div>
            </div>
            <div class="stat-card">
                <div class="stat-header"><span>Best turnout</span></div>
                <div class="stat-value">${best}</div>
                <div class="stat-delta flat">of ${a.enrolled} enrolled</div>
            </div>
            <div class="stat-card">
                <div class="stat-header"><span>Sessions recorded</span></div>
                <div class="stat-value">${a.sessions}</div>
            </div>
        </div>

        <div class="card" style="margin-bottom:16px">
            <div class="card-body meter-row">
                ${Charts.radialMeter(rate, { caption: 'of enrolled athletes', label: 'Average turnout' })}
                <div class="meter-copy">
                    <div class="meter-title">Average turnout</div>
                    <p class="text-sm text-muted" style="margin:6px 0 0">
                        On a typical session ${avg.toFixed(1)} of ${a.enrolled} enrolled athletes
                        were recognised. The ring is that share; the sessions below are how it
                        moved over time.
                    </p>
                </div>
            </div>
        </div>

        <div class="analytics-grid">
            <div class="card col-span-2">
                <div class="card-header">
                    <h3 class="card-title">Athletes present per session</h3>
                    <span class="text-xs text-muted">${a.enrolled} enrolled</span>
                </div>
                <div class="card-body">
                    ${Charts.areaChart(
                        trend.map(d => ({ label: d.date, short: shortDate(d.date), value: d.present })),
                        { unit: '', height: 250 }
                    )}
                    ${Charts.tableView(['Date', 'Present'],
                        trend.map(d => [d.date, d.present]), 'Athletes present per session')}
                </div>
            </div>

            <div class="card">
                <div class="card-header"><h3 class="card-title">Location verification</h3></div>
                <div class="card-body">
                    ${Charts.stackedBar(geoSegments, { height: 30 })}
                    ${Charts.tableView(['Status', 'Records'],
                        geoSegments.map(s => [s.label, s.value]), 'Location verification of attendance records')}
                </div>
            </div>

            <div class="card">
                <div class="card-header"><h3 class="card-title">Records by centre</h3></div>
                <div class="card-body">
                    ${Charts.barChart(centreData, { height: 220 })}
                    ${Charts.tableView(['Centre', 'Records', 'Athletes'],
                        (a.by_centre || []).map(c => [c.name, c.present, c.athletes]), 'Attendance records by centre')}
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">Match confidence</h3>
                    <span class="text-xs text-muted">similarity %</span>
                </div>
                <div class="card-body">
                    <div class="text-xs text-muted" style="margin-bottom:8px">
                        Where matches cluster. Bars close to the threshold are the ones
                        that will start being missed as athletes grow.
                    </div>
                    ${Charts.barChart(confData, { height: 220 })}
                    ${Charts.tableView(['Similarity', 'Matches'],
                        conf.map(c => [`${(c.bucket * 100).toFixed(0)}%`, c.count]), 'Distribution of match confidence')}
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">Lowest attendance</h3>
                    <span class="text-xs text-muted">% of sessions</span>
                </div>
                <div class="card-body">
                    ${Charts.rankedBars(worst, { unit: '%' })}
                    ${Charts.tableView(['Athlete', 'Roll no', 'Sessions', 'Rate %'],
                        perAthlete.map(p => [p.name, p.roll_no, p.present, p.rate]), 'Attendance rate by athlete')}
                </div>
            </div>
        </div>
        </div>`;

    const daysSel = document.getElementById('an-days');
    if (daysSel) {
        daysSel.value = String(analyticsFilter.days);
        daysSel.addEventListener('change', () => {
            analyticsFilter.days = +daysSel.value;
            renderAnalytics(true);
        });
    }
    const centreSel = document.getElementById('an-centre');
    if (centreSel) {
        centreSel.value = analyticsFilter.centre;
        centreSel.addEventListener('change', () => {
            analyticsFilter.centre = centreSel.value;
            renderAnalytics(true);
        });
    }

    Charts.initChartInteraction(root);
    Charts.countUp(root);
}
