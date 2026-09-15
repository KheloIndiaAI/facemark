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
            // Mirror the PREVIEW for the front camera only. The overlay's box
            // and landmark dots flip to match (see openClipCapture.draw), so
            // the two must agree - a mirrored overlay over an unmirrored video
            // tracks the wrong way the moment the head moves. Attendance uses
            // the rear camera and is left alone.
            this._applyMirror();

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
    
    /** Keep the preview's mirroring in step with the facing mode. */
    _applyMirror() {
        if (!this.video || !this.video.classList) return;
        this.video.classList.toggle('mirrored', this.facingMode === 'user');
    }

    // Safari records MP4/H.264, Chrome and Firefox WebM/VP8-9. Both decode
    // server-side through the same FFmpeg backend, so the first type this
    // browser supports wins rather than forcing one and failing on the other.
    static pickMimeType() {
        if (!window.MediaRecorder) return null;
        const candidates = [
            'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm',
            'video/mp4;codecs=avc1', 'video/mp4',
        ];
        for (const t of candidates) {
            try { if (MediaRecorder.isTypeSupported(t)) return t; } catch { /* older browsers throw */ }
        }
        return '';
    }

    /** Record a short clip from the live stream.
     *
     * The clip is what proves the subject is a person rather than a photograph:
     * a picture on a screen is flat, so everything in it moves as one plane,
     * and the server measures that. A still frame cannot show it.
     *
     * @param {number} ms      how long to record
     * @param {function} onTick called with 0..1 progress, for the UI ring
     */
    /**
     * @param ms       safety ceiling - recording stops here even if `control`
     *                 never signals done. For a fixed-length capture (attendance)
     *                 this IS the duration; for a gated capture it is a backstop.
     * @param onTick   called with 0..1 = elapsed/ms. Meaningless for a gated
     *                 capture with no fixed target, so gated callers pass null
     *                 and drive their own progress indicator instead.
     * @param control  optional mutable {done: false}. The caller flips
     *                 control.done = true to end the recording before `ms`
     *                 elapses - this is what makes "stop once every instruction
     *                 is verified complete" possible instead of a blind timer.
     */
    async recordClip(ms = 2000, onTick = null, control = null) {
        if (!this.isActive || !this.stream) return null;
        if (!window.MediaRecorder) {
            showToast('Recording unavailable',
                      'This browser cannot record video. Update it, or open the app in Chrome or Safari.',
                      'error');
            return null;
        }
        const mime = CameraCapture.pickMimeType();
        let rec;
        try {
            // An explicit bitrate. Left to itself a phone encoder drops quality
            // hard indoors while the head moves, exactly when the server must
            // find the face. 4Mbps keeps even the 40s longest guided clip near
            // 20MB, inside the app's 25MB and the proxy's 30MB upload caps.
            const bits = { videoBitsPerSecond: 4000000 };
            rec = mime ? new MediaRecorder(this.stream, { mimeType: mime, ...bits })
                       : new MediaRecorder(this.stream, bits);
        } catch (err) {
            showToast('Recording unavailable', 'The camera stream could not be recorded.', 'error');
            return null;
        }

        const chunks = [];
        rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
        const stopped = new Promise(resolve => { rec.onstop = resolve; });

        if (navigator.vibrate) navigator.vibrate(40);
        try {
            rec.start();
        } catch (err) {
            showToast('Recording unavailable', 'The camera could not start recording.', 'error');
            return null;
        }

        const t0 = Date.now();
        // A timer rather than requestAnimationFrame: rAF is paused when the tab
        // is not visible, which would leave a recording running with a frozen
        // progress ring and no way to end it.
        await new Promise(resolve => {
            const tick = setInterval(() => {
                const p = Math.min(1, (Date.now() - t0) / ms);
                if (onTick) onTick(p);
                if (p >= 1 || (control && control.done)) { clearInterval(tick); resolve(); }
            }, 50);
        });

        try { rec.stop(); } catch { /* already stopped */ }
        await stopped;

        if (!chunks.length) {
            showToast('Nothing recorded', 'The camera produced no video. Try again.', 'error');
            return null;
        }
        const type = (mime || 'video/webm').split(';')[0];
        const ext = type.includes('mp4') ? 'mp4' : 'webm';
        const blob = new Blob(chunks, { type });
        const file = new File([blob], `clip_${Date.now()}.${ext}`, { type });
        file.source = `camera_${this.facingMode}`;
        file.isClip = true;
        return file;
    }

    _startQualityMonitor() {
        if (this.qualityTimer) clearInterval(this.qualityTimer);
        if (!this.qualityBadge) return;

        // This used to write "Good Lighting" every second regardless of the
        // frame, with a comment admitting it was a placeholder. In a product
        // whose whole job is telling a coach when a photo cannot be trusted, an
        // indicator that always says "good" is worse than none - it is a
        // confident wrong answer. It now measures the frame.
        const probe = document.createElement('canvas');
        probe.width = 64; probe.height = 48;
        const pctx = probe.getContext('2d', { willReadFrequently: true });

        this.qualityTimer = setInterval(() => {
            if (!this.isActive || !this.video.videoWidth) return;
            let mean = 0, spread = 0;
            try {
                pctx.drawImage(this.video, 0, 0, probe.width, probe.height);
                const d = pctx.getImageData(0, 0, probe.width, probe.height).data;
                let sum = 0, sumSq = 0, n = 0;
                // Luma, sampled: enough to tell dark from blown out.
                for (let i = 0; i < d.length; i += 16) {
                    const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
                    sum += y; sumSq += y * y; n++;
                }
                mean = sum / n;
                spread = Math.sqrt(Math.max(0, sumSq / n - mean * mean));
            } catch {
                return;                       // a tainted or not-yet-ready frame
            }

            let label, colour;
            if (mean < 45)        { label = 'Too dark';        colour = '#f87171'; }
            else if (mean > 215)  { label = 'Too bright';      colour = '#f87171'; }
            else if (spread < 18) { label = 'Low contrast';    colour = '#fbbf24'; }
            else                  { label = 'Lighting OK';     colour = '#34d399'; }
            this.qualityBadge.textContent = label;
            this.qualityBadge.style.color = colour;
        }, 1000);
    }
}

// --- Router ---
function initRouter() {
    window.addEventListener('hashchange', handleRoute);
    handleRoute();
}

function handleRoute() {
    // Register is the landing page: taking the register - an athlete marking
    // themselves, or a coach adding one by hand - is the job the app exists
    // for and the one people open it to do. The dashboard reports on work
    // already done, which is a second question, not the first.
    // Role-aware default. An athlete has no business on the coach's register
    // screen, and landing there is how someone concludes the app is not for
    // them. Coaches and admins keep Register as the first thing they see.
    // Kept in step with the sign-in redirect in auth.js: athletes land on their
    // own page, super admins (no register of their own) on the Dashboard.
    const home = (typeof isAthlete === 'function' && isAthlete()) ? '/me'
        : (typeof isSuperAdmin === 'function' && isSuperAdmin()) ? '/dashboard'
        : '/register';
    let hash = window.location.hash.slice(1) || home;

    // Close any open dialog. The capture modal tears its own stream down
    // when it hides - closeModal's comment says exactly that - but nothing was
    // hiding it on a route change, so navigating away from a live capture left
    // the dialog and its camera running behind the next page, with the phone's
    // camera light on and no visible way back to it.
    const openModalEl = document.getElementById('modal-container');
    if (openModalEl && !openModalEl.classList.contains('hidden')) closeModal();

    // Default route
    if (hash === '/') hash = home;

    // Role gate. `home` only chose a starting point; typing the URL walked
    // straight past it. The server is the real boundary - every one of these
    // routes' endpoints is staff-only now - but a page that loads and then
    // fails every request is a worse answer than not opening it.
    const STAFF_ROUTES = ['/dashboard', '/oversight', '/register', '/take-attendance',
                          '/students', '/centres', '/users'];
    if (typeof isAthlete === 'function' && isAthlete() && STAFF_ROUTES.includes(hash)) {
        window.location.hash = '#' + home;
        return;
    }
    
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
        const tpl = document.getElementById('tpl-dashboard').content.cloneNode(true);
        root.appendChild(tpl);
        renderDashboard();
    }
    else if (hash === '/oversight') {
        title.textContent = 'Oversight';
        const tpl = document.getElementById('tpl-oversight').content.cloneNode(true);
        root.appendChild(tpl);
        initOversightPage();
    }
    else if (hash === '/me') {
        title.textContent = 'My attendance';
        const tpl = document.getElementById('tpl-me').content.cloneNode(true);
        root.appendChild(tpl);
        initMePage();
    }
    else if (hash === '/register') {
        title.textContent = 'Register';
        const tpl = document.getElementById('tpl-register').content.cloneNode(true);
        root.appendChild(tpl);
        initRegisterPage();
    }
    else if (hash === '/take-attendance') {
        title.textContent = 'Take Attendance';
        root.appendChild(document.getElementById('tpl-take-attendance').content.cloneNode(true));
        initTakeAttendancePage();
    }
    else if (hash === '/students') {
        title.textContent = 'Directory';
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
    else if (hash === '/analytics' || hash === '/records') {
        // Analytics was removed; Records moved to the foot of Mark Attendance.
        // Redirected rather than left to fall through, so an old bookmark, an
        // installed PWA shortcut or a back button lands somewhere useful
        // instead of bouncing through the unknown-route branch.
        window.location.hash = '#' + home;
        return;
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
        if (!isSuperAdmin()) { window.location.hash = '#' + home; return; }
        title.textContent = 'Accounts';
        actions.innerHTML = `<button class="btn btn-primary" onclick="openAddUserModal()">Create account</button>`;
        root.appendChild(document.getElementById('tpl-users').content.cloneNode(true));
        renderUsersPage();
    }
    else {
        // Unknown route - e.g. a bookmark to a page that no longer exists.
        // Fall back to the landing page rather than leaving the shell blank -
        // the caller's landing page, not the coach's.
        window.location.hash = '#' + home;
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
    // `method` exists because the register toggle is a PATCH. Without it the
    // third argument was silently ignored and every toggle POSTed to a route
    // that only accepts PATCH, which is a 405 the caller reports as a generic
    // failure.
    /* `quiet` suppresses the error toast and only throws.
     *
     * Right for a button somebody pressed: they deserve to know it failed.
     * Wrong for a POLL - the camera guide calls this several times a second,
     * so one bad minute stacks dozens of identical toasts over the very screen
     * being used. Those loops already count failures and report a run of them
     * once; they just had no way to stop this layer shouting first. */
    async postForm(endpoint, formData, method = 'POST', quiet = false) {
        try {
            const res = await fetch(endpoint, {
                method,
                body: formData
            });
            if (res.status === 401) { handleUnauthorized(); throw new Error('Unauthorized'); }
            const data = await res.json();
            if (!res.ok) {
                // The STATUS travels with the error. Without it a caller sees
                // only a sentence and cannot tell "you are not allowed" from
                // "slow down" from "the server broke" from "the network is
                // gone" - and the camera framing loop reported all four as a
                // lost connection because that was all it could distinguish.
                const err = new Error(data.detail || 'API Error');
                err.status = res.status;
                throw err;
            }
            return data;
        } catch (err) {
            if (!quiet && err.message !== 'Unauthorized') showToast('Error', err.message, 'error');
            throw err;
        }
    },
    /* Same shape as postForm: it reports the SERVER's reason, and `quiet` lets
     * a caller that shows its own message stop this one firing too.
     *
     * It used to throw a flat 'API Error' and toast 'Failed to delete', so
     * "You cannot delete your own account" - a thing somebody would want to
     * read - arrived as three words that explain nothing, twice. */
    async delete(endpoint, quiet = false) {
        try {
            const res = await fetch(endpoint, { method: 'DELETE' });
            if (res.status === 401) { handleUnauthorized(); throw new Error('Unauthorized'); }
            let data = null;
            try { data = await res.json(); } catch { /* some deletes return no body */ }
            if (!res.ok) throw new Error((data && data.detail) || `Failed (${res.status})`);
            return data;
        } catch (err) {
            if (!quiet && err.message !== 'Unauthorized') {
                showToast('Error', err.message, 'error');
            }
            throw err;
        }
    }
};

// --- Application Init ---
/* Resolve a device location, resolving null rather than rejecting. */
// Local calendar date as YYYY-MM-DD.
//
// NOT toISOString().split('T')[0], which is the UTC date: in IST that is
// yesterday between midnight and 05:30, so an early-morning session - which is
// most of them here - would open the register on the wrong day and show it
// empty.
function localISODate(d = new Date()) {
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

async function boot() {
    document.getElementById('login-form').addEventListener('submit', doLogin);
    const user = await checkSession();
    if (!user) { showLogin(); return; }
    hideLogin();
    applyRoleChrome();
    await initApp();
}

let _appInitialised = false;

async function initApp() {
    // Called from BOTH boot() and doLogin(), so signing out and back in used to
    // install a second health interval and a second hashchange listener - the
    // router then re-rendered every navigation twice, and it compounded on
    // every re-login. The work below is one-time setup; only the routing needs
    // to happen again, so that is all that runs on a repeat call.
    if (_appInitialised) {
        handleRoute();
        return;
    }
    _appInitialised = true;

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

/* An earlier register the coach marked attendance on but never submitted.
   It comes first: the coach is reminded everywhere they would take
   attendance, the Register page opens it before today's, and Take Attendance
   will not scan until it is submitted. */
async function loadPendingRegister() {
    if (typeof isCoach !== 'function' || !isCoach()) return null;
    try {
        const r = await api.get('/api/pending-register');
        return r.pending || null;
    } catch {
        return null;
    }
}

function prettyDay(iso) {
    const d = new Date(`${iso}T00:00:00`);
    return isNaN(d) ? iso : d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' });
}

async function renderPendingBanner(hostId, withButton = true) {
    const host = document.getElementById(hostId);
    const p = await loadPendingRegister();
    if (!host) return p;
    if (!p) { host.innerHTML = ''; return null; }
    host.innerHTML = `
        <div style="background:#fffbeb;border:1px solid #fcd34d;border-left:4px solid #f59e0b;
                    border-radius:var(--radius-md, 10px);padding:14px 16px;margin-bottom:16px">
            <div style="font-weight:600;margin-bottom:4px">
                Your register for ${Charts.esc(prettyDay(p.date))} was not submitted</div>
            <div class="text-sm" style="margin-bottom:${withButton ? '10px' : '0'}">
                ${p.marked} ${p.marked === 1 ? 'athlete was' : 'athletes were'} marked on it.
                Submit it first, then take today's attendance.</div>
            ${withButton ? `<button type="button" class="btn btn-primary" onclick="window.location.hash='#/register'">
                Submit the ${Charts.esc(prettyDay(p.date))} register</button>` : ''}
        </div>`;
    return p;
}

// --- Dashboard ---

/* The one dashboard card that depends on who is looking: a coach sees which of
   their athletes have no attendance today; the super admin sees attendance
   that was added after a register was submitted, centre by centre. */
async function renderDashboardRoleCard(hostId = 'dashboard-role-card') {
    const host = document.getElementById(hostId);
    if (!host) return;
    const E = Charts.esc;
    const card = (title, sub, body) => `
        <div class="card" style="margin-bottom:16px">
            <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                <h3 class="card-title">${E(title)}</h3>
                <span class="text-xs text-muted">${E(sub)}</span>
            </div>
            <div class="card-body" style="overflow-x:auto">${body}</div>
        </div>`;
    const time = s => (s || '').replace('T', ' ').slice(11, 16) || '-';
    try {
        if (isCoach()) {
            const r = await api.get('/api/dashboard/absent');
            const sub = !r.register_status ? 'No register opened today yet'
                : r.register_status === 'submitted' ? 'Register submitted'
                : 'Register open - not submitted yet';
            // Everyone on the roster, not just the absentees: P for anybody with
            // attendance on today's register, "Not marked" for everybody else -
            // listed first, since they are the ones that need chasing.
            const everyone = (r.athletes || []).slice()
                .sort((x, y) => (x.present - y.present) || x.name.localeCompare(y.name));
            const body = !r.roster_count
                ? '<div class="empty-state">No athletes are registered at your centre yet.</div>'
                : `<table class="data-table">
                    <thead><tr><th>Athlete</th><th>Roll no</th><th>Sport</th><th>Today</th><th>Last attended</th></tr></thead>
                    <tbody>${everyone.map(a => `<tr>
                        <td class="cell-primary" data-label="Athlete">${E(a.name)}</td>
                        <td class="font-mono text-sm" data-label="Roll no">${E(a.roll_no || '-')}</td>
                        <td data-label="Sport">${E(a.sport || '-')}</td>
                        <td data-label="Today">${a.present
                            ? '<span class="badge badge-green">P</span>'
                            : '<span class="badge badge-red">Not marked</span>'}</td>
                        <td data-label="Last attended">${E(a.present ? 'Today' : (a.last_attended || 'Never'))}</td>
                    </tr>`).join('')}</tbody></table>`;
            host.innerHTML = card(
                `Today's attendance - ${r.present_count} present, ${r.absent_count} not marked`, sub, body);
        } else if (isSuperAdmin()) {
            const r = await api.get('/api/late-additions');
            const summary = !r.centres.length
                ? '<div class="empty-state">No attendance was added late today.</div>'
                : `<table class="data-table">
                    <thead><tr><th>Centre</th><th>Late additions</th><th>Registers</th></tr></thead>
                    <tbody>${r.centres.map(c => `<tr>
                        <td class="cell-primary" data-label="Centre">${E(c.centre_name)}</td>
                        <td data-label="Late additions">${c.count}</td>
                        <td data-label="Registers">${c.registers}</td>
                    </tr>`).join('')}</tbody></table>`;
            const detail = !r.rows.length ? '' : `
                <table class="data-table" style="margin-top:12px">
                    <thead><tr><th>Athlete</th><th>Centre</th><th>Register of</th><th>Added by</th><th>Submitted at</th><th>Added at</th></tr></thead>
                    <tbody>${r.rows.map(x => `<tr>
                        <td class="cell-primary" data-label="Athlete">${E(x.athlete_name)}</td>
                        <td data-label="Centre">${E(x.centre_name || '-')}</td>
                        <td data-label="Register of">${E(x.coach_name || 'Centre sweep')}</td>
                        <td data-label="Added by">${E(x.added_by || '-')}</td>
                        <td data-label="Submitted at">${E(time(x.submitted_at))}</td>
                        <td data-label="Added at">${E(time(x.marked_at))}</td>
                    </tr>`).join('')}</tbody></table>`;
            host.innerHTML = card(`Late attendance submitted (${r.total})`,
                                  `Added after the register was submitted · ${r.date}`,
                                  summary + detail);
        } else {
            host.innerHTML = '';
        }
    } catch {
        host.innerHTML = '';
    }
}

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
                ${stats.marked_today ? `
                <a href="#/register" class="text-xs" style="display:block;margin-top:6px;color:#b45309;font-weight:500">
                    +${stats.marked_today} marked, not yet confirmed - submit the register to count them
                </a>` : ''}
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
        renderDashboardRoleCard();
        renderPendingBanner('dashboard-pending');

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

        // Last 7 calendar days, not the last 7 sessions. A week with four
        // sessions must show three empty days: collapsing it to four bars would
        // draw an unbroken week and hide exactly the gap worth seeing.
        const weekBox = document.getElementById('dashboard-week');
        if (weekBox) {
            const byDate = new Map(trend.map(d => [d.date, d.present]));
            const week = [];
            for (let i = 6; i >= 0; i--) {
                const dt = new Date();
                dt.setDate(dt.getDate() - i);
                const iso = localISODate(dt);
                week.push({
                    label: iso,
                    short: dt.toLocaleDateString(undefined, { weekday: 'short' }),
                    value: byDate.get(iso) || 0,
                });
            }
            weekBox.innerHTML =
                Charts.barChart(week, { height: 180, empty: 'No attendance in the last 7 days' })
                + Charts.tableView(['Date', 'Present'],
                    week.map(d => [d.label, d.value]), 'Attendance, last 7 days');
            Charts.initChartInteraction(document.getElementById('app-root'));
        }

        // Render Recent Activity
        const recentHtml = stats.recent.length === 0 ?
            '<div class="p-4 text-center text-muted">No recent activity</div>' :
            stats.recent.map(r => `
                <div class="activity-item">
                    <div class="avatar">${Charts.esc(String(r.name || '').charAt(0))}</div>
                    <div class="activity-details">
                        <div class="activity-name">${Charts.esc(r.name)}</div>
                        <div class="activity-sub">${Charts.esc(r.roll_no)}</div>
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


/** The liveness verdict, with the frames it was decided from.
 *
 * "inconclusive" is deliberately not styled as a failure: it means the clip
 * carried no depth information either way, usually because nothing moved, and
 * telling someone they were rejected when they were not is its own bug.
 */
function livenessBanner(l, message) {
    if (!l) return '';
    const kind = l.verdict === 'screen' ? 'bad'
               : l.verdict === 'live'   ? 'good' : 'warn';
    // Titled from the server's CODE, not guessed from the verdict. "Could not
    // confirm this was live" is the right headline for a clip nobody moved,
    // and the wrong one for a group across a hall - which the check does not
    // reach at all, and which is not a failure the coach can do anything about.
    const title = l.verdict === 'live'    ? 'Live capture confirmed'
                : l.verdict === 'screen'  ? 'This looks like a screen, not a person'
                : l.verdict === 'no_face' ? 'No face in the clip'
                : l.code === 'too_far'    ? 'Too far away to check'
                : l.code === 'no_motion'  ? 'The camera did not move'
                : l.code === 'no_detail'  ? 'Could not follow the face'
                : 'Could not confirm this was live';
    const frames = (l.frame_urls || []).map(u =>
        `<img src="${Charts.esc(u)}" alt="Frame from the clip">`).join('');

    // What was actually measured, on a refusal. Without this a rejection is
    // unfalsifiable - the coach cannot tell "you barely moved" from "the
    // threshold is wrong", and neither can anyone debugging it later.
    //
    // THE ADVICE IS THE SERVER'S. This used to add its own line, picked from
    // the numbers, and it contradicted the message printed directly above it:
    // a clip refused for being too distant was answered with "turn your head
    // slowly left and right", which is not what happened and not something the
    // person could act on. The server knows which of six reasons it was; the
    // browser was guessing between two. Only the measurements are added here,
    // because those cannot disagree with anything.
    let detail = '';
    if (l.verdict === 'screen' || l.verdict === 'inconclusive') {
        const bits = [];
        if (typeof l.depth_score === 'number') bits.push(`depth ${l.depth_score}`);
        if (typeof l.motion === 'number') bits.push(`motion ${l.motion}`);
        if (l.tracked_points) bits.push(`${l.tracked_points} points`);
        if (l.face_px) bits.push(`face ${l.face_px}px`);
        detail = `<div class="text-xs text-muted mt-1" style="font-family:var(--font-mono)">${
                      Charts.esc(bits.join(' · '))}</div>`;
    }
    return `
        <div class="liveness-banner ${kind}">
            <div class="liveness-title">${Charts.esc(title)}</div>
            <div class="text-sm">${Charts.esc(message || l.reason || '')}</div>
            ${detail}
            ${frames ? `<div class="liveness-frames">${frames}</div>
                        <div class="text-xs text-muted mt-1">Frames the check was made from</div>` : ''}
        </div>`;
}


// --- Students Page ---
/* ---------------------------------------------------------------------------
   One athlete, in full

   The directory is a grid of thumbnails; this is where somebody actually looks
   at a person - the photo at a size worth having, what is on record, and a copy
   of the picture if they need one for a form.
--------------------------------------------------------------------------- */

function openStudentDetail(studentId) {
    // From the list the grid was drawn from, not a fresh request: there is no
    // GET /api/students/{id}, and adding one would be a new route and a new
    // access decision for data this page already holds.
    const p = (state.students || []).find(x => String(x.id) === String(studentId));
    if (!p) {
        return showToast('Could not open', 'That athlete is no longer listed.', 'error');
    }
    const line = (k, v) => v ? `
        <div><span class="ck">${Charts.esc(k)}</span><div>${Charts.esc(String(v))}</div></div>` : '';

    openModal(p.name || 'Athlete', `
        <div style="display:flex;gap:18px;flex-wrap:wrap">
            <div style="flex:0 0 220px;max-width:100%">
                <img src="${p.photo_url}" alt="${Charts.esc(p.name || '')}"
                     style="width:220px;height:220px;object-fit:cover;border-radius:12px;background:var(--bg-subtle)">
                <button class="btn btn-secondary" style="width:220px;margin-top:8px"
                        data-download-photo data-url="${p.photo_url}"
                        data-filename="${Charts.esc((p.roll_no || p.name || 'athlete'))}.jpg">
                    Download photo
                </button>
            </div>
            <div style="flex:1;min-width:220px">
                <div class="detail-grid">
                    <div><span class="ck">NSRS ID</span>
                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                            <span${isPlaceholderId(p.roll_no)
                                ? ' style="opacity:.65;font-style:italic"' : ''
                            }>${Charts.esc(rollLabel(p.roll_no))}</span>
                            <button class="btn btn-secondary"
                                style="min-height:26px;padding:0 8px;font-size:11px"
                                data-set-nsrs data-student-id="${p.id}"
                                data-current="${Charts.esc(p.roll_no || '')}"
                                data-name="${Charts.esc(p.name || '')}">
                                ${isPlaceholderId(p.roll_no) ? 'Set' : 'Change'}</button>
                        </div></div>
                    ${line('Role', p.role)}
                    ${line('Gender', p.gender)}
                    ${line('Sport', p.sport)}
                    ${line('Days present', p.total_present ?? 0)}
                    ${line('Face templates', p.templates ?? 0)}
                    ${line('Enrolled', (p.created_at || '').replace('T', ' '))}
                </div>
            </div>
        </div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Close</button>`);
}

async function downloadStudentPhoto(url, filename) {
    // Fetched, not linked. /api/photos needs a session, and a plain
    // <a download> hands the URL to the browser's downloader, which does not
    // send the Authorization header - the file would come back as a 401 page
    // saved under a .jpg name.
    try {
        const res = await fetch(url, { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`Server said ${res.status}`);
        const blob = await res.blob();
        const href = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = href;
        a.download = filename || 'athlete.jpg';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(href), 10000);
    } catch (err) {
        showToast('Could not download', (err && err.message) || 'Try again.', 'error');
    }
}

/* A signup has to fill roll_no with something - the column is unique and NOT
   NULL - so it gets PEND-xxxxxxxx. That is a placeholder, not an NSRS ID, and
   showing it verbatim put an official-looking code next to somebody's name
   that nobody could look up or correct. */
function isPlaceholderId(roll) {
    return !roll || String(roll).startsWith('PEND-');
}

function rollLabel(roll) {
    return isPlaceholderId(roll) ? 'NSRS ID not set' : roll;
}

async function setNsrsId(studentId, current, name) {
    const next = window.prompt(
        `NSRS ID for ${name}.\n\nThis is the official Khelo India identifier. `
        + `Leave blank to cancel.`, isPlaceholderId(current) ? '' : current);
    if (next === null) return;
    const val = next.trim();
    if (!val) return;
    try {
        const fd = new FormData();
        fd.append('roll_no', val);
        await api.postForm(`/api/people/${studentId}`, fd, 'PATCH');
        showToast('NSRS ID set', `${name} is now ${val}.`, 'success');
        closeModal();
        renderStudents();
    } catch (err) {
        // A duplicate is the likely failure and worth naming: two people cannot
        // share an NSRS ID, and the server's unique constraint says so.
        showToast('Could not set it',
                  (err && err.message) || 'That ID may already belong to somebody.',
                  'error');
    }
}

let studentRole = 'athlete';

async function renderStudents() {
    try {
        // The page is the ATHLETE Directory, so it asks for athletes. Coaches
        // are enrolled people too and this is the only screen that can enrol
        // one, so they are one button away rather than unreachable.
        const data = await api.get(`/api/students?role=${encodeURIComponent(studentRole)}`);
        state.students = data.students;
        drawStudents(state.students);

        document.querySelectorAll('[data-role-filter]').forEach(b => {
            b.className = 'btn ' + (b.dataset.roleFilter === studentRole
                ? 'btn-primary' : 'btn-secondary');
            b.onclick = () => {
                if (studentRole === b.dataset.roleFilter) return;
                studentRole = b.dataset.roleFilter;
                renderStudents();
            };
        });
        const title = document.getElementById('page-title');
        if (title) {
            title.textContent = studentRole === 'coach'
                ? 'Coaches' : 'Athletes';
        }

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
            <!-- This handler interpolated s.name with NO escaping at all, so a
                 person named  ');alert(1);//  ran code on this page for every
                 coach who opened it. dataset removes the JS-string context. -->
            <button class="btn-icon btn-delete-student" title="Delete"
                    data-delete-student data-student-id="${s.id}" data-student-name="${Charts.esc(s.name)}">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
            </button>
            <!-- The photo is the obvious thing to click, so it is the way in.
                 A button, not a div with a handler, so it is reachable by
                 keyboard and announced as something that does anything. -->
            <button class="student-photo-wrap" data-open-student data-student-id="${s.id}"
                    title="See ${Charts.esc(s.name)}'s photo and details"
                    style="display:block;width:100%;padding:0;border:0;background:none;cursor:pointer">
                ${s.photo_url
                    ? `<img src="${s.photo_url}" class="student-photo" alt="${Charts.esc(s.name)}">`
                    : `<div class="student-photo" style="display:flex;align-items:center;justify-content:center;
                            background:var(--bg-subtle);color:var(--text-secondary);font-size:13px">
                           No photo yet</div>`}
            </button>
            <div class="student-info">
                <button class="student-name" data-open-student data-student-id="${s.id}"
                        style="border:0;background:none;padding:0;font:inherit;color:inherit;cursor:pointer;text-align:left">
                    ${Charts.esc(s.name)}</button>
                <div class="student-meta">
                    <span class="student-roll"${isPlaceholderId(s.roll_no)
                        ? ' style="opacity:.65;font-style:italic"' : ''
                    }>${Charts.esc(rollLabel(s.roll_no))}</span>
                    <span title="Days marked present">${s.total_present || 0} present</span>
                </div>
                <div class="student-meta" style="margin-top: 6px; gap: 6px; display: flex; flex-wrap: wrap; align-items: center;">
                    ${tmplBadge}
                    <!-- dataset + delegated listeners, never an interpolated
                         handler. Backslash-escaping a quote does not work here:
                         the HTML parser decodes the attribute BEFORE the JS is
                         parsed, so a name containing a quote still breaks out.
                         Passing the value as data and reading it with .dataset
                         removes the JS-string context altogether. -->
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;"
                            data-add-photo data-student-id="${s.id}" data-student-name="${Charts.esc(s.name)}"
                            title="Add another photo (recent selfie or ID)">
                        ${Icon('upload', 12)}Add photo
                    </button>
                    <!-- One button, not two. "Register face" and "Record clip"
                         did the same job by different means; the clip is the
                         one that also proves a real person is present, so the
                         head-circle scan is gone and this keeps the familiar
                         label. dataset + a delegated listener, never an
                         interpolated onclick - a name containing a quote breaks
                         out of a handler string. -->
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;"
                            data-clip-enrol data-student-id="${s.id}" data-student-name="${Charts.esc(s.name)}"
                            title="Record a two second clip - captures several views and checks a real person is present">
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
    if (!dateInput) return;
    const today = localISODate();
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
                <td class="font-mono" data-label="NSRS ID">${Charts.esc(r.roll_no)}</td>
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
    // The footer is omitted entirely when there is nothing to put in it. The
    // previous version always emitted the bar and interpolated footerHTML, so
    // every caller that passes only a title and body - and several do - printed
    // the literal word "undefined" under an empty rule.
    const footer = footerHTML
        ? `<div class="modal-footer" style="margin: 20px -20px -20px; padding: 16px 20px; border-top: 1px solid var(--border-subtle); display: flex; justify-content: flex-end; gap: 12px; background: var(--bg-surface);">
               ${footerHTML}
           </div>`
        : '';
    document.getElementById('modal-body').innerHTML = `${contentHTML}${footer}`;
    document.getElementById('modal-container').classList.remove('hidden');
}

function closeModal() {
    // Any camera running inside the modal cleans itself up: openClipCapture
    // watches this element's class and tears its stream down when it hides, so
    // the X, Escape and every Cancel are all covered without this function
    // knowing what the modal happens to contain.
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
    'Follow the on-screen prompts and turn your head as asked - that is what shows a real person is present, not a photograph',
];

let regDetails = null;

async function openRegisterModal() {
    let centres = [];
    try {
        centres = (await api.get('/api/centres')).centres || [];
    } catch { /* the select simply renders empty and the field stays required */ }

    const html = `
        <!-- onsubmit is load-bearing: the footer buttons are type="button", but
             pressing Enter in a text field (a phone keyboard's "Go" key) still
             submits natively, which reloaded the app and silently discarded
             everything typed. Enter now does what the person meant. -->
        <form id="register-form" autocomplete="off" onsubmit="event.preventDefault(); regContinue(); return false;">
            <div class="form-group">
                <label class="form-label" for="reg-name">Full name</label>
                <input type="text" id="reg-name" class="form-input" required placeholder="As it appears on the roster">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label" for="reg-roll">NSRS ID</label>
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
                <strong>Before you record</strong>
                <ul style="margin:6px 0 0 18px;padding:0;font-size:12px;line-height:1.7">
                    ${REG_GUIDELINES.map(g => `<li>${Charts.esc(g)}</li>`).join('')}
                </ul>
            </div>
        </form>`;

    openModal('Register person', html, `
        <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn btn-primary" onclick="regContinue()">Continue to recording</button>`);
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
    // Same capture component as re-registering an existing person; only what
    // happens with the clip at the end differs.
    openClipCapture({
        title: `Record clip - ${regDetails.name}`,
        intro: "Look at the camera and move your head a little while recording. "
             + "Two seconds is enough.",
        onClip: enrolSubmit,
    });
}

/* The step photos the guided capture took at each confirmed turn. The server
   judges those instead when it cannot find the face in the compressed video -
   which is what "could not see your face" was, over a face the camera had
   just tracked through every turn. */
function appendStepPhotos(fd, extra) {
    ((extra && extra.snapshots) || []).forEach((s, i) => {
        fd.append('snapshots', s.blob, `${i}_${s.step}.jpg`);
    });
}

/* When the phone saw the blink, from the start of the recording. Only tells the
   server where to look - backend/blink.py still has to find the blink in the
   video itself. */
function appendBlinkTime(fd, extra) {
    if (extra && Number.isFinite(extra.blinkAtMs)) fd.append('blink_at_ms', String(Math.round(extra.blinkAtMs)));
}

// The app refuses clips over 25MB and the proxy over 30MB, the proxy with an
// empty page the app can only call "Could not reach the server".
const CLIP_UPLOAD_MAX_BYTES = 24 * 1024 * 1024;

async function enrolSubmit(file, ui, extra) {
    ui.status('Checking the clip and registering...');
    if (file.size > CLIP_UPLOAD_MAX_BYTES) {
        ui.status('That recording was too long to upload. Record again - it only needs a few seconds.');
        await ui.resume();
        return;
    }

    // ONE request, deliberately. The old flow created the person from the first
    // frame and then added the rest, so a failure part-way left a roster entry
    // with no usable templates - someone who can never be recognised and whom
    // nobody is prompted to fix. The server now checks liveness before writing
    // anything and creates the person only if the clip passes.
    const fd = new FormData();
    fd.append('video', file);
    fd.append('name', regDetails.name);
    fd.append('roll_no', regDetails.roll_no);
    fd.append('centre_id', regDetails.centre_id);
    fd.append('role', regDetails.role);
    if (regDetails.sport) fd.append('sport', regDetails.sport);
    appendStepPhotos(fd, extra);

    try {
        const r = await api.postForm('/api/students/register-video', fd);
        if (r.ok === false && r.duplicate) {
            // Recording again cannot change the answer - close and say why.
            ui.close();
            showToast('Already registered', r.message, 'error');
            return;
        }
        if (r.ok === false) {
            // A pose refusal comes from a clip that WAS live, and
            // livenessBanner would title it "Live capture confirmed" right
            // beside the refusal. Say what was actually missing instead.
            if (r.pose_check && r.pose_check.ok === false) ui.status(r.message);
            else ui.status(livenessBanner(r.liveness, r.message), true);
            showToast('Not registered', r.message || 'The clip was refused', 'error');
            await ui.resume();
            return;
        }
        const n = r.templates || 1;
        const poses = (r.poses_captured || []).join(', ');
        const pc = r.pose_check || {};
        ui.close();
        // THIS USED TO BE SILENT ON THE CASE THAT MATTERS. "Registered" fired
        // unqualified whether or not the head-turning the intro asked for ever
        // happened - a still face for two seconds and a full turn through four
        // angles produced the identical toast, because pose_check.sufficient
        // was computed by the server and then never read. The distinction is
        // exactly the one a coach needs before walking away: one template from
        // one angle is a person who may not be recognised from across a room
        // tomorrow, and the only chance to redo it is now, not after the first
        // missed attendance.
        if (pc.sufficient === false) {
            showToast('Registered - but check the recording',
                      `${regDetails.name} enrolled with ${n} template${n === 1 ? '' : 's'}. `
                      + (pc.message || 'Only one view of the face was captured.'),
                      'info');
        } else {
            showToast('Registered',
                      `${regDetails.name} enrolled with ${n} template${n === 1 ? '' : 's'}`
                      + (poses ? ` (${poses})` : ''),
                      'success');
        }
        if (state.currentRoute === '/students') renderStudents();
    } catch (err) {
        // A duplicate NSRS ID is a 409 the person can act on, so it must not
        // be swallowed into a generic failure.
        const msg = (err && err.message) ? err.message : 'Could not register. Try again.';
        ui.status(msg);
        await ui.resume();
    }
}


function confirmDeleteStudent(id, name) {
    // Escaped here as well as in the attribute: .dataset DECODES the entity on
    // read, so `name` arrives as the raw string and interpolating it into
    // innerHTML would put the injection straight back.
    const html = `<p>Are you sure you want to delete <strong>${Charts.esc(name)}</strong>? This action cannot be undone and will not remove past attendance records, but will prevent future recognition.</p>`;
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


/* ---------------------------------------------------------------------------
   The register (v1)

   The coach's review screen. Attendance is no longer whatever the recogniser
   returned - it is this list, after a human has looked at it.

   Two things this screen must do that a results panel does not:
     * show athletes who are ABSENT as well as present, because a register you
       cannot use to notice who is missing is not a register; and
     * make every row togglable, since the recogniser is the draft and the
       coach is the authority.
--------------------------------------------------------------------------- */

let regSession = null;
let regCounts = { present: 0, total: 0 };

/* ---------------------------------------------------------------------------
   Take Attendance

   The coach's rear camera, one athlete at a time. Each clip is recognised
   against this coach's own athletes and, on a match, marks that athlete
   present on today's register (added late if it is already submitted). The
   camera stays open: recording starts again by itself once the last face has
   left the frame and the next athlete steps up.
--------------------------------------------------------------------------- */
async function initTakeAttendancePage() {
    const start = document.getElementById('ta-start');
    // An earlier register left unsubmitted comes first - no scanning until then.
    const pend = await renderPendingBanner('ta-pending');
    if (start) {
        if (pend) {
            start.disabled = true;
            start.textContent = 'Submit your earlier register first';
        } else {
            start.addEventListener('click', taScan);
        }
    }
    await renderDashboardRoleCard('ta-table');
}

function taScan() {
    const last = document.getElementById('ta-last');
    openClipCapture({
        title: 'Take attendance',
        guided: false,
        facingMode: 'environment',
        rearmOnNoFace: true,
        frameWidth: 960,
        // Recognition does not need a blink, so a clip without one is still
        // sent - it is simply not proven live.
        uploadWithoutBlink: true,
        intro: 'Hold the phone close to one athlete - about an arm\'s length. Recording '
             + 'starts by itself once their face is close enough; ask them to blink.',
        onClip: async (file, ui) => {
            ui.status('Checking…');
            let scanned = false;
            try {
                const fd = new FormData();
                fd.append('clip', file);
                const r = await api.postForm('/api/attendance/scan', fd, 'POST', true);
                if (!r.ok && r.reason === 'pending_register') {
                    // The server's own ordering check: send them to submit it.
                    ui.close();
                    showToast('Submit your earlier register first', r.message, 'warning');
                    window.location.hash = '#/register';
                    return;
                }
                if (!r.ok) {
                    speak(r.message || 'Not recognised');
                    ui.status(r.message || 'Not recognised - try again.');
                } else {
                    const msg = r.already ? `${r.name} is already marked present`
                        : r.late ? `${r.name} - added late` : `${r.name} - present`;
                    speak(r.already ? `${r.name}, already present` : `${r.name}, present`);
                    ui.status(`✓ ${msg}. Next athlete, please.`);
                    if (last) last.textContent = `Last scanned: ${msg}`;
                    scanned = true;
                    renderDashboardRoleCard('ta-table');
                }
            } catch (err) {
                ui.status((err && err.message) || 'Could not check that - try again.');
            }
            // A moment to read the result before the next athlete steps up.
            await new Promise(res => setTimeout(res, 1800));
            // A marked athlete must leave the frame before the next scan (or
            // they are scanned on a loop); a refused one is scanned again now.
            await ui.resume({ rearm: !scanned });
        },
    });
}

async function initRegisterPage() {
    const sub = document.getElementById('reg-submit-btn');
    if (sub) sub.addEventListener('click', regSubmit);

    // Records, collapsed at the foot of the page - see the template comment
    // in tpl-register for why this lives here rather than behind its own nav
    // item.
    const recToggle = document.getElementById('btn-toggle-records');
    const recBody = document.getElementById('mark-records-body');
    if (recToggle && recBody) {
        recToggle.addEventListener('click', () => {
            if (!recBody.classList.contains('hidden')) {
                recBody.classList.add('hidden');
                recToggle.setAttribute('aria-expanded', 'false');
                return;
            }
            // Built on first open rather than at page load: fetching it up
            // front would delay the roster for a request most visits never make.
            if (!recBody.dataset.ready) {
                recBody.appendChild(
                    document.getElementById('tpl-records').content.cloneNode(true));
                recBody.dataset.ready = '1';
                initRecordsPage();
            }
            recBody.classList.remove('hidden');
            recToggle.setAttribute('aria-expanded', 'true');
        });
    }

    // Delegated, not per-row: the roster is re-rendered after every toggle and
    // per-row listeners would leak one per render.
    const host = document.getElementById('reg-roster');
    if (host) {
        host.addEventListener('click', (e) => {
            const el = e.target.closest('[data-toggle-student]');
            if (!el || el.disabled) return;
            regToggle(parseInt(el.dataset.toggleStudent, 10), el.dataset.present !== 'true',
                      el.dataset.name || '');
        });
    }
    const appr = document.getElementById('reg-approvals');
    if (appr) {
        appr.addEventListener('click', (e) => {
            const re = e.target.closest('[data-reassign-user]');
            if (re) return regReassign(parseInt(re.dataset.reassignUser, 10),
                                       parseInt(re.dataset.centreId, 10),
                                       re.dataset.personName || '');
            const el = e.target.closest('[data-approve-user]');
            if (el) regDecide(parseInt(el.dataset.approveUser, 10),
                              el.dataset.decision === 'approve',
                              el.dataset.personName || '',
                              el.dataset.personRole || 'athlete',
                              el.dataset.merge === 'true',
                              el.dataset.templates);
        });
    }
    // Centre first, then open: for a super admin regOpen() reads #reg-centre,
    // which does not exist to read yet until this has run.
    await populateRegisterCentres();
    await Promise.all([regOpen(), regLoadApprovals()]);
}


async function regLoadApprovals() {
    const card = document.getElementById('reg-approvals-card');
    const host = document.getElementById('reg-approvals');
    const count = document.getElementById('reg-approvals-count');
    if (!card || !host) return;
    try {
        const r = await api.get('/api/approvals');
        const list = r.pending || [];
        card.style.display = list.length ? '' : 'none';
        if (count) count.textContent = list.length ? `(${list.length})` : '';
        host.innerHTML = list.map(p => {
            const name = p.person_name || p.full_name || p.username;
            const photo = p.photo_path
                ? `<img src="/api/photos/${encodeURIComponent(String(p.photo_path).split(/[\\/]/).pop())}"
                        alt="" style="width:44px;height:44px;border-radius:8px;object-fit:cover">`
                : '<div style="width:44px;height:44px;border-radius:8px;background:var(--bg-subtle)"></div>';
            // A coach application is called out rather than left looking like
            // every other row. Only a super admin ever sees one here, and
            // approving it hands over a whole centre - so it should not be
            // clearable in the same rhythm as a queue of athletes.
            const flag = p.role === 'coach'
                ? `<div class="text-xs" style="color:#b45309;font-weight:600">
                       Asking for COACH access to ${Charts.esc(p.centre_name || 'a centre')}</div>`
                : '';
            const role = Charts.esc(p.role || 'athlete');

            // Somebody already enrolled whose face this matched. Offered as a
            // question with its own button, because approving it as a new
            // person is what creates the duplicate - two lines on the
            // register, and one of them marked absent every day.
            const dup = p.duplicate_of ? `
                <div class="text-xs" style="color:#b45309;font-weight:600">
                    Looks like ${Charts.esc(p.duplicate_name || 'someone already enrolled')}
                    ${p.duplicate_roll_no ? `(${Charts.esc(p.duplicate_roll_no)})` : ''}
                    &middot; ${(Number(p.duplicate_score) || 0).toFixed(2)}
                    ${p.duplicate_centre_name ? `&middot; ${Charts.esc(p.duplicate_centre_name)}` : ''}
                </div>` : '';
            // Their coach was deleted, so nobody is looking at this but a
            // super admin - who has no way to know that without being told.
            const orphan = p.orphaned ? `
                <div class="text-xs" style="color:#b45309;font-weight:600">
                    No coach &mdash; the one they chose has been removed</div>` : '';
            const mergeBtn = p.duplicate_of ? `
                <button type="button" class="btn btn-secondary" style="height:30px;font-size:12px;padding:0 10px"
                        data-approve-user="${p.user_id}" data-decision="approve" data-merge="true"
                        data-person-role="${role}"
                        data-person-name="${Charts.esc(name)}">Same person</button>` : '';
            const reassignBtn = p.orphaned ? `
                <button type="button" class="btn btn-secondary" style="height:30px;font-size:12px;padding:0 10px"
                        data-reassign-user="${p.user_id}" data-centre-id="${p.centre_id || ''}"
                        data-person-name="${Charts.esc(name)}">Assign a coach</button>` : '';
            return `
            <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border-subtle)">
                ${photo}
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600">${Charts.esc(name)}</div>
                    ${flag}
                    ${dup}
                    ${orphan}
                                        <div class="text-xs text-muted font-mono">${Charts.esc(p.roll_no || '')}</div>
                    <!-- The person row is created before any face is ever
                         recorded, so 0 here is not unusual - it is what a
                         failed or abandoned capture looks like. Plain muted
                         text made it read the same as any other count, and it
                         was the one number on this card that actually
                         mattered before pressing Approve. -->
                    <div class="text-xs${p.templates ? ' text-muted' : ''}"
                         ${p.templates ? '' : 'style="color:#b45309;font-weight:600"'}>
                        ${p.templates || 0} face template(s)${p.templates ? '' : ' - no face on file'}</div>
                </div>
                ${reassignBtn}
                ${mergeBtn}
                <button type="button" class="btn btn-secondary" style="height:30px;font-size:12px;padding:0 10px"
                        data-approve-user="${p.user_id}" data-decision="reject"
                        data-person-role="${role}"
                        data-person-name="${Charts.esc(name)}">Reject</button>
                <button type="button" class="btn btn-primary" style="height:30px;font-size:12px;padding:0 10px"
                        data-approve-user="${p.user_id}" data-decision="approve"
                        data-person-role="${role}" data-templates="${Number(p.templates) || 0}"
                        data-person-name="${Charts.esc(name)}">Approve</button>
            </div>`;
        }).join('');
    } catch (err) {
        card.style.display = 'none';
    }
}

async function regReassign(userId, centreId, name) {
    try {
        const r = await api.get(`/api/approvals/coaches?centre_id=${centreId}`);
        const list = r.coaches || [];
        if (!list.length) {
            return showToast('No coaches', `There are no active coaches at that centre yet.`, 'info');
        }
        const menu = list.map((c, i) => `${i + 1}. ${c.name}`).join("\n");
        const pick = window.prompt(
            `Who should decide ${name}?\n\n${menu}\n\nEnter a number.`, '');
        if (pick === null) return;
        const idx = parseInt(pick, 10) - 1;
        if (!(idx >= 0 && idx < list.length)) return;
        const fd = new FormData();
        fd.append('coach_id', list[idx].id);
        await api.postForm(`/api/approvals/${userId}/coach`, fd);
        showToast('Assigned', `${name} is now in ${list[idx].name}'s queue.`, 'success');
        await regLoadApprovals();
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
}

async function regDecide(userId, approve, name, role = 'athlete', merge = false, templates = 0) {
    let guardian = null;
    // The person row is created at the START of self-registration, before any
    // face is ever recorded - see signup.start. So "pending, 0 templates" is
    // not rare: it is what a capture that failed, or was never attempted,
    // looks like from here. Approving it anyway can be the right call - a
    // coach can register the face in person afterwards - but it must be seen,
    // not defaulted into by a click that looks identical to a clean approval.
    const noFace = approve && !merge && !(Number(templates) > 0);
    const faceWarning = noFace
        ? `\n\n⚠ No face has been captured for this person yet. They will `
          + `not be recognised in any capture until one is added.\n`
        : '';
    if (approve && merge) {
        // The merge is the destructive half of this screen - it deletes the
        // record just created and moves its faces onto an existing person - so
        // it is confirmed on its own terms rather than folded into Approve.
        if (!window.confirm(
            `Treat ${name} as somebody already enrolled?\n\nTheir new face `
            + `captures move onto the existing record and this duplicate is removed. `
            + `Their attendance stays on the one record instead of splitting `
            + `between two.`)) return;
    }
    if (approve && role === 'coach') {
        // Typed, not clicked. Approving a coach grants a whole centre, and it
        // arrives in a list where the muscle memory is to tap Approve - so the
        // confirmation has to break that rhythm rather than join it.
        const typed = window.prompt(
            `Approving ${name} as a COACH.\n\nThey will see every athlete at `
            + `their centre, take attendance, and approve athletes themselves.${faceWarning}\n`
            + `Type APPROVE to confirm.`, '');
        if ((typed || '').trim().toUpperCase() !== 'APPROVE') return;
    } else if (approve) {
        // Guardian consent is asked for on approval, not at signup: the coach is
        // the person who knows whether this athlete is a minor.
        guardian = window.prompt(
            `Approving ${name}.\n\nIf this athlete is under 18, enter the guardian's `
            + `name to record consent. Leave blank if they are an adult.${faceWarning}`, '');
        if (guardian === null) return;          // cancelled
    }
    try {
        const fd = new FormData();
        fd.append('approve', approve ? 'true' : 'false');
        if (merge) fd.append('merge', 'true');
        if (guardian) {
            fd.append('guardian_name', guardian);
            fd.append('guardian_consent', 'true');
        }
        await api.postForm(`/api/approvals/${userId}`, fd);
        showToast(approve ? (merge ? 'Merged' : 'Approved') : 'Rejected',
                  approve ? (merge
                             ? `${name} was folded into the record already enrolled.`
                             : role === 'coach'
                             ? `${name} can now sign in as a coach at their centre.`
                             : `${name} can now sign in and be recognised.`)
                          : `${name} was rejected.`,
                  approve ? 'success' : 'info');
        await Promise.all([regLoadApprovals(), regLoad()]);
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
}

/* A super admin has no centre_id of their own - they are an operator, not
 * attached to one - so #reg-centre (see tpl-register) is the only way they
 * can say which one today's register is for. Without this, regOpen() sent no
 * centre_id at all, the server correctly refused with "A centre is required
 * to open a register", and the roster panel was left on "Loading..."
 * permanently: nothing after that ever set regSession, so regLoad() - the
 * only thing that would replace that text - was never reached. */
async function populateRegisterCentres() {
    const sel = document.getElementById('reg-centre');
    if (!sel || sel.options.length > 1) return;
    try {
        const data = await api.get('/api/centres');
        data.centres.forEach(c => sel.add(new Option(`${c.name} (${c.code})`, c.id)));
        // Same key Mark Attendance remembers under: one super admin is almost
        // always working one centre at a time, and there is no reason picking
        // it twice should be required on two different pages in the same visit.
        const remembered = localStorage.getItem('facemark.lastCentre');
        if (remembered && data.centres.some(c => String(c.id) === remembered)) {
            sel.value = remembered;
        }
        sel.addEventListener('change', () => {
            if (sel.value) localStorage.setItem('facemark.lastCentre', sel.value);
            regOpen();
        });
    } catch { /* the selector stays empty; regOpen() will report why */ }
}

async function regOpen(forceToday = false) {
    try {
        const fd = new FormData();
        if (session.user && session.user.centre_id) {
            fd.append('centre_id', session.user.centre_id);
        } else {
            const cs = document.getElementById('reg-centre');
            if (cs && cs.value) fd.append('centre_id', cs.value);
        }
        // An earlier register with attendance marked but not submitted is
        // opened FIRST (reopening it if it had expired), so it is reviewed and
        // signed before today's.
        const pend = forceToday ? null : await loadPendingRegister();
        if (pend) fd.append('date_str', pend.date);
        const r = await api.postForm('/api/sessions', fd);
        regSession = r.session;
        await regLoad();
    } catch (err) {
        const meta = document.getElementById('reg-session-meta');
        if (meta) meta.textContent = (err && err.message) || 'Could not open a register.';
        // regLoad() never ran, so nothing else clears "Loading..." - which is
        // exactly the stuck state this whole function exists to avoid.
        const roster = document.getElementById('reg-roster');
        if (roster) roster.innerHTML = '<div class="empty-state">Choose a centre above to open its register.</div>';
    }
}

async function regLoad() {
    if (!regSession) return;
    const data = await api.get(`/api/sessions/${regSession.id}`);
    regSession = data.session;

    // Working on an EARLIER day's register: say so, and once it is submitted
    // offer the way on to today's.
    const pendHost = document.getElementById('reg-pending');
    if (pendHost) {
        if (data.session.date !== localISODate()) {
            const day = Charts.esc(prettyDay(data.session.date));
            const done = data.session.status === 'submitted';
            pendHost.innerHTML = done
                ? `<div style="background:#ecfdf5;border:1px solid #6ee7b7;border-left:4px solid #16a34a;
                               border-radius:var(--radius-md, 10px);padding:14px 16px;margin-bottom:16px">
                       <div style="font-weight:600;margin-bottom:8px">Register for ${day} submitted.</div>
                       <button type="button" class="btn btn-primary" id="reg-go-today">Take today's attendance</button>
                   </div>`
                : `<div style="background:#fffbeb;border:1px solid #fcd34d;border-left:4px solid #f59e0b;
                               border-radius:var(--radius-md, 10px);padding:14px 16px;margin-bottom:16px">
                       <div style="font-weight:600;margin-bottom:4px">This is your register for ${day} - it was not submitted</div>
                       <div class="text-sm">Review it and submit it first. Today's register opens after that.</div>
                   </div>`;
            const go = document.getElementById('reg-go-today');
            if (go) go.addEventListener('click', () => regOpen(true));
        } else {
            pendHost.innerHTML = '';
        }
    }

    // Kept for the submit confirmation, which has to say what it is about to
    // record. regLoad already has the authoritative counts from the server;
    // recounting them from the DOM would be a second source of the same truth.
    // Off-roster rows (on the register but not in the listed roster) count too -
    // they are attendance the coach is about to sign for.
    const extras = data.off_roster || [];
    regCounts = { present: data.present_count, total: data.roster_count + extras.length };

    const regTitle = document.getElementById('reg-session-title');
    if (regTitle) {
        regTitle.textContent = data.session.date === localISODate()
            ? 'Today’s register' : `Register for ${prettyDay(data.session.date)}`;
    }
    const meta = document.getElementById('reg-session-meta');
    if (meta) {
        meta.textContent =
            `${data.session.date} \u00b7 ${data.present_count} of ${data.roster_count + extras.length} present`
            + ` \u00b7 ${data.session.status}`;
    }

    // An empty roster is the state every coach starts in, and it looks
    // identical to "everybody is absent". Say which it is.
    const emptyCard = document.getElementById('reg-empty-roster');
    if (emptyCard) emptyCard.style.display = (data.roster_count || extras.length) ? 'none' : '';

    const submitted = data.session.status === 'submitted';
    const subBtn = document.getElementById('reg-submit-btn');
    if (subBtn) {
        subBtn.disabled = submitted;
        subBtn.textContent = submitted
            ? (data.session.submitter_verified ? 'Submitted' : 'Submitted (unverified)')
            : 'Submit register';
    }

    const host = document.getElementById('reg-roster');
    if (!host) return;
    if (!data.roster.length && !extras.length) {
        host.innerHTML = `<div class="empty-state">
            <div>No athletes are registered at your centre yet.</div>
            <div class="text-xs text-muted" style="margin-top:6px">
                Athletes appear here once they are registered and approved.</div></div>`;
        return;
    }

    // Everyone on the register is drawn - including anybody marked who is not
    // in the listed roster - so what the coach signs for is what they can see.
    host.innerHTML = [...data.roster, ...extras].map(e => {
        const on = e.present;
        const lateOrigin = e.origin === 'late_added' || e.origin === 'late_approved';
        const badge = !on ? ''
            : lateOrigin
                ? '<span class="badge badge-amber">Added late</span>'
            : e.origin === 'self_marked'
                ? '<span class="badge badge-blue">Self-marked</span>'
                : e.origin === 'coach_added'
                    ? '<span class="badge badge-blue">Added by you</span>'
                    : `<span class="badge badge-green">Recognised${
                        e.confidence ? ' \u00b7 ' + Math.round(e.confidence * 100) + '%' : ''}</span>`;
        // After submitting, a coach can still add LATE JOINERS (recorded as late
        // and reported to the super admin by centre) or take a late addition
        // off again. Who was on the register when it was signed stays as signed.
        const locked = submitted && on && !lateOrigin;
        const label = on
            ? (submitted && lateOrigin ? 'Remove late' : 'Present')
            : (submitted ? 'Add late' : 'Mark present');
        const action = `<button type="button" class="btn ${on ? 'btn-secondary' : 'btn-primary'}"
                    style="height:30px;font-size:12px;padding:0 12px"
                    data-toggle-student="${e.student_id}" data-present="${on}"
                    data-name="${Charts.esc(e.name)}" ${locked ? 'disabled' : ''}>${label}</button>`;
        const geo = (on && e.geo_status && e.geo_status !== 'inside')
            ? `<span class="badge badge-amber">${Charts.esc(e.geo_status)}${
                e.distance_m ? ' \u00b7 ' + Math.round(e.distance_m) + 'm' : ''}</span>` : '';
        const crop = e.crop_url
            ? `<img src="${e.crop_url}" alt="" style="width:40px;height:40px;border-radius:8px;object-fit:cover">`
            : '<div style="width:40px;height:40px;border-radius:8px;background:var(--bg-subtle)"></div>';
        return `
        <div class="list-row" style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border-subtle)">
            ${crop}
            <div style="flex:1;min-width:0">
                <div style="font-weight:600">${Charts.esc(e.name)}</div>
                <div class="text-xs text-muted font-mono">${Charts.esc(e.roll_no || '')}</div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${badge}${geo}</div>
            ${action}
        </div>`;
    }).join('');
}

/* Everything a signed-in coach accumulated in memory, dropped on the way out.
   These are module-level and survive a logout on their own - the app never
   reloads between sessions, because routing is hashchange - so the next person
   to sign in on a shared centre phone inherited the last one's roster. */
function resetSessionState() {
    regSession = null;
    regCounts = { present: 0, total: 0 };
}

async function regToggle(studentId, present, name) {
    // On a submitted register this is a LATE change, visible to the super
    // admin - say so before doing it.
    if (regSession && regSession.status === 'submitted') {
        const who = name || 'this athlete';
        if (!window.confirm(present
                ? `Add ${who} as a late joiner?\n\nThe register is already submitted, so `
                  + `this is recorded as late attendance and shown to the super admin.`
                : `Remove ${who}'s late attendance?`)) return;
    }
    try {
        const fd = new FormData();
        fd.append('present', present ? 'true' : 'false');
        await api.postForm(`/api/sessions/${regSession.id}/roster/${studentId}`, fd, 'PATCH');
        await regLoad();
    } catch (err) {
        showToast('Could not change that', (err && err.message) || 'Try again.', 'error');
    }
}

/* Submitting is what turns drafts into attendance, so it asks for the coach's
   own face. A failed check may be retried; after config.VERIFY_MAX_RETRIES the
   server submits anyway and records it as unverified for an admin to see. The
   attempt counter is tracked here because the server is stateless about it. */
let regAttempt = 1;

function regSubmit() {
    if (!regSession) return;

    // Submitting is the irreversible step: it turns drafts into attendance and
    // closes the register. Until now the only thing between a stray tap and
    // that was the face check, which reads as a formality rather than a
    // decision. Say what is about to be recorded, and let it be cancelled.
    const present = regCounts.present || 0;
    const total = regCounts.total || 0;
    const absent = Math.max(0, total - present);
    if (!window.confirm(
            `Submit the register for ${regSession.date}?

`
            + `Present: ${present}
`
            + `Absent:  ${absent}
`
            + `Total:   ${total}

`
            + `This records attendance for ${present} `
            + `${present === 1 ? 'athlete' : 'athletes'} and closes the register `
            + `for that day. It cannot be undone from here.`)) {
        return;
    }

    regAttempt = 1;
    openClipCapture({
        // One face against one enrolled record. Several views were never needed
        // here, and the prompts contradicted this screen's own instruction.
        guided: false,
        title: 'Confirm it is you',
        intro: 'Look at the camera to sign this register. Recording starts by itself - '
             + 'blink when asked.',
        onClip: async (file, ui, extra) => {
            ui.status('Checking\u2026');
            try {
                const fd = new FormData();
                fd.append('clip', file);
                fd.append('attempt', String(regAttempt));
                appendBlinkTime(fd, extra);
                const r = await api.postForm(`/api/sessions/${regSession.id}/submit`, fd);

                if (r.submitted === false) {
                    regAttempt += 1;
                    ui.status(`${r.message}. ${r.retries_left} attempt`
                              + `${r.retries_left === 1 ? '' : 's'} left.`);
                    await ui.resume();
                    return;
                }
                ui.close();
                if (r.verified) {
                    showToast('Register submitted',
                              `${r.promoted} marked present`, 'success');
                } else {
                    // Not an error: the register IS submitted. Saying otherwise
                    // would leave a coach re-recording something already done.
                    showToast('Submitted, unverified',
                              'Your face could not be verified, so this has been '
                              + 'flagged for an administrator.', 'warning');
                }
                await regLoad();
            } catch (err) {
                ui.status((err && err.message) || 'Could not submit.');
                await ui.resume();
            }
        },
    });
}


/* ---------------------------------------------------------------------------
   The athlete's own page

   An athlete is not a coach with fewer buttons. This is the only screen they
   need: who their coaches are, a way to mark themselves present with one of
   them, and their own history. A self-mark is a DRAFT - the coach still
   confirms it - and the copy says so, because an athlete who thinks they are
   already marked will not chase it up.
--------------------------------------------------------------------------- */

async function initMePage() {
    const host = document.getElementById('me-coaches');
    if (host) {
        host.addEventListener('click', (e) => {
            const el = e.target.closest('[data-mark-coach]');
            if (el) meMark(parseInt(el.dataset.markCoach, 10), el.dataset.coachName || '');
        });
    }
    await Promise.all([meLoadCoaches(), meLoadHistory()]);
}

async function meLoadCoaches() {
    const host = document.getElementById('me-coaches');
    if (!host) return;
    try {
        const r = await api.get('/api/me/coaches');
        if (!r.coaches.length) {
            host.innerHTML = `<div class="empty-state">
                <div>You are not linked to a coach yet.</div>
                <div class="text-xs text-muted" style="margin-top:6px">
                    Your coach adds you to their roster.</div></div>`;
            return;
        }
        // Where today stands with each coach, so a mark never vanishes into
        // silence: waiting for the coach, confirmed, or left off a submitted
        // register. The button is only offered while nothing is recorded yet.
        const TODAY = {
            confirmed:  ['badge-green', 'Present today - confirmed by your coach'],
            pending:    ['badge-amber', 'Marked - waiting for your coach to confirm'],
            not_marked: ['badge-red',   'Not marked present today'],
        };
        host.innerHTML = r.coaches.map(c => {
            const state = (c.today && c.today.state) || 'open';
            const badge = TODAY[state];
            return `
            <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border-subtle)">
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600">${Charts.esc(c.name)}</div>
                    <div class="text-xs text-muted">${Charts.esc(c.centre_name || '')}</div>
                    ${badge ? `<span class="badge ${badge[0]}" style="margin-top:6px;white-space:normal;line-height:1.3">${badge[1]}</span>` : ''}
                </div>
                ${state === 'open' ? `
                <button type="button" class="btn btn-primary" style="height:32px;font-size:12px;padding:0 14px"
                        data-mark-coach="${c.id}" data-coach-name="${Charts.esc(c.name)}">
                    Mark me present
                </button>` : ''}
            </div>`;
        }).join('');
    } catch (err) {
        host.innerHTML = `<div class="empty-state">Could not load your coaches.</div>`;
    }
}

async function meLoadHistory() {
    const host = document.getElementById('me-history');
    if (!host) return;
    try {
        const r = await api.get('/api/me/attendance');
        if (!r.records.length) {
            host.innerHTML = `<div class="empty-state">No attendance recorded yet.</div>`;
            return;
        }
        const LABEL = {
            confirmed: ['badge-green', 'Confirmed'],
            pending:   ['badge-amber', 'Waiting for coach'],
            lapsed:    ['badge-red',   'Not confirmed'],
        };
        host.innerHTML = r.records.slice(0, 60).map(x => {
            const [cls, text] = LABEL[x.state] || LABEL.confirmed;
            const sub = [x.coach_name, (x.marked_at || '').replace('T', ' ').slice(0, 16)]
                .filter(Boolean).join(' · ');
            return `
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border-subtle)">
                <div style="min-width:0">
                    <div class="font-mono">${Charts.esc(x.date)}</div>
                    <div class="text-xs text-muted">${Charts.esc(sub)}</div>
                </div>
                <span class="badge ${cls}">${text}</span>
            </div>`;
        }).join('');
    } catch (err) {
        host.innerHTML = `<div class="empty-state">Could not load your history.</div>`;
    }
}

function meMark(coachId, coachName) {
    // Location is requested but never required. A refused or missing fix still
    // marks the athlete - it is flagged for the coach instead, because a
    // genuine athlete with bad GPS should not lose their attendance silently.
    const send = async (pos) => {
        openClipCapture({
            title: `Mark present \u2014 ${coachName}`,
            // One face against one enrolled record - the same 1:1 check as
            // signing the register, and it wants one view, not four.
            //
            // Without this the intro below ran the four-turn head-turning
            // sequence instead, so
            // the written instruction and the on-screen prompts asked for
            // different things at the same time, and a mark that should take
            // three seconds took up to thirty-four. The register-signing
            // capture was fixed for exactly this and its twin here was missed.
            guided: false,
            intro: 'Look at the camera. Recording starts by itself - blink when asked.',
            onClip: async (file, ui, extra) => {
                ui.status('Checking\u2026');
                try {
                    const fd = new FormData();
                    fd.append('clip', file);
                    fd.append('coach_id', String(coachId));
                    appendBlinkTime(fd, extra);
                    if (pos) {
                        fd.append('latitude', pos.coords.latitude);
                        fd.append('longitude', pos.coords.longitude);
                        fd.append('accuracy_m', pos.coords.accuracy);
                    }
                    const r = await api.postForm('/api/me/attendance', fd);
                    if (r.ok === false) {
                        ui.status(r.message || 'Could not confirm that was you.');
                        await ui.resume();
                        return;
                    }
                    ui.close();
                    showToast('Marked', r.message || 'Your coach will confirm it.',
                              r.geo && r.geo.status === 'inside' ? 'success' : 'warning');
                    await Promise.all([meLoadCoaches(), meLoadHistory()]);
                } catch (err) {
                    ui.status((err && err.message) || 'Could not mark you present.');
                    await ui.resume();
                }
            },
        });
    };
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(send, () => send(null),
                                                 { timeout: 6000, maximumAge: 60000 });
    } else {
        send(null);
    }
}


/* ---------------------------------------------------------------------------
   Oversight (super admin)

   The question this answers is not "how many were present" - the dashboard
   already does that. It is which registers are MISSING, and which of the ones
   that exist should not be taken at face value. Those two lists come first
   because they are the only ones that require somebody to do something.
--------------------------------------------------------------------------- */

async function initOversightPage() {
    const picker = document.getElementById('ov-date');
    if (picker) {
        picker.value = localISODate();
        picker.addEventListener('change', () => ovLoad(picker.value));
    }
    await ovLoad(picker ? picker.value : null);
}

function ovTile(label, value, tone) {
    const colour = tone === 'bad' ? 'var(--red)'
                 : tone === 'warn' ? 'var(--amber, #b45309)'
                 : 'var(--text-primary)';
    return `<div class="stat-card">
        <div class="stat-header">${Charts.esc(label)}</div>
        <div class="stat-value" style="color:${colour}">${value}</div>
    </div>`;
}

function ovList(title, rows, render, empty) {
    return `<div class="card" style="margin-bottom:16px">
        <div class="card-header"><div style="font-weight:600">${Charts.esc(title)}
            <span class="text-xs text-muted">(${rows.length})</span></div></div>
        <div class="card-body">${
            rows.length ? rows.map(render).join('')
                        : `<div class="empty-state">${Charts.esc(empty)}</div>`}</div>
    </div>`;
}

async function ovLoad(day) {
    const tiles = document.getElementById('ov-tiles');
    const lists = document.getElementById('ov-lists');
    const meta = document.getElementById('ov-meta');
    if (!tiles || !lists) return;
    try {
        const o = await api.get('/api/admin/overview' + (day ? `?date_str=${day}` : ''));
        if (meta) meta.textContent = `for ${o.date}`;

        tiles.innerHTML =
              ovTile('Registers missing', o.missing_count, o.missing_count ? 'bad' : null)
            + ovTile('Submitted', o.submitted_count)
            + ovTile('Still draft', o.draft_count, o.draft_count ? 'warn' : null)
            + ovTile('Unverified', o.unverified_count, o.unverified_count ? 'bad' : null)
            + ovTile('Photo-only captures', o.photo_only_count, o.photo_only_count ? 'warn' : null)
            + ovTile('Pending approvals', o.pending_approvals)
            // Its own tile, not folded into the one above. These are the
            // applications no coach can see, so they are the ones that sit
            // there until somebody comes looking - which is what this page is.
            + ovTile('No coach assigned', o.orphaned_approvals,
                     o.orphaned_approvals ? 'bad' : null);

        const row = (main, sub) => `
            <div style="display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border-subtle)">
                <span style="font-weight:600">${Charts.esc(main)}</span>
                <span class="text-xs text-muted">${Charts.esc(sub)}</span>
            </div>`;

        lists.innerHTML =
              ovList('Coaches with no register today', o.missing,
                     x => row(x.coach_name || `Coach ${x.coach_id}`, x.centre_name || ''),
                     'Every coach with athletes has opened a register.')
            + ovList('Submitted but NOT verified', o.unverified,
                     x => row(x.coach_name || 'Centre sweep',
                              `${x.date} \u00b7 score ${x.submitter_score ?? '-'} \u00b7 ${x.centre_name || ''}`),
                     'Every submission was verified.')
            + ovList('Still draft', o.drafts,
                     x => row(x.coach_name || 'Centre sweep',
                              `${x.rows || 0} row(s) \u00b7 expires ${(x.expires_at || '').replace('T', ' ')}`),
                     'Nothing left unsubmitted.')
            // A register that timed out appeared in none of the other lists -
            // not submitted, not draft, and its coach is not "missing" because
            // a session row exists. The attendance inside was deleted with it,
            // so without this panel nothing on this page ever mentioned it.
            + ovList('Expired unsubmitted', o.expired || [],
                     x => row(x.coach_name || 'Centre sweep',
                              `opened ${(x.created_at || '').replace('T', ' ')} \u00b7 `
                              + `expired ${(x.expires_at || '').replace('T', ' ')}`),
                     'No register timed out.')
            + ovList('Captures that could not be liveness-checked', o.photo_only,
                     x => row(x.coach_name || 'Centre sweep',
                              `${x.date} \u00b7 ${x.kind} \u00b7 ${x.liveness_verdict || 'not_checked'}`),
                     'Every capture was a checked video.')
            + ovList('Submitted today', o.submitted,
                     x => row(x.coach_name || 'Centre sweep',
                              `${(x.submitted_at || '').replace('T', ' ')} \u00b7 `
                              + `${x.submitter_verified ? 'verified' : 'UNVERIFIED'}`),
                     'No registers submitted yet today.');
    } catch (err) {
        lists.innerHTML = `<div class="empty-state">Could not load the overview.</div>`;
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

    // The icon is a fixed literal chosen above, so it can be markup. The title
    // and message are NOT: several callers pass server-controlled strings -
    // a student's name, an error from the API - and innerHTML on those is a
    // script-injection route through anything that can set a name.
    toast.innerHTML = icon;
    const content = document.createElement('div');
    content.className = 'toast-content';
    const t = document.createElement('div');
    t.className = 'toast-title';
    t.textContent = title == null ? '' : String(title);
    const m = document.createElement('div');
    m.className = 'toast-message';
    m.textContent = message == null ? '' : String(message);
    content.append(t, m);
    toast.appendChild(content);

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

document.addEventListener('click', (e) => {
    if (!e.target.closest) return;

    // Downloading is its own thing - it carries a url and a filename, not a
    // student id - so it is matched before the id-shaped buttons below.
    const nsrs = e.target.closest('[data-set-nsrs]');
    if (nsrs) {
        e.preventDefault();
        return setNsrsId(nsrs.dataset.studentId, nsrs.dataset.current, nsrs.dataset.name);
    }

    const dl = e.target.closest('[data-download-photo]');
    if (dl) {
        e.preventDefault();
        return downloadStudentPhoto(dl.dataset.url, dl.dataset.filename);
    }

    const btn = e.target.closest(
        '[data-clip-enrol], [data-add-photo], [data-delete-student], [data-open-student]');
    if (!btn) return;
    e.preventDefault();
    const id = btn.dataset.studentId;
    const name = btn.dataset.studentName || '';
    if (btn.hasAttribute('data-add-photo')) openAddPhotoModal(id, name);
    else if (btn.hasAttribute('data-delete-student')) confirmDeleteStudent(id, name);
    else if (btn.hasAttribute('data-open-student')) openStudentDetail(id);
    else openClipEnrol(id, name);
});

/** Record a short clip with live face tracking, and hand it to a caller.
 *
 * Shared by both enrolment paths - registering a new person, and re-registering
 * an existing one - because the capture is identical and only what happens with
 * the clip differs. It replaces the 24-segment "move your head in a circle"
 * ceremony: two seconds of ordinary movement produces the same several views
 * without asking a child to perform a sequence on cue, and unlike a set of
 * stills it carries the parallax that proves the subject is a person rather
 * than a photograph held to the lens.
 *
 * Recording blind and reporting a verdict afterwards was the wrong shape: the
 * person holding the phone could not tell whether the face was being seen at
 * all until it was too late. So this polls /api/enroll/pose-check while the
 * modal is open, draws the detected box over the video, and says what to change
 * - the same live loop the old scan used, so no new model and no build step.
 *
 * opts: { title, intro, onClip(file, ui) }
 *   ui.status(textOrHtml, isHtml)  report progress or a refusal
 *   ui.resume()                    return to a live camera for another attempt
 *   ui.close()                     finish and close the modal
 */
// The unguided clip. Longer than the legacy attendance capture because the
// parallax check has to find depth in it without any head movement to help -
// all it gets is the hand holding the phone.
const CLIP_MS_PLAIN = 3000;

/* The unguided clip, checked ON THE PHONE while it is framed and recorded, so
 * a clip the server's depth check cannot judge is not sent to be told so.
 *
 * DISTANCE. The server will not judge depth on a face under
 * config.LIVENESS_MIN_FACE_PX (150) wide in the recorded video - "too far" -
 * and that width is YuNet's box. The phone only has the landmark mesh, which
 * runs a little wider: replayed over 1,218 frames of 317 test clips, mesh/box
 * was 1.034 at the median and 1.090 at the 95th percentile. 164px of mesh puts
 * the server's box at 150px or more on 95% of frames.
 *
 * BLINK. Proof of a live face is an eye blink, not moving the phone: a photo
 * cannot close its eyes. Eye openness is the eye aspect ratio (EAR) of the
 * landmark mesh - eyelid gap over eye width, averaged over both eyes - which
 * does not depend on distance or head tilt. Judged against the person's OWN
 * open-eye reading, not a fixed number, because resting eye shape differs
 * between people: closed below BLINK_CLOSED_RATIO of it, open again above
 * BLINK_OPEN_RATIO. The server re-checks the uploaded clip with the same
 * landmark model and the same rule (backend/blink.py) - a browser's "blinked"
 * is not evidence. The recording ends a moment after the blink, so the clip
 * holds the eyes opening again. */
const LIVE_MIN_MESH_PX = 164;
const BLINK_CLOSED_RATIO = 0.60;
const BLINK_OPEN_RATIO = 0.80;
const BLINK_MAX_CLOSED_MS = 2000;   // longer than this is not a blink but eyes shut
const BLINK_TAIL_MS = 500;          // keep recording after the eyes reopen
const CLIP_MS_MIN = 1500;           // enough frames for the server's sampler to spread over
const CLIP_MS_MAX = 8000;           // then stop regardless - see opts.uploadWithoutBlink

/* Eye aspect ratio from the 478-point mesh, both eyes averaged; null if the
   mesh is incomplete. Indices are MediaPipe's: for each eye the two corners,
   and two upper/lower eyelid pairs. The same indices as backend/blink.py. */
const EAR_EYES = [[33, 160, 158, 133, 153, 144], [362, 385, 387, 263, 373, 380]];
function eyeAspect(pts, W = 1, H = 1) {
    if (!pts || pts.length < 478) return null;
    const d = (a, b) => Math.hypot((pts[a].x - pts[b].x) * W, (pts[a].y - pts[b].y) * H);
    let sum = 0;
    for (const [p1, p2, p3, p4, p5, p6] of EAR_EYES) {
        const width = d(p1, p4);
        if (width <= 0) return null;
        sum += (d(p2, p6) + d(p3, p5)) / (2 * width);
    }
    return sum / EAR_EYES.length;
}

/* Blink = eyes measured open, then closed, then open again - within
   BLINK_MAX_CLOSED_MS. The open reference is the median of recent open
   readings, so it follows the person rather than a constant. */
function makeBlinkDetector() {
    const open = [];
    let closedAt = 0;
    const median = a => { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };
    return {
        push(ear, t) {
            if (ear === null || ear === undefined) return false;
            if (open.length < 5) { open.push(ear); return false; }
            const base = median(open);
            if (ear < base * BLINK_CLOSED_RATIO) {
                if (!closedAt) closedAt = t;
                return false;
            }
            if (ear > base * BLINK_OPEN_RATIO) {
                const blinked = closedAt && t - closedAt <= BLINK_MAX_CLOSED_MS;
                closedAt = 0;
                open.push(ear);
                if (open.length > 30) open.shift();
                return !!blinked;
            }
            return false;
        },
    };
}

// Registration does NOT record for a fixed duration. A clock was tried first -
// ten seconds, on the reasoning that more elapsed time gives a person more
// chance to shift naturally. It was the wrong mechanism: a script tied to a
// clock plays "turn left" for a slice of time whether or not anyone actually
// turned, so it does not GUARANTEE the motion the depth check depends on, and
// a full ten seconds is also longer than most people need once they are
// actually being told what to do and confirmed to have done it.
//
// So each instruction is verified against the person's own measured pose
// before the next one is shown, reusing /api/enroll/pose-check's existing
// named steps (left/right/up/down, judged relative to a captured baseline) -
// the same endpoint and thresholds the old guided-multiview flow used, just
// without that flow's ring visualisation. Recording stops once every step has
// been measured complete, however long that actually took.
//
// A REQUIRED step (see GUIDED_REQUIRED) never gives up on its own: the old
// behaviour moved on after one timeout and only found out the turn was
// missing once the whole clip had already been recorded, discarded it, and
// made the person start over from "hold still". Asking again, on the SAME
// recording, is strictly better - so a required step keeps re-prompting
// until it is measured or the recording itself is about to run out. An
// OPTIONAL step (up/down) still gets one timed attempt and moves on, because
// the server does not require them and stalling on one would only make a
// successful attempt take longer for no gain.
//
// GUIDED_CAPTURE_MAX_MS is that outer backstop, not a target: if pose
// measurement never works at all (bad light, an unreliable estimate for this
// face, a network hiccup) this is the ceiling a required step's retries are
// budgeted against, so normal use should never come close to it and a truly
// stuck attempt still ends rather than recording forever. The server's own
// liveness check on the finished clip remains the real gate either way; this
// sequence exists to elicit good motion, not to replace it.
// 40s, not 90s: uploads are capped (25MB by the app, 30MB at the proxy), and at
// the recording bitrate below a 90s clip was far past it - refused with a
// plain page the app could only report as "Could not reach the server".
const GUIDED_CAPTURE_MAX_MS = 40000;

// Turns that must be CONFIRMED during the guided capture before the clip is
// even uploaded. Mirrors the server's ENROL_REQUIRED_POSES (config.py), which
// is the actual gate: the server re-checks the uploaded video itself and
// refuses a clip that does not show these, because what a browser reports is
// not evidence. This copy exists only so an attempt that plainly missed a
// turn is redone at once, without waiting on an upload the server will refuse.
// "centre" is the hold-still baseline this sequence always starts from.
const GUIDED_REQUIRED = ['left', 'right'];

// KEYS ARE CAMERA-IMAGE DIRECTIONS, WORDS ARE THE PERSON'S. The frames the
// server judges are not mirrored, so turning to your own RIGHT moves your nose
// toward the image's LEFT - which is what pose-check and the clip check call
// 'left'. The text used to say "your LEFT" for key 'left', so everyone was
// told the opposite of what was being measured, and "Other way" fired at
// people doing exactly as asked. The arrows point in the mirrored preview,
// where your own right is on the screen's right.
//
// Only the two turns the server requires. Up/down were optional, unchecked by
// the server, and cost up to seven seconds each for anyone whose tilt did not
// register.
const GUIDED_DIRECTIONS = [
    { key: 'right', text: 'Slowly turn your head to your LEFT',  arrow: 'left'  },
    { key: 'left',  text: 'Slowly turn your head to your RIGHT', arrow: 'right' },
];

function _arrowSvg(direction) {
    // Same stroke-based style as every other icon in this file, so it reads as
    // part of the app rather than a dropped-in graphic.
    const rot = { left: 180, right: 0, up: -90, down: 90 }[direction] ?? 0;
    return `<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                 style="transform:rotate(${rot}deg)">
                <path d="M5 12h14M13 6l6 6-6 6"/>
            </svg>`;
}

/* The two states the tracking overlay can be in.
 *
 * Read by the canvas AND by the on-screen legend, because the legend's whole
 * job is to say what these colours mean - a swatch that has drifted from the
 * dots it describes teaches the wrong thing, and nothing would catch it. */
/* On-device turn detection, used during the guided recording whenever the
 * face landmarker is running (see facemesh.js pose() for how its angles were
 * checked against the server's on 303 labelled frames).
 *
 * WHY NOT THE SERVER. Each server judgement was a JPEG encoded, uploaded,
 * decoded and run through detection - ~50 ms of compute behind a lock every
 * phone shares, plus the round trip - so a turn was confirmed a beat after it
 * happened. And its yaw comes from five landmarks: a still face wobbled a
 * median 6.3 deg between frames, against a 12 deg threshold. The landmarker
 * is already running for the dots, at video rate, and wobbles 2.1.
 *
 * NOTHING IS TRUSTED FROM HERE. The server still re-checks the uploaded clip
 * itself (_verify_enrol_poses) and refuses one that does not show the turns.
 * These numbers exist so that what this says "Got it" to, the server agrees
 * with:
 *   - REL: a turn is measured from the person's own straight-ahead, like the
 *     server's live check, so a head that rests slightly turned is not
 *     counted as turned.
 *   - ABS: and it must ALSO be past this in absolute terms, because the clip
 *     check is absolute (12 deg on the server's scale). The landmarker reads
 *     ~0.8x the server; the server called 95% of its left/right frames at 18
 *     deg or more here, while resting faces stayed under 3.1.
 *   - HOLD: the clip check looks at ~60 frames spread across the whole clip,
 *     so a turn has to last a couple of those gaps or it can fall between
 *     them. The gap grows with the clip, so the hold does too.
 * Pitch (the optional up/down) has no absolute bar: its absolute value is
 * dominated by where the phone is held, so only the change means anything. */
// Lowered from 12/18, which asked for a bigger turn than the clip check needs.
// 14 on this scale is ~17.5 on the server's, still clear of its 12; on the
// labelled frames 95% of real turns read 17.9 or more, so a genuine turn clears
// it at once instead of after "turn further" prompts.
const LOCAL_TURN_REL = 10;
const LOCAL_TURN_ABS = 14;
const LOCAL_TILT_REL = 15;
const LOCAL_CENTRE_MAX_YAW = 12;
const LOCAL_CENTRE_SAMPLES = 10;      // ~0.5 s of steady straight-ahead readings
const LOCAL_HOLD_MIN_MS = 600;
const ENROL_CLIP_SAMPLE_FRAMES = 60;  // config.ENROL_POSE_SAMPLE_FRAMES
const LOCAL_POSE_STALE_MS = 400;      // older than this is not a live reading
const LOCAL_POSE_GIVEUP_MS = 2500;    // this long with none: hand back to the server

/* Spoken prompts during a recording. The phone is at arm's length and the
   person is turning their head, so a line of text is easy to miss. Speech
   runs after the tap that opened the camera, which is what browsers require.
   Never fatal: no voice is simply no voice. */
/* Australian English. Setting only `lang` is a hint many browsers ignore -
   they keep speaking in the device's default voice - so an installed en-AU
   voice is chosen explicitly when there is one (iOS: Karen/Lee, Android
   Chrome: Google English (Australia), Windows: Catherine/James). The voice
   list loads asynchronously and is often empty on the first call, hence the
   cache refreshed on voiceschanged. Android reports the tag as en_AU.
   A device with no Australian voice gets the nearest: New Zealand, then
   British, then any English except Indian - never silently the default, which
   on many phones here is the Indian voice this replaced. */
const SPEECH_LANG = 'en-AU';
const SPEECH_PREFERENCE = ['AU', 'NZ', 'GB'];
let speechVoice = null;
function pickSpeechVoice() {
    try {
        const voices = window.speechSynthesis.getVoices() || [];
        const region = v => ((v.lang || '').match(/^en[-_]([a-z]{2})/i) || [])[1];
        // A local voice starts speaking at once; a network one lags the prompt.
        const best = list => list.find(v => v.localService) || list[0] || null;
        speechVoice = null;
        for (const r of SPEECH_PREFERENCE) {
            speechVoice = best(voices.filter(v => (region(v) || '').toUpperCase() === r));
            if (speechVoice) break;
        }
        if (!speechVoice) {
            speechVoice = best(voices.filter(v => region(v) && region(v).toUpperCase() !== 'IN'));
        }
    } catch { speechVoice = null; }
    return speechVoice;
}
if ('speechSynthesis' in window) {
    pickSpeechVoice();
    try {
        window.speechSynthesis.addEventListener('voiceschanged', pickSpeechVoice);
    } catch {
        window.speechSynthesis.onvoiceschanged = pickSpeechVoice;
    }
}

function speak(text) {
    try {
        if (!text || !('speechSynthesis' in window)) return;
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(text);
        const voice = speechVoice || pickSpeechVoice();
        if (voice) u.voice = voice;
        u.lang = voice ? voice.lang : SPEECH_LANG;
        u.rate = 1;
        window.speechSynthesis.speak(u);
    } catch { /* no audio */ }
}

const POSE_ACCENT_WAIT = '#f59e0b';   // amber: a face, but not usable yet
const POSE_ACCENT_GOOD = '#22c55e';   // green: framed, and the shutter is live

async function openClipCapture(opts) {
    /* Every pose-check poll goes through here.
     *
     * It carries the signup token when there is one - self-registration has no
     * session, so without it every poll is a 403 - and it is quiet, because
     * these run several times a second and each caller already counts failures
     * and reports a run of them once. Getting either wrong is invisible until
     * somebody is standing in front of a camera that will not respond. */
    async function pollPose(fd) {
        if (opts.signupToken) fd.append('signup_token', opts.signupToken);
        return api.postForm('/api/enroll/pose-check', fd, 'POST', true);
    }

    openModal(opts.title || 'Record clip', `
        <div class="camera-container" id="clip-cap-camera">
            <video id="clip-cap-video" class="camera-video" autoplay playsinline muted></video>
            <canvas id="clip-cap-overlay" class="camera-overlay"></canvas>
            <div class="rec-hint" id="clip-cap-hint">Looking for a face...</div>
            <div id="clip-cap-analyzing" style="position:absolute;top:12px;left:50%;transform:translateX(-50%);
                 background:rgba(0,0,0,0.62);color:#fff;padding:6px 14px;border-radius:999px;font-size:13px;
                 font-weight:600;white-space:nowrap;z-index:5;min-height:18px"></div>
            <div id="clip-cap-processing" style="display:none;position:absolute;inset:0;align-items:center;
                 justify-content:center;background:rgba(0,0,0,0.6);color:#fff;font-size:18px;font-weight:600;
                 z-index:6;text-align:center;padding:16px">Processing, please wait....</div>
            <!-- Shown only while recording. Separate from #clip-cap-hint on
                 purpose: the pose-check poll used for pre-recording framing
                 (tick()) keeps overwriting that pill, which would fight the
                 guided sequence for control of the same element - so the
                 framing poll is stopped for the duration of the recording and
                 this element takes over instead. -->
            <div class="rec-prompt hidden" id="clip-cap-prompt">
                <div class="rec-prompt-arrow" id="clip-cap-prompt-arrow"></div>
                <div class="rec-prompt-text" id="clip-cap-prompt-text"></div>
                <!-- The server's own words for THIS frame ("turn further",
                     "hold the phone at eye level"). pose-check has always
                     returned it and the guided sequence threw it away, so the
                     only feedback was a prompt that changed on a timer. -->
                <div class="rec-prompt-live" id="clip-cap-prompt-live"></div>
            </div>
            <!-- Unguided recording: the blink prompt and the time left for it,
                 watched on the phone. See watchBlink. -->
            <div class="rec-move hidden" id="clip-cap-move" aria-live="polite">
                <div id="clip-cap-move-text"></div>
                <div class="rec-move-bar"><div class="rec-move-fill" id="clip-cap-move-fill"></div></div>
            </div>
            <div class="camera-controls" style="justify-content:center">
                <!-- Front / rear camera. Shown only on a device that has both,
                     and never while recording - see flipCamera. -->
                <button type="button" class="camera-flip" id="clip-cap-flip" aria-label="Switch camera" hidden>
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M4 9a8 8 0 0 1 14-3l2 2"/><path d="M20 4v4h-4"/>
                        <path d="M20 15a8 8 0 0 1-14 3l-2-2"/><path d="M4 20v-4h4"/>
                        <circle cx="12" cy="12" r="2.5"/>
                    </svg>
                </button>
                <!-- Never shown: recording starts by itself once the face is
                     framed, on every attempt, so there is nothing to press. Kept
                     for the progress ring's markup and nothing else. -->
                <button type="button" class="camera-shutter" id="clip-cap-shutter"
                        aria-label="Recording starts automatically" disabled hidden>
                    <svg class="rec-ring" viewBox="0 0 44 44" aria-hidden="true">
                        <circle class="rec-ring-track" cx="22" cy="22" r="20"></circle>
                        <circle class="rec-ring-fill" id="clip-cap-ring" cx="22" cy="22" r="20"></circle>
                    </svg>
                    <div class="shutter-inner"></div>
                </button>
            </div>
        </div>
        <div id="clip-cap-status" class="text-sm text-muted mt-3">
            ${Charts.esc(opts.intro || "Hold the phone at arm's length. Follow the on-screen prompts - "
                                      + "recording stops automatically once every step is done.")}
        </div>
        <!-- What the dots on the face mean. People saw them change colour and
             had no way to know that green was the signal they were waiting
             for, or that the record button stays disabled until it appears -
             the button looked broken rather than not-yet-ready. -->
        <div class="clip-legend" id="clip-cap-legend">
            <span class="clip-legend-item">
                <i class="clip-dot" id="clip-dot-wait"></i>Keep adjusting
            </span>
            <span class="clip-legend-item">
                <i class="clip-dot" id="clip-dot-good"></i>Green - ready to record
            </span>
        </div>`);

    const video   = document.getElementById('clip-cap-video');
    const overlay = document.getElementById('clip-cap-overlay');
    const hint    = document.getElementById('clip-cap-hint');
    const analyzing  = document.getElementById('clip-cap-analyzing');
    const processing = document.getElementById('clip-cap-processing');
    const moveBox  = document.getElementById('clip-cap-move');
    const moveText = document.getElementById('clip-cap-move-text');
    const moveFill = document.getElementById('clip-cap-move-fill');

    // "Please hold - Analyzing", typed out letter by letter and looped, while
    // the camera looks for a usable face. Hidden once recording starts.
    const TYPE_TEXT = 'Please hold - Analyzing';
    let typeTimer = 0;
    function showAnalyzing(on) {
        if (!analyzing) return;
        if (!on) {
            clearInterval(typeTimer); typeTimer = 0;
            analyzing.style.display = 'none';
            return;
        }
        analyzing.style.display = '';
        if (typeTimer) return;
        let i = 0;
        typeTimer = setInterval(() => {
            i = (i + 1) % (TYPE_TEXT.length + 10);          // a short pause after each pass
            const n = Math.min(i, TYPE_TEXT.length);
            analyzing.textContent = TYPE_TEXT.slice(0, n) + (i <= TYPE_TEXT.length ? '▌' : '');
        }, 90);
    }
    // Over the frozen camera while the server checks the recording.
    const showProcessing = on => { if (processing) processing.style.display = on ? 'flex' : 'none'; };
    showAnalyzing(true);
    const shutter = document.getElementById('clip-cap-shutter');
    const ring    = document.getElementById('clip-cap-ring');
    const status  = document.getElementById('clip-cap-status');
    // Width of the frames the framing guide sends to the server. 480 suits a
    // selfie at arm's length; scanning somebody a metre or two from the rear
    // camera shrinks their face below what the detector can see at 480, so
    // the guide said "No face detected" over a face the dots were tracking.
    const FRAME_W = opts.frameWidth || 480;

    // Painted here rather than in the stylesheet so the swatches and the dots
    // cannot disagree; see POSE_ACCENT_WAIT / POSE_ACCENT_GOOD.
    const dotWait = document.getElementById('clip-dot-wait');
    const dotGood = document.getElementById('clip-dot-good');
    if (dotWait) dotWait.style.background = POSE_ACCENT_WAIT;
    if (dotGood) dotGood.style.background = POSE_ACCENT_GOOD;

    const cam = new CameraCapture(video, null, null);
    // Front camera photographs the holder; Take Attendance scans somebody else.
    cam.facingMode = opts.facingMode || 'user';
    if (await cam.start() === false) { closeModal(); return; }

    const state = { busy: false, timer: null, box: null, landmarks: null, good: false,
                    goodStreak: 0, recording: false, alive: true, fails: 0, closed: false,
                    mesh: null, pose: null, raf: 0 };
    // A single good poll used to be enough to turn the dots green AND arm the
    // shutter - the same instant. At close range a detector reading can
    // flicker frame to frame (a second face candidate appearing and vanishing
    // between two 350ms polls, a blink, a moment of motion blur), and a tap
    // landing in that one-frame window recorded a clip the very next poll
    // would have refused. Requiring a short run of consecutive good frames
    // before arming - never before disarming, which stays immediate - means
    // the green the person sees is the same green the shutter is honouring,
    // not a single lucky frame.
    const GOOD_STREAK_TO_ARM = 2;
    const setRing = p => { if (ring) ring.style.strokeDashoffset = String(126 * (1 - p)); };

    /* Front or rear camera, on the capture screen itself. Each flow opens the
       one it expects - rear for Take Attendance, front for a selfie - but a
       coach registering an athlete is pointing the phone at somebody else and
       needs the rear one, and a scan can need the front. Hidden on a device
       with only one camera, and while recording: switching mid-clip would cut
       the very video the server judges. The choice holds for the rest of this
       capture, including the next athlete after a scan. */
    const flipBtn = document.getElementById('clip-cap-flip');
    const flipLabel = () => (cam.facingMode === 'user' ? 'Switch to rear camera' : 'Switch to front camera');
    const setFlipVisible = on => { if (flipBtn) flipBtn.hidden = !on || !state.canFlip; };
    async function flipCamera() {
        if (!flipBtn || state.recording || state.flipping || state.closed || !cam.isActive) return;
        state.flipping = true;
        flipBtn.disabled = true;
        const was = cam.facingMode;
        // Nothing seen through the old lens may arm the shutter through the new one.
        state.good = false; state.goodStreak = 0; state.goodHist = [];
        state.mesh = null; state.meshT = 0; state.meshW = 0; state.pose = null;
        state.box = null; state.landmarks = null; state.autoFired = false;
        shutter.disabled = true;
        showAnalyzing(true);
        try {
            await cam.switchCamera();
            if (!cam.isActive && !state.closed) {
                // The other camera would not open: go back to the one that did.
                cam.facingMode = was;
                await cam.start();
            }
        } finally {
            if (state.closed) { try { cam.stop(); } catch { /* already stopped */ } }
            state.flipping = false;
            flipBtn.disabled = false;
            flipBtn.setAttribute('aria-label', flipLabel());
            flipBtn.title = flipLabel();
            draw();
        }
    }
    if (flipBtn) flipBtn.addEventListener('click', flipCamera);
    // Device labels need permission, but the COUNT is available once the
    // camera has opened, which it has by this point.
    (async () => {
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            state.canFlip = devices.filter(d => d.kind === 'videoinput').length > 1;
        } catch { state.canFlip = false; }
        if (flipBtn) { flipBtn.setAttribute('aria-label', flipLabel()); flipBtn.title = flipLabel(); }
        setFlipVisible(!state.recording);
    })();

    // Stop everything however the modal closes - X, Escape, or a route change.
    // A stream left running behind a closed dialog keeps the camera light on.
    const teardown = () => {
        state.alive = false;
        state.closed = true;          // one-way: nothing may restart after this
        if (state.timer) clearInterval(state.timer);
        if (state.raf) cancelAnimationFrame(state.raf);
        state.raf = 0;
        state.mesh = null;
        state.pose = null;
        clearInterval(typeTimer);
        try { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); } catch { /* ok */ }
        try { cam.stop(); } catch { /* already stopped */ }
    };
    const modal = document.getElementById('modal-container');
    const observer = new MutationObserver(() => {
        if (modal.classList.contains('hidden')) { teardown(); observer.disconnect(); }
    });
    observer.observe(modal, { attributes: true, attributeFilter: ['class'] });

    function draw() {
        if (!state.alive || !video.videoWidth) return;
        const r = video.getBoundingClientRect();
        // A zero rect happens transiently - the modal mid-open, or a
        // backgrounded tab. Sizing to it would blank the overlay until the next
        // resize, so keep the last good size instead.
        if (r.width < 1 || r.height < 1) return;
        if (overlay.width !== Math.round(r.width) || overlay.height !== Math.round(r.height)) {
            overlay.width = Math.round(r.width);
            overlay.height = Math.round(r.height);
        }
        const ctx = overlay.getContext('2d');
        ctx.clearRect(0, 0, overlay.width, overlay.height);

        // Geometry shared by both sources: the video is object-fit: cover, so
        // part of it is cropped, and the preview is mirrored for the front
        // camera. Anything drawn on top has to undo both or it drifts.
        const sw0 = video.videoWidth, sh0 = video.videoHeight;
        const sc = Math.max(overlay.width / sw0, overlay.height / sh0);
        const ox = (overlay.width - sw0 * sc) / 2;
        const oy = (overlay.height - sh0 * sc) / 2;
        const mirror = cam.facingMode === 'user';
        const toScreen = (px, py) => [
            (mirror ? sw0 - px : px) * sc + ox,
            py * sc + oy,
        ];

        // Preferred: the on-device mesh. 478 points at video rate.
        if (state.mesh) {
            const accentM = state.good ? POSE_ACCENT_GOOD : POSE_ACCENT_WAIT;
            // Small and semi-transparent: 478 opaque dots read as a blob and
            // hide the face they are meant to be tracking.
            const r = Math.max(0.8, Math.min(1.8, overlay.width * 0.0035));
            ctx.fillStyle = accentM;
            ctx.globalAlpha = 0.75;
            for (let i = 0; i < state.mesh.length; i++) {
                const lm = state.mesh[i];
                const [X, Y] = toScreen(lm.x * sw0, lm.y * sh0);
                ctx.beginPath();
                ctx.arc(X, Y, r, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.globalAlpha = 1;
            return;
        }

        if (!state.box) return;

        // pose-check was sent a 480px-wide frame, so the box is in those
        // coordinates. Scale to the drawn video, allowing for the cover crop.
        const sw = video.videoWidth, sh = video.videoHeight;
        const scale = Math.max(overlay.width / sw, overlay.height / sh);
        const dx = (overlay.width - sw * scale) / 2;
        const dy = (overlay.height - sh * scale) / 2;
        const k = sw / Math.min(FRAME_W, sw);     // the width the frame was sent at
        let [x1, y1, x2, y2] = state.box.map(v => v * k);
        // The preview is mirrored for the front camera, so the box must be too,
        // or it tracks the opposite way as the head moves.
        if (cam.facingMode === 'user') { const t = x1; x1 = sw - x2; x2 = sw - t; }

        const X = x1 * scale + dx, Y = y1 * scale + dy;
        const W = (x2 - x1) * scale, H = (y2 - y1) * scale;
        const accent = state.good ? POSE_ACCENT_GOOD : POSE_ACCENT_WAIT;

        // A light frame, kept thin - it says where the face is, and the dots
        // below say the face is actually being tracked.
        ctx.strokeStyle = accent;
        ctx.lineWidth = 2;
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(X, Y, W, H, 10); else ctx.rect(X, Y, W, H);
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Landmark dots, drawn the way the KIRTI analyzer draws pose joints:
        // a dark casing under a coloured joint, so they stay readable over
        // both a bright face and a dark room. YuNet gives five - right eye,
        // left eye, nose, right mouth corner, left mouth corner.
        const pts = (state.landmarks || []).map(([lx, ly]) => {
            let px = lx * k;
            // Mirrored for the front camera, exactly as the box is, or the
            // dots drift the wrong way the moment the head moves.
            if (cam.facingMode === 'user') px = sw - px;
            return [px * scale + dx, ly * k * scale + dy];
        });
        if (pts.length === 5) {
            const [rEye, lEye, nose, rMouth, lMouth] = pts;
            const bones = [[rEye, lEye], [rEye, nose], [lEye, nose],
                           [nose, rMouth], [nose, lMouth], [rMouth, lMouth]];
            const r = Math.max(2.5, Math.min(6, W * 0.035));
            // Casing first, then the bone inside it - the same two-pass trick
            // that keeps a skeleton legible against any background.
            ctx.lineCap = 'round';
            ctx.strokeStyle = 'rgba(0,0,0,0.45)';
            ctx.lineWidth = Math.max(3, r * 1.1);
            bones.forEach(([a, b]) => {
                ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
            });
            ctx.strokeStyle = 'rgba(253,252,248,0.9)';
            ctx.lineWidth = Math.max(1.5, r * 0.5);
            bones.forEach(([a, b]) => {
                ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
            });
            pts.forEach(([px, py]) => {
                ctx.beginPath(); ctx.arc(px, py, r + 1.5, 0, Math.PI * 2);
                ctx.fillStyle = 'rgba(0,0,0,0.5)'; ctx.fill();
                ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
                ctx.fillStyle = accent; ctx.fill();
            });
        }
    }

    /* How often the framing guide asks the server. Slow is not a penalty: it
     * is for a server that is already struggling or rate-limiting, which three
     * requests a second makes worse. A success puts it straight back to fast. */
    const FAST_POLL_MS = 350;
    const SLOW_POLL_MS = 1500;
    let framePollMs = FAST_POLL_MS;
    const setFramePoll = (ms) => {
        if (framePollMs === ms) return;
        framePollMs = ms;
        // Only re-arm a timer that is already running. Recording clears it on
        // purpose so the guided sequence owns pose-check, and resurrecting it
        // here would put two pollers on the same camera.
        if (state.timer && !state.closed) {
            clearInterval(state.timer);
            state.timer = setInterval(tick, ms);
        }
    };

    /* What a failed poll actually was, in words that match it.
     *
     * Returning null means "not worth counting" - one frame the server could
     * not read is not a fault in the camera or the connection.
     *
     * `fatal` means retrying cannot help: the caller is no longer allowed to
     * use the guide, so the honest thing is to stop and say why. Everything
     * else keeps polling, because it may well come back - and on a phone,
     * usually does. */
    function pollFailure(err) {
        const code = err && err.status;
        if (code === 403) {
            return opts.signupToken
                // The signup token is the only credential an applicant has and
                // it expires. Telling them the connection dropped sends them
                // to look at their wifi instead of starting again.
                ? { text: 'This registration has timed out - close and start again.',
                    fatal: true }
                : { text: 'Your session has expired - sign in again.', fatal: true };
        }
        if (code === 400) return null;             // an unreadable frame, not a streak
        if (code === 429) return { text: 'Too busy - still trying\u2026', fatal: false };
        if (code >= 500) return { text: 'The server is having trouble - still trying\u2026',
                                  fatal: false };
        return { text: 'Connection trouble - still trying\u2026', fatal: false };
    }

    /* The guided sequence's version: same classification, but it reports
     * through the live prompt and tells the loop whether to give up.
     *
     * Both guided loops used to swallow every failure and retry until they
     * timed out. With an expired signup token that is six seconds of "hold
     * still" followed by four seven-second turns - over half a minute of
     * somebody dutifully turning their head at a camera whose every frame is
     * being refused - and then a recording that could only ever be rejected.
     * Nothing on screen said a word about it. */
    function guidedPollFailed(err) {
        const f = pollFailure(err);
        if (!f) return false;                 // one unreadable frame; retry
        if (!f.fatal) return false;           // transient; retry, as before
        setPromptLive(f.text);
        return true;
    }

    function grab(maxW) {
        if (!video.videoWidth) return null;
        const c = document.createElement('canvas');
        const s = Math.min(1, maxW / video.videoWidth);
        c.width = Math.round(video.videoWidth * s);
        c.height = Math.round(video.videoHeight * s);
        c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
        return c;
    }

    // The on-device mesh, only while its readings are live.
    const meshFresh = () =>
        (state.mesh && state.meshT && Date.now() - state.meshT <= LOCAL_POSE_STALE_MS) ? state.mesh : null;

    // Mesh width in the RECORDED video's pixels, the unit the server's
    // too-far rule is written in. Points are normalised to the raw frame, so
    // the preview's mirror and cover crop do not enter into it.
    function meshWidthPx(pts) {
        let lo = 1, hi = 0;
        for (const p of pts) { if (p.x < lo) lo = p.x; if (p.x > hi) hi = p.x; }
        return Math.max(0, hi - lo) * (video.videoWidth || 0);
    }

    // Only the unguided clip is gated on this. Registration's guided capture
    // is a selfie at arm's length, already far past it.
    const tooFar = () => opts.guided === false && !!meshFresh() && state.meshW < LIVE_MIN_MESH_PX;

    /* Keeps the recording going until the person blinks - see BLINK_* - then a
     * moment longer so the eyes are seen opening again. The bar is the time
     * left; it turns green on the blink. Resolves { ok } or { ok: false, reason }. */
    async function watchBlink(control) {
        const t0 = Date.now();
        const detector = makeBlinkDetector();
        let seen = false, blinkAt = 0, lastSpoken = '';
        const say = (text, voice) => {
            if (moveText && moveText.textContent !== text) moveText.textContent = text;
            if (voice && lastSpoken !== text) { lastSpoken = text; speak(text); }
        };
        state.onMesh = (pts, t) => {
            const W = video.videoWidth || 1, H = video.videoHeight || 1;
            if (detector.push(eyeAspect(pts, W, H), t) && !blinkAt) blinkAt = Date.now();
        };
        if (moveBox) moveBox.classList.remove('hidden');
        if (moveFill) { moveFill.style.width = '0%'; moveFill.classList.remove('ok'); }
        say('Look at the camera and blink', true);
        try {
            while (!state.closed && !control.done) {
                const elapsed = Date.now() - t0;
                if (blinkAt) {
                    if (moveFill) { moveFill.style.width = '100%'; moveFill.classList.add('ok'); }
                    say('Got it', true);
                    setRing(1);
                    if (Date.now() - blinkAt >= BLINK_TAIL_MS && elapsed >= CLIP_MS_MIN) {
                        control.done = true;
                        // When, from the start of the recording - the server
                        // looks for the blink around this moment.
                        return { ok: true, blinkAtMs: blinkAt - t0 };
                    }
                } else {
                    const m = meshFresh();
                    if (!m) say('Keep the face in view');
                    else {
                        seen = true;
                        if (state.meshW < LIVE_MIN_MESH_PX) say('Too far - move closer', true);
                        else say('Look at the camera and blink');
                    }
                    const p = Math.min(1, elapsed / CLIP_MS_MAX);
                    if (moveFill) moveFill.style.width = `${Math.round(p * 100)}%`;
                    setRing(p);
                }
                if (elapsed >= CLIP_MS_MAX) {
                    control.done = true;
                    return { ok: false, reason: seen
                        ? 'No blink was seen - look at the camera and blink. Recording again.'
                        : 'The face was not seen while recording - keep it in view. Recording again.' };
                }
                await new Promise(res => setTimeout(res, 50));
            }
            return { ok: false, reason: 'Recording did not complete. Trying again.' };
        } finally {
            state.onMesh = null;
            if (moveBox) moveBox.classList.add('hidden');
        }
    }

    async function tick() {
        if (!state.alive || state.busy) return;
        const c = grab(FRAME_W);
        if (!c) return;
        state.busy = true;
        try {
            const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.8));
            // A null blob is a failed ENCODE, not a failed request. Letting it
            // fall through would throw in FormData.append and be counted as a
            // network failure, and five of those dead-end the person at "Lost
            // connection" with the poll cancelled - recoverable only by closing
            // the modal. Skip the frame and keep the streak intact.
            if (!blob) return;
            const fd = new FormData();
            fd.append('frame', blob, 'f.jpg');
            fd.append('step', 'centre');
            const r = await pollPose(fd);
            if (!state.alive) return;

            state.box = r.box || null;
            state.landmarks = r.landmarks || null;
            // "ok" means correctly posed AND framed. Pose does not matter for a
            // clip - the recording captures several angles by itself - so only
            // framing and image quality gate the button.
            // The raw per-frame reading. Used for the streak below, not
            // assigned to state.good directly - see GOOD_STREAK_TO_ARM.
            // 'pitch' (phone below or above eye level) is ADVICE, not a gate.
            // Replayed over 50 real registration clips it was the only reason
            // any frame was ever refused - one in seven, and a tenth of frames
            // in clips that went on to succeed sat past 27 degrees - so a phone
            // held at chest height could keep the dots amber and the shutter
            // locked indefinitely. The 5-point pitch estimate also carries a
            // per-face bias. The advice stays on screen; the portrait is picked
            // from the most frontal frame of the clip regardless.
            const angleOnly = r.reason === 'pitch';
            // Close enough for the depth check, measured on the phone - see
            // LIVE_MIN_MESH_PX. The server's own "Move closer" bar is lower: it
            // judges a downscaled frame for framing, not for depth.
            const far = tooFar();
            if (far && !state.saidFar) { state.saidFar = true; speak('Move closer'); }
            if (!far) state.saidFar = false;
            const rawGood = !!r.box && !far && (r.ok || r.reason === 'pose' || angleOnly);
            // Two of the last three polls, not two in a row. On a phone one poll
            // in a pair often lands on a blink or a hand tremor, so "in a row"
            // left people parked on "Hold steady..." with a face clearly in shot.
            // The server's check on the finished clip is still the real gate.
            state.goodHist = [...(state.goodHist || []), rawGood].slice(-3);
            state.goodStreak = state.goodHist.filter(Boolean).length;
            // "ok" means correctly posed AND framed. Pose does not matter for a
            // clip - the recording captures several angles by itself - so only
            // framing and image quality gate the button. Disarm is immediate -
            // one bad frame is enough - arm requires the streak, so a single
            // flickering good frame cannot open the shutter on its own.
            state.good = state.goodStreak >= GOOD_STREAK_TO_ARM;
            hint.textContent = state.good
                ? (state.recording ? 'Recording - keep moving gently'
                    : angleOnly ? `Starting - ${(r.message || '').toLowerCase()} if you can`
                    : 'Face found - starting')
                : rawGood
                    ? 'Hold steady…'
                    : far ? 'Too far - move closer' : (r.message || 'No face detected');
            if (!state.recording) shutter.disabled = !state.good;
            showAnalyzing(!state.good && !state.recording);
            // Scanning several people: wait for the last face to leave the frame
            // before recording again, or the same athlete is recorded on a loop.
            if (!rawGood && opts.rearmOnNoFace) state.autoFired = false;
            // AUTO-RECORD. The green dots are the go signal - nobody taps.
            if (state.good && !state.recording && !state.autoFired && !state.flipping) {
                state.autoFired = true;
                startRecording();
            }
            // Recovered. Anything the last failure put on screen has just been
            // overwritten by a real answer, so drop back to the fast poll.
            state.fails = 0;
            setFramePoll(FAST_POLL_MS);
            draw();
        } catch (err) {
            /* THIS USED TO END THE CAPTURE. Five consecutive failures - at a
             * 350ms poll, 1.75 seconds - cleared the timer, disabled the
             * shutter and printed "Lost connection - close and try again".
             *
             * Two things were wrong with that. It was usually untrue: a 403
             * from an expired signup token, a 429, or a 500 is not a lost
             * connection, and somebody sent to check their wifi cannot fix any
             * of them. And it was permanent - nothing ever restarted the poll,
             * so a phone that hiccuped for two seconds on a train had to be
             * closed and begun again, which for an applicant means the whole
             * registration.
             *
             * Now the reason is named, the loop keeps trying unless trying is
             * pointless, and one good frame clears it. */
            const f = pollFailure(err);
            if (!f) return;
            state.fails += 1;
            if (f.fatal) {
                if (state.timer) { clearInterval(state.timer); state.timer = null; }
                hint.textContent = f.text;
                shutter.disabled = true;
                return;
            }
            // Wait for a short run before saying anything: single dropped
            // frames are normal and a message that flickers on every one of
            // them is worse than silence.
            if (state.fails >= 3) {
                hint.textContent = f.text;
                shutter.disabled = true;
                setFramePoll(SLOW_POLL_MS);
            }
        } finally {
            state.busy = false;
        }
    }

    const ui = {
        status(text, isHtml) {
            if (isHtml) status.innerHTML = text; else status.textContent = text;
        },
        async resume({ rearm = false } = {}) {
            // Refuse once the modal has been closed. onClip callbacks await a
            // server round trip and then call this on a refusal, so the close
            // can land WHILE that request is in flight - and by then teardown
            // has run and disconnected the observer, so nothing would ever stop
            // the camera or the 350ms poll again. Without this guard, closing
            // the dialog mid-upload left the camera light on and pose-check
            // firing three times a second for the life of the page.
            if (state.closed || modal.classList.contains('hidden')) return false;
            if (await cam.start() === false) return false;
            if (state.closed) { try { cam.stop(); } catch {} return false; }  // closed while starting
            state.alive = true;
            state.fails = 0;
            // Back to looking for a face: analyzing banner on, processing off,
            // and auto-record armed again (or armed once the face has left, when
            // scanning one person after another).
            // `rearm`: record again straight away. After a refusal the same
            // person is still in front of the camera, and making them step out
            // of frame and back before anything happened read as a hang.
            state.autoFired = !!opts.rearmOnNoFace && !rearm;
            showProcessing(false);
            hint.classList.remove('hidden');
            showAnalyzing(true);
            // NOT shutter.disabled = false here. This used to enable the
            // shutter the instant the camera restarted - before tick() had
            // run even once for the retry - so a refusal's "try again" could
            // be followed by a tap that recorded before anything was checked
            // at all. Leave it disabled; the first good, STABLE poll (see
            // GOOD_STREAK_TO_ARM) is what is allowed to arm it.
            state.good = false;
            state.goodStreak = 0; state.goodHist = [];
            shutter.disabled = true;
            if (state.timer) clearInterval(state.timer);
            framePollMs = FAST_POLL_MS;
            state.timer = setInterval(tick, framePollMs);
            return true;
        },
        close() { teardown(); closeModal(); },
    };

    state.timer = setInterval(tick, framePollMs);
    tick();

    // On-device landmarks, if the runtime was fetched at build time. This is
    // what makes the dots track rather than step: the server poll above is a
    // round trip roughly three times a second, while this runs at video rate
    // and returns 478 points instead of five. Entirely optional - when
    // frontend/vendor is absent FaceMesh.load() resolves null and the overlay
    // keeps using the five server points.
    (async () => {
        const ok = await FaceMesh.load();
        if (!ok || state.closed) return;
        const loop = () => {
            if (state.closed) return;
            const r = FaceMesh.detect(video, performance.now());
            if (r) {
                state.mesh = r.points;
                // Stamped, so the guided sequence can tell a live reading from
                // the last one left behind when the face leaves the frame.
                state.pose = { yaw: r.yaw, pitch: r.pitch, t: Date.now() };
                // Face size in the recorded video's pixels - see LIVE_MIN_MESH_PX.
                state.meshW = meshWidthPx(r.points);
                state.meshT = state.pose.t;
                // Every mesh reading, at video rate, while a blink is being
                // watched for - a 50ms poll can step straight over one.
                if (state.onMesh) state.onMesh(r.points, state.meshT);
            }
            // Only the mesh path redraws here; the server path redraws on its
            // own poll, so a dropped mesh frame never blanks the overlay.
            if (state.mesh) draw();
            state.raf = requestAnimationFrame(loop);
        };
        state.raf = requestAnimationFrame(loop);
    })();

    const promptBox   = document.getElementById('clip-cap-prompt');
    const promptText  = document.getElementById('clip-cap-prompt-text');
    const promptArrow = document.getElementById('clip-cap-prompt-arrow');
    const promptLive  = document.getElementById('clip-cap-prompt-live');

    /* What the server said about the frame just sent. Cleared between steps so
       advice for the previous turn cannot linger over the next one. */
    function setPromptLive(text) {
        if (promptLive) promptLive.textContent = text || '';
    }

    /* A turn was actually measured. Say so - visibly, and for long enough to
       be seen at arm's length - before moving on. Without this, doing it right
       and doing nothing at all looked identical. */
    async function flashStepDone(text) {
        promptBox.classList.add('done');
        promptText.textContent = text || 'Got it';
        promptArrow.innerHTML = '';
        promptArrow.classList.add('hidden');
        setPromptLive('');
        if (navigator.vibrate) navigator.vibrate([25, 40, 25]);
        speak(text || 'Got it');
        await new Promise(res => setTimeout(res, 450));
        promptBox.classList.remove('done');
    }

    function setPromptStep(step) {
        promptBox.classList.remove('done');
        setPromptLive('');
        promptText.textContent = step.text;
        promptArrow.innerHTML = step.arrow ? _arrowSvg(step.arrow) : '';
        promptArrow.classList.toggle('hidden', !step.arrow);
        // A beat of haptic feedback on each new instruction - the phone is
        // usually held at arm's length during this, where a small on-screen
        // text change is easy to miss.
        if (navigator.vibrate) navigator.vibrate(25);
        speak(step.text);
    }

    /** Wait for one frame captured DURING an active recording to satisfy one
     *  named pose-check step, polling at its own pace independent of the
     *  pre-recording framing loop (which is stopped for the duration - see
     *  the shutter handler). Resolves the measured response on success, or
     *  null if `timeoutMs` passes first - the caller decides what "gave up"
     *  means, this function only reports which one happened.
     *
     *  `failFlag`, if passed, is set to `{ fatal: true }` when the reason for
     *  giving up was an unrecoverable poll failure (an expired token, a
     *  fatal HTTP status) rather than a plain timeout - a required step's
     *  retry loop needs that distinction so it stops asking someone to keep
     *  turning their head at a camera that has already given up talking to
     *  the server, instead of spinning silently until the outer deadline. */
    async function waitForStep(stepKey, baseYaw, basePitch, timeoutMs, pollMs, failFlag) {
        const start = Date.now();
        while (!state.closed && Date.now() - start < timeoutMs) {
            // The WHOLE poll attempt is one try/catch, not just the network
            // call. canvas.toBlob() is explicitly allowed by spec to resolve
            // null if encoding fails - rare on an idle desktop browser, far
            // less rare on a real phone under the load a live camera plus a
            // 200ms encode loop puts on it - and fd.append('frame', null, ...)
            // throws a TypeError that a narrower try/catch would not catch,
            // crashing the whole guided sequence with an unhandled rejection
            // mid-registration. One bad frame here must cost one retry, never
            // the capture.
            try {
                const c = grab(480);
                if (c) {
                    const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.8));
                    if (blob) {
                        const fd = new FormData();
                        fd.append('frame', blob, 'f.jpg');
                        fd.append('step', stepKey);
                        if (baseYaw !== null) { fd.append('base_yaw', baseYaw); fd.append('base_pitch', basePitch); }
                        const r = await pollPose(fd);
                        if (r) {
                            if (r.box) { state.box = r.box; draw(); }
                            // The server's coaching for THIS frame. It knows
                            // whether the head is turning the wrong way, not
                            // far enough, or out of frame; the browser was
                            // showing a fixed instruction regardless.
                            if (!r.ok) setPromptLive(r.message || '');
                            if (r.ok) return r;
                        }
                    }
                }
            } catch (err) {
                if (guidedPollFailed(err)) {
                    if (failFlag) failFlag.fatal = true;
                    return null;
                }
            }
            await new Promise(res => setTimeout(res, pollMs));
        }
        return null;
    }

    const sleep = ms => new Promise(res => setTimeout(res, ms));

    /* A still photo at each confirmed step, sent with the clip. They come from
       the same live camera the green dots are drawn over, so they show the face
       the guide actually saw - the server falls back to them when it cannot
       find a face in the compressed video. Never fatal: a failed grab is
       simply skipped. */
    async function snap(control, step) {
        try {
            const c = grab(720);
            if (!c) return;
            const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.92));
            if (blob && control.snapshots.length < 6) control.snapshots.push({ step, blob });
        } catch { /* skip this one */ }
    }
    const median = a => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
    const livePose = () => {
        const p = state.pose;
        return p && p.yaw !== null && Date.now() - p.t <= LOCAL_POSE_STALE_MS ? p : null;
    };

    /** Is this on-device reading the pose `stepKey` asks for? See LOCAL_TURN_*. */
    function judgeLocal(stepKey, p, base) {
        const dy = p.yaw - base.yaw;
        const dp = (p.pitch ?? base.pitch) - base.pitch;
        switch (stepKey) {
        case 'left':
            // Camera-image directions: see GUIDED_DIRECTIONS for why 'left'
            // is the person's own RIGHT.
            return dy <= -LOCAL_TURN_REL && p.yaw <= -LOCAL_TURN_ABS
                ? { ok: true }
                : { ok: false, message: dy >= LOCAL_TURN_REL / 2
                    ? 'Other way - turn to your right' : 'Turn further to your right' };
        case 'right':
            return dy >= LOCAL_TURN_REL && p.yaw >= LOCAL_TURN_ABS
                ? { ok: true }
                : { ok: false, message: dy <= -LOCAL_TURN_REL / 2
                    ? 'Other way - turn to your left' : 'Turn further to your left' };
        case 'up':
            return dp <= -LOCAL_TILT_REL ? { ok: true }
                : { ok: false, message: 'Tilt your chin up a little more' };
        case 'down':
            return dp >= LOCAL_TILT_REL ? { ok: true }
                : { ok: false, message: 'Tilt your chin down a little more' };
        }
        return { ok: false, message: '' };
    }

    /** The straight-ahead baseline, from ~half a second of steady on-device
     *  readings. Null if none arrived in time - the caller falls back to the
     *  server's version rather than guessing. */
    async function captureBaselineLocal(timeoutMs) {
        const start = Date.now();
        const ys = [], ps = [];
        let lastT = 0;
        while (!state.closed && Date.now() - start < timeoutMs) {
            const p = livePose();
            if (!p) {
                setPromptLive('Keep your face in view');
            } else if (p.t !== lastT) {
                lastT = p.t;
                if (Math.abs(p.yaw) < LOCAL_CENTRE_MAX_YAW) {
                    ys.push(p.yaw); ps.push(p.pitch ?? 0);
                    setPromptLive('Hold it…');
                    if (ys.length >= LOCAL_CENTRE_SAMPLES) return { yaw: median(ys), pitch: median(ps) };
                } else {
                    ys.length = 0; ps.length = 0;
                    setPromptLive('Look straight at the camera');
                }
            }
            await sleep(40);
        }
        return null;
    }

    /** waitForStep's on-device twin. Resolves {ok:true} once the pose has been
     *  HELD long enough for the clip check to land on it (see LOCAL_TURN_*),
     *  null on timeout, or {stale:true} when the landmarker has stopped
     *  producing readings - then the caller hands the rest of the sequence to
     *  the server path instead of timing out step after step on a dead feed. */
    async function waitForStepLocal(stepKey, base, timeoutMs, recStart) {
        const start = Date.now();
        let heldSince = 0;
        while (!state.closed && Date.now() - start < timeoutMs) {
            const p = livePose();
            if (!p) {
                heldSince = 0;
                const last = state.pose ? state.pose.t : 0;
                if (Date.now() - Math.max(last, start) > LOCAL_POSE_GIVEUP_MS) return { stale: true };
                setPromptLive('Keep your face in view');
            } else {
                const v = judgeLocal(stepKey, p, base);
                if (!v.ok) {
                    heldSince = 0;
                    setPromptLive(v.message);
                } else {
                    if (!heldSince) heldSince = Date.now();
                    const gap = (Date.now() - recStart) / ENROL_CLIP_SAMPLE_FRAMES;
                    if (Date.now() - heldSince >= Math.max(LOCAL_HOLD_MIN_MS, 2.5 * gap)) return { ok: true };
                    setPromptLive('Hold it…');
                }
            }
            await sleep(40);
        }
        return null;
    }

    /** Drive the person through hold-still, then four verified turns, setting
     *  control.done = true only once that is genuinely complete (or a step's
     *  own timeout gives up on it - see the constant's comment for why that
     *  is the right trade-off rather than trapping someone indefinitely). */
    async function runGuidedSequence(control) {
        const CENTRE_TIMEOUT_MS = 6000;
        const STEP_TIMEOUT_MS = 7000;
        const POLL_MS = 200;
        // A floor under the fast-completion case, not a target: someone who
        // turns quickly could otherwise finish in a couple of seconds, and the
        // backend's frame sampler wants a reasonably sized clip to spread
        // across regardless of how briskly the steps were satisfied.
        const MIN_TOTAL_MS = 3000;
        const t0 = Date.now();

        setPromptStep({ text: 'Hold still, looking at the camera', arrow: null });
        setRing(0);
        let baseYaw = null, basePitch = null, hold = 0;
        // On-device when the landmarker is live - see LOCAL_TURN_REL. The
        // server loop below only runs if this did not produce a baseline.
        let useLocal = !!(FaceMesh.ready && livePose());
        if (useLocal) {
            const base = await captureBaselineLocal(CENTRE_TIMEOUT_MS);
            if (base) { baseYaw = base.yaw; basePitch = base.pitch; } else useLocal = false;
        }
        const centreStart = Date.now();
        while (!state.closed && baseYaw === null && Date.now() - centreStart < CENTRE_TIMEOUT_MS) {
            // Same reasoning as waitForStep: the whole attempt is one
            // try/catch, because canvas.toBlob() resolving null under real
            // device load is a real failure mode a narrower catch would miss,
            // and that must cost one retry rather than crash the sequence.
            try {
                const c = grab(480);
                if (c) {
                    const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.8));
                    if (blob) {
                        const fd = new FormData();
                        fd.append('frame', blob, 'f.jpg');
                        fd.append('step', 'centre');
                        const r = await pollPose(fd);
                        if (r) {
                            if (r.box) { state.box = r.box; draw(); }
                            if (r.ok) {
                                hold++;
                                setPromptLive(hold >= 3 ? '' : 'Hold it\u2026');
                                if (hold >= 3) { baseYaw = r.yaw; basePitch = r.pitch; }
                            } else {
                                hold = 0;
                                setPromptLive(r.message || '');
                            }
                        }
                    }
                }
            } catch (err) { if (guidedPollFailed(err)) break; }
            if (baseYaw === null) await new Promise(res => setTimeout(res, POLL_MS));
        }
        // Framing never stabilised - fall through on an absolute baseline
        // rather than trap someone here. The turns below are still measured
        // and still shown, just against 0 instead of their own straight-ahead
        // reading; the server's liveness check on the finished clip is the
        // actual authority regardless of how this phase went.
        if (baseYaw === null) { baseYaw = 0; basePitch = 0; }
        await snap(control, 'centre');
        setRing(0.2);

        // A required step's retries are budgeted against the SAME clock the
        // recording itself is capped by (GUIDED_CAPTURE_MAX_MS), minus room
        // for whatever optional steps and the final flash/hold still need -
        // otherwise a person could finally get the turn right a moment after
        // cam.recordClip had already stopped the tape, and the server would
        // reject a clip that a "Got it" had just told them was fine.
        const OPTIONAL_BUDGET_MS = STEP_TIMEOUT_MS * (GUIDED_DIRECTIONS.length - GUIDED_REQUIRED.length);
        const requiredDeadline = t0 + GUIDED_CAPTURE_MAX_MS - OPTIONAL_BUDGET_MS - MIN_TOTAL_MS;

        // One step's wait, on-device while that works, otherwise the server.
        // A feed that dies mid-sequence switches for good, and resets the
        // baseline to 0 - the server's angles are on a different scale, and 0
        // is what its own path falls back to.
        const waitFor = async (key, failFlag) => {
            if (useLocal) {
                const r = await waitForStepLocal(key, { yaw: baseYaw, pitch: basePitch },
                                                 STEP_TIMEOUT_MS, t0);
                if (!r || !r.stale) return r;
                useLocal = false; baseYaw = 0; basePitch = 0;
            }
            return waitForStep(key, baseYaw, basePitch, STEP_TIMEOUT_MS, POLL_MS, failFlag);
        };

        const measured = [];
        for (let i = 0; i < GUIDED_DIRECTIONS.length && !state.closed; i++) {
            const step = GUIDED_DIRECTIONS[i];
            const required = GUIDED_REQUIRED.includes(step.key);
            setPromptStep(step);

            let got = null;
            if (required) {
                // Keep the recording rolling and keep asking for exactly this
                // turn - see the comment on GUIDED_CAPTURE_MAX_MS. Moving on
                // and finding out only after the whole clip was thrown away
                // is a worse experience than staying here until it happens.
                const failFlag = { fatal: false };
                while (!state.closed && !got && !failFlag.fatal && Date.now() < requiredDeadline) {
                    got = await waitFor(step.key, failFlag);
                    if (!got && !failFlag.fatal && !state.closed) {
                        // A held pause, not just a live-text update - the poll
                        // that resumes immediately after would otherwise stamp
                        // its own per-frame message ("turn further", "hold
                        // still") over this within one 200ms tick, and the
                        // reminder that a whole timeout just passed with no
                        // turn detected would never actually be seen.
                        setPromptLive('Still waiting - please follow the arrow');
                        if (navigator.vibrate) navigator.vibrate([15, 30, 15, 30, 15]);
                        await new Promise(res => setTimeout(res, 600));
                    }
                }
            } else {
                got = await waitFor(step.key, null);
            }

            // Still advances either way - an optional step's single timeout,
            // or a required step's deadline/fatal give-up (see above) - but
            // the two outcomes no longer look the same to the person doing
            // it, and a required give-up here is the rare backstop case, not
            // the normal path.
            if (got) {
                measured.push(step.key);
                await snap(control, step.key);
                await flashStepDone('Got it');
            } else if (!state.closed) {
                setPromptLive('Did not see that turn - carrying on');
                await new Promise(res => setTimeout(res, 350));
            }
            setRing(0.2 + 0.8 * (i + 1) / GUIDED_DIRECTIONS.length);
        }
        control.measured = measured;

        // Close on a held straight-ahead look. It puts frontal frames at the end
        // of the clip for the enrolment photo and templates, and it makes the
        // capture visibly deliberate - a few seconds of turns alone read as if
        // no face had been recorded at all.
        if (!state.closed && measured.length) {
            setPromptStep({ text: 'Now look straight at the camera', arrow: null });
            if (useLocal) {
                const start = Date.now();
                let since = 0;
                while (!state.closed && Date.now() - start < 4000) {
                    const p = livePose();
                    if (p && Math.abs(p.yaw - baseYaw) < LOCAL_CENTRE_MAX_YAW / 2) {
                        if (!since) since = Date.now();
                        if (Date.now() - since >= 800) break;
                        setPromptLive('Hold it…');
                    } else {
                        since = 0;
                        setPromptLive('Look straight at the camera');
                    }
                    await sleep(40);
                }
            } else {
                await sleep(1200);
            }
            await snap(control, 'straight');
            await flashStepDone('Face recorded');
        }
        setRing(1);

        const elapsed = Date.now() - t0;
        if (!state.closed && elapsed < MIN_TOTAL_MS) {
            await new Promise(res => setTimeout(res, MIN_TOTAL_MS - elapsed));
        }
        control.done = true;
    }

    // Called by the framing loop the moment the face is framed - on the first
    // attempt and on every retry. There is no button for it.
    async function startRecording() {
        if (state.recording || state.flipping || state.closed) return;
        state.recording = true;
        shutter.disabled = true;
        shutter.classList.add('recording');
        setFlipVisible(false);
        showAnalyzing(false);
        ui.status(opts.guided === false
            ? 'Recording - look at the camera and blink.'
            : 'Recording - follow the on-screen prompts.');

        // The pre-recording framing poll and the guided sequence's own poll
        // would otherwise both be hitting pose-check for the same video at
        // once. One voice at a time.
        if (state.timer) { clearInterval(state.timer); state.timer = null; }
        hint.classList.add('hidden');
        promptBox.classList.remove('hidden');

        // Both recordClip and runGuidedSequence already catch every failure
        // mode I could identify (a dropped poll, a null blob, a recorder that
        // refuses to start) and degrade to a retry rather than throwing. This
        // outer catch is the backstop for whatever that reasoning missed -
        // without it, an unanticipated exception here left the shutter
        // disabled, the prompt panel stuck visible, and no way back to the
        // camera except closing and reopening the whole modal.
        let file = null;
        let snapshots = [];
        let blinkAtMs = null;
        // What the retry path below tells the person, when it fires. Default
        // covers the outer catch and a genuinely empty capture; the guided
        // branch overwrites it with something specific when IT is the reason.
        let retryReason = 'Recording did not complete. Try again.';
        try {
            if (opts.guided === false) {
                // One face, scanned or verified against one record. Neither
                // wants "turn left": a 1:1 check needs one view. Proof of a
                // live face is a blink - see watchBlink.
                promptBox.classList.add('hidden');
                ui.status('Recording - look at the camera and blink.');
                if (meshFresh()) {
                    // Watched on the phone: ends a moment after a blink,
                    // instead of after a fixed three seconds.
                    const control = { done: false };
                    const [recorded, blinked] = await Promise.all([
                        cam.recordClip(CLIP_MS_MAX + 1000, null, control)
                            .then(r => { control.done = true; return r; }),
                        watchBlink(control),
                    ]);
                    if (recorded && (blinked.ok || opts.uploadWithoutBlink)) {
                        file = recorded;
                        blinkAtMs = blinked.ok ? blinked.blinkAtMs : null;
                    } else if (recorded && !state.closed) {
                        // Not sent - the server would only say "no blink" after
                        // the upload. The retry below records again by itself.
                        retryReason = blinked.reason;
                        speak('No blink seen. Try again.');
                    }
                } else {
                    // No on-device mesh (old phone, blocked download): record
                    // for a fixed time and let the server look for the blink.
                    speak('Look at the camera and blink');
                    file = await cam.recordClip(opts.clipMs || CLIP_MS_PLAIN + 1000, setRing);
                }
            } else {
                const control = { done: false, measured: [], snapshots: [] };
                const [recorded] = await Promise.all([
                    cam.recordClip(GUIDED_CAPTURE_MAX_MS, null, control),
                    runGuidedSequence(control),
                ]);
                const measured = control.measured || [];
                snapshots = control.snapshots || [];
                const seen = measured.length;
                const missing = GUIDED_REQUIRED.filter(k => !measured.includes(k));
                if (missing.length) {
                    // NOT UPLOADED - see GUIDED_REQUIRED. Nothing is sent and
                    // no account is touched; the person records again.
                    file = null;
                    retryReason = `Your head was not seen turning `
                        // Camera-image keys, the person's words - see GUIDED_DIRECTIONS.
                        + missing.map(k => ({ left: 'RIGHT', right: 'LEFT' }[k] || k.toUpperCase())).join(' or ')
                        + ' - record again and follow each prompt.';
                    // The status line sits under a camera the person is
                    // watching, not their eyes - a toast is what is actually
                    // seen. This is the one message in this whole flow that
                    // has to land, since it is the difference between "redo
                    // it" actually happening and a thin capture going through
                    // unnoticed exactly as it always used to.
                    showToast(seen ? 'Movement not confirmed' : 'No movement seen',
                              retryReason, 'error');
                } else {
                    file = recorded;
                    // Honest about what the clip actually contains, for the
                    // case that DOES upload: seen enough to proceed, but not
                    // all four. The person is the only one who can decide
                    // whether that is worth redoing before walking away.
                    if (seen < GUIDED_DIRECTIONS.length) {
                        showToast('Some turns were not seen',
                                  `${seen} of ${GUIDED_DIRECTIONS.length} measured. `
                                  + 'The clip was still sent - if it is refused, try '
                                  + 'again in better light and turn a little further.',
                                  'info');
                    }
                }
            }
        } catch (err) {
            console.error('Guided capture failed:', err);
            file = null;
        }

        promptBox.classList.add('hidden');
        hint.classList.remove('hidden');
        shutter.classList.remove('recording');
        setRing(0);
        state.recording = false;
        setFlipVisible(true);
        if (!file) {
            // retryReason distinguishes three situations that used to share
            // one message: a legitimate empty capture (recordClip already
            // toasts its own specific reason, e.g. no MediaRecorder support),
            // the outer catch above firing on something unexpected, and now
            // also too few of the guided prompts having been followed - see
            // GUIDED_MIN_MEASURED.
            //
            // NOT shutter.disabled = false here - same reasoning as
            // ui.resume(). Enabling it before the framing loop has even
            // restarted let a retry be tapped before a single frame of the
            // new attempt was checked. state.good/goodStreak reset too, so a
            // stale "good" from before the failed recording cannot leak into
            // arming the shutter for the retry on its own.
            state.good = false;
            state.goodStreak = 0; state.goodHist = [];
            shutter.disabled = true;
            ui.status(retryReason);
            state.autoFired = false;            // record again by itself once framed
            showAnalyzing(true);
            // The framing loop was stopped to give the guided sequence sole
            // use of pose-check; restore it so the shutter re-enables/
            // disables correctly for the retry instead of staying stuck at
            // whatever state.good last was.
            if (!state.closed && !state.timer) {
                framePollMs = FAST_POLL_MS;
                state.timer = setInterval(tick, framePollMs);
            }
            return;
        }

        try { cam.stop(); } catch { /* already stopped */ }
        // CANCEL HAS TO MEAN CANCEL. Closing the dialog mid-recording set
        // state.closed and stopped the camera, but the recording promise then
        // resolved anyway and this line ran regardless - so dismissing the
        // capture went on to submit the register or mark attendance, which is
        // the opposite of what the person just asked for. state.closed is
        // one-way, so testing it here is the whole fix.
        if (state.closed) return;
        // The server check takes a few seconds: say so in the middle of the
        // frozen camera, not with a stale "recording" pill left on it.
        hint.classList.add('hidden');
        showProcessing(true);
        await opts.onClip(file, ui, { snapshots, blinkAtMs });
        showProcessing(false);
    }
}

/** Attach one more photograph to an existing person.
 *
 * The "Add photo" button on every card in the directory called this by name and
 * nothing defined it, so every click threw a ReferenceError and the button did
 * nothing at all - silently, because the delegated handler swallows it. The
 * endpoint it needs has existed and been centre-guarded the whole time.
 *
 * A file picker rather than the camera: the button's own tooltip offers "recent
 * selfie or ID", which is a photograph the person already has. Liveness is not
 * asked for here because this adds a gallery template for RECOGNITION, and the
 * clip capture next to it remains the way to prove a real person is present.
 */
function openAddPhotoModal(studentId, studentName) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        showToast('Uploading', `Adding a photo for ${studentName || 'this athlete'}…`, 'info');
        try {
            const fd = new FormData();
            fd.append('photo', file);
            fd.append('source', 'upload');
            const r = await api.postForm(`/api/students/${studentId}/photos`, fd);
            if (r && r.ok === false) {
                showToast('Not accepted', r.message || 'That photo was refused', 'error');
                return;
            }
            showToast('Photo added',
                      `${(r && r.templates_added) || 1} template(s) for ${studentName}`,
                      'success');
            renderStudents();
        } catch (err) {
            showToast('Upload failed', (err && err.message) || 'Could not reach the server',
                      'error');
        }
    });
    input.click();
}

/** Re-register an existing person from a clip. */
async function openClipEnrol(studentId, studentName) {
    await openClipCapture({
        title: `Record clip - ${studentName}`,
        intro: "Look at the camera and keep turning your head slowly - left, right, "
             + "up and down - for the whole recording. The movement is what proves "
             + "a real person is present.",
        onClip: async (file, ui, extra) => {
            ui.status('Checking the clip and building templates...');
            if (file.size > CLIP_UPLOAD_MAX_BYTES) {
                ui.status('That recording was too long to upload. Record again - it only needs a few seconds.');
                await ui.resume();
                return;
            }
            const fd = new FormData();
            fd.append('video', file);
            appendStepPhotos(fd, extra);
            try {
                const r = await api.postForm(`/api/students/${studentId}/enroll-video`, fd);
                if (r.ok === false && r.duplicate) {
                    ui.close();
                    showToast('Not accepted', r.message, 'error');
                    return;
                }
                if (r.ok === false) {
                    // Same as enrolSubmit: a pose refusal is not a liveness one.
                    if (r.pose_check && r.pose_check.ok === false) ui.status(r.message);
                    else ui.status(livenessBanner(r.liveness, r.message), true);
                    showToast('Not accepted', r.message || 'The clip was refused', 'error');
                    await ui.resume();
                    return;
                }
                const poses = (r.poses_captured || []).join(', ') || 'one view';
                // r.sufficient/r.message come straight from enroll_multiview,
                // which already knows whether the turning happened - it was
                // just never read here. "from one view" used to sit inside an
                // unqualified success toast, indistinguishable from a real
                // multi-angle capture unless somebody happened to read the
                // word "one".
                if (r.sufficient === false) {
                    showToast('Registered - but check the recording',
                              `${r.templates_added} template(s) from ${poses}. `
                              + (r.message || 'Only one view of the face was captured.'),
                              'info');
                } else {
                    showToast('Face registered', `${r.templates_added} template(s) from ${poses}`, 'success');
                }
                ui.close();
                renderStudents();
            } catch {
                ui.status('Could not reach the server. Try again.');
                await ui.resume();
            }
        },
    });
}

