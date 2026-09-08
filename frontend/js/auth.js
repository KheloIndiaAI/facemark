/* Login gate and role-aware chrome.
 *
 * The server is the actual authority - every protected endpoint checks the
 * session cookie. This file only decides what to *show*, so hiding a nav item
 * is a convenience, never the security boundary.
 */

const session = { user: null };

function isSuperAdmin() { return session.user && session.user.role === 'super_admin'; }
function isCoach() { return session.user && session.user.role === 'coach'; }
function isAthlete() { return session.user && session.user.role === 'athlete'; }

// Three roles now, so "not super admin" no longer means coach. Every
// label below went through that assumption and would have called an
// athlete a Coach.
function roleLabel(role, centreName) {
    if (role === 'super_admin') return 'Super Admin';
    if (role === 'athlete') return 'Athlete';
    return 'Coach - ' + (centreName || 'unassigned');
}
function roleShort(role) {
    return role === 'super_admin' ? 'Super Admin'
         : role === 'athlete' ? 'Athlete' : 'Coach';
}

async function checkSession() {
    try {
        const res = await fetch('/api/auth/me');
        if (!res.ok) return null;
        const data = await res.json();
        session.user = data.user;
        return data.user;
    } catch { return null; }
}

function showLogin(message) {
    document.getElementById('app-shell').classList.add('hidden');
    const gate = document.getElementById('login-gate');
    gate.classList.remove('hidden');
    const err = document.getElementById('login-error');
    if (message) { err.textContent = message; err.classList.remove('hidden'); }
    else { err.classList.add('hidden'); }
    setTimeout(() => document.getElementById('login-username')?.focus(), 60);
}

function hideLogin() {
    document.getElementById('login-gate').classList.add('hidden');
    document.getElementById('app-shell').classList.remove('hidden');
}

async function doLogin(ev) {
    if (ev) ev.preventDefault();
    const btn = document.getElementById('login-submit');
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    if (!username || !password) return showLogin('Enter a username and password');

    btn.disabled = true;
    btn.textContent = 'Signing in...';
    try {
        const fd = new FormData();
        fd.append('username', username);
        fd.append('password', password);
        const res = await fetch('/api/auth/login', { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok) {
            showLogin(data.detail || 'Sign in failed');
            return;
        }
        session.user = data.user;
        document.getElementById('login-password').value = '';
        hideLogin();
        applyRoleChrome();
        await initApp();
        // Signing in lands on the job that role opens the app to do: Mark
        // Attendance for a coach or admin, their own page for an athlete.
        // Set here as well as in the router - this assignment overrides
        // whatever default handleRoute() would have picked, so changing one
        // without the other silently keeps the old landing page. That is
        // exactly what happened when the router learned about athletes and
        // this line did not.
        window.location.hash = isAthlete() ? '#/me' : '#/mark';
        handleRoute();
        showToast('Welcome', `Signed in as ${data.user.full_name}`, 'success');
    } catch {
        showLogin('Could not reach the server');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign in';
    }
}

async function doLogout() {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* sign out locally anyway */ }
    session.user = null;
    showLogin();
}

/* Show/hide nav by role and stamp the identity chip. */
function applyRoleChrome() {
    const u = session.user;
    if (!u) return;

    document.querySelectorAll('[data-role-only]').forEach(el => {
        const allowed = el.getAttribute('data-role-only').split(',').map(s => s.trim());
        el.classList.toggle('hidden', !allowed.includes(u.role));
    });

    const chip = document.getElementById('user-chip');
    if (chip) {
        chip.innerHTML = `
            <div class="user-chip-avatar">${Charts.esc(u.full_name.charAt(0).toUpperCase())}</div>
            <div class="user-chip-text">
                <div class="user-chip-name">${Charts.esc(u.full_name)}</div>
                <div class="user-chip-role">${Charts.esc(
                    roleLabel(u.role, u.centre_name))}</div>
            </div>`;
    }
    const badge = document.getElementById('role-badge');
    if (badge) {
        badge.textContent = roleShort(u.role);
        badge.className = 'badge ' + (u.role === 'super_admin' ? 'badge-blue'
                                    : u.role === 'athlete' ? 'badge-amber' : 'badge-green');
    }
}

/* Any 401 from anywhere drops straight back to the login gate. */
function handleUnauthorized() {
    session.user = null;
    showLogin('Your session expired. Sign in again.');
}

async function openPasswordModal() {
    openModal('Change password', `
        <div class="form-group">
            <label class="form-label">Current password</label>
            <input type="password" id="pw-current" class="form-input" autocomplete="current-password">
        </div>
        <div class="form-group">
            <label class="form-label">New password (min 6 characters)</label>
            <input type="password" id="pw-new" class="form-input" autocomplete="new-password">
        </div>
        <div class="form-group">
            <label class="form-label">Confirm new password</label>
            <input type="password" id="pw-confirm" class="form-input" autocomplete="new-password">
        </div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitPasswordChange()">Update password</button>`);
}

async function submitPasswordChange() {
    const cur = document.getElementById('pw-current').value;
    const nw = document.getElementById('pw-new').value;
    const cf = document.getElementById('pw-confirm').value;
    if (nw !== cf) return showToast('Error', 'The new passwords do not match', 'error');
    if (nw.length < 6) return showToast('Error', 'Use at least 6 characters', 'error');
    const fd = new FormData();
    fd.append('current_password', cur);
    fd.append('new_password', nw);
    try {
        await api.postForm('/api/auth/password', fd);
        closeModal();
        showToast('Password changed', 'Sign in again with your new password', 'success');
        setTimeout(doLogout, 1200);
    } catch { /* api layer already surfaced the error */ }
}


/* ---------------------------------------------------------------------------
   Self-signup

   Four steps, each gated by the token the first returns. The account that
   comes out cannot sign in and its face is not recognised until a coach
   approves it - the copy says so at every step, because someone who thinks
   they are already enrolled will turn up and be marked absent.
--------------------------------------------------------------------------- */

const suState = { token: null, centre: null, role: 'athlete' };

// One panel, two applications. The steps are the same except for the coach
// picker, which only an athlete has - a coach is approved by a super admin, so
// there is nobody for them to choose.
const SU_COPY = {
    athlete: {
        title: 'Create your account',
        sub: 'Your coach approves it before it works.',
        note: 'Registering as an <strong>athlete</strong>. You will pick your coach '
            + 'next, and they approve you before you can be marked present.',
        doneTitle: 'Sent to your coach.',
        doneBody: 'You can sign in once they approve you. Until then you will not '
            + 'be recognised in a capture, so keep signing the register the usual way.',
    },
    coach: {
        title: 'Register as a coach',
        sub: 'A super admin approves coach accounts.',
        note: 'Registering as a <strong>coach</strong>. A coach account can see a '
            + 'whole centre, so a super admin checks it - not another coach. '
            + 'This is not instant.',
        doneTitle: 'Sent to a super admin.',
        doneBody: 'Coach access is approved centrally, so this is not instant. '
            + 'You will be able to sign in once it is approved.',
    },
};

function suMsg(text, bad = true) {
    const el = document.getElementById('su-msg');
    if (el) { el.textContent = text || ''; el.style.color = bad ? 'var(--red)' : 'var(--text-secondary)'; }
}

function suShow(step) {
    ['su-step-1', 'su-step-2', 'su-step-3', 'su-step-4', 'su-done']
        .forEach((id, i) => {
            const el = document.getElementById(id);
            if (el) el.classList.toggle('hidden', i !== step);
        });
}

async function openSignup(role = 'athlete') {
    suState.role = (role === 'coach') ? 'coach' : 'athlete';
    suState.token = null;
    const copy = SU_COPY[suState.role];
    const set = (id, html) => { const e = document.getElementById(id); if (e) e.innerHTML = html; };
    set('su-title', copy.title);
    set('su-sub', copy.sub);
    set('su-role-note', copy.note);
    set('su-done-title', copy.doneTitle);
    set('su-done-body', copy.doneBody);
    // Coaches only. An athlete's coach approves them in person; a coach may be
    // approved by a super admin who has never met them, so the application has
    // to carry something the centre actually issued.
    document.getElementById('su-code-group')
        ?.classList.toggle('hidden', suState.role !== 'coach');
    document.getElementById('login-gate')?.classList.add('hidden');
    document.getElementById('signup-gate')?.classList.remove('hidden');
    suShow(0); suMsg('');
    // Centres are needed before an account exists, so this one list is public.
    // It carries no personal data - names and codes of government centres.
    try {
        const r = await fetch('/api/signup/centres');
        const j = await r.json();
        const sel = document.getElementById('su-centre');
        if (sel) sel.innerHTML = (j.centres || [])
            .map(c => `<option value="${c.id}">${Charts.esc(c.name)}</option>`).join('');
    } catch { suMsg('Could not load centres. Try again later.'); }
}

function closeSignup() {
    document.getElementById('signup-gate')?.classList.add('hidden');
    document.getElementById('login-gate')?.classList.remove('hidden');
}

async function suStart() {
    const fd = new FormData();
    fd.append('full_name', document.getElementById('su-name').value.trim());
    fd.append('username', document.getElementById('su-user').value.trim());
    fd.append('password', document.getElementById('su-pw').value);
    fd.append('phone', document.getElementById('su-phone').value.trim());
    suState.centre = document.getElementById('su-centre').value;
    fd.append('centre_id', suState.centre);
    fd.append('role', suState.role);
    if (suState.role === 'coach') {
        fd.append('join_code', document.getElementById('su-joincode').value.trim());
    }
    suMsg('');
    try {
        const res = await fetch('/api/signup', { method: 'POST', body: fd });
        const j = await res.json();
        if (!res.ok) return suMsg(j.detail || 'Could not create the account');
        suState.token = j.token;
        // The SERVER says whether a coach has to be chosen, rather than the
        // browser inferring it from the role it just sent. The two can only
        // disagree if the server rejected or altered the role, and in that case
        // the server is right.
        if (j.needs_coach === false) return suSendCode();
        await suLoadCoaches();
        suShow(1);
    } catch { suMsg('Could not reach the server'); }
}

async function suLoadCoaches() {
    const host = document.getElementById('su-coaches');
    if (!host) return;
    const r = await fetch(`/api/signup/coaches?token=${encodeURIComponent(suState.token)}`
                          + `&centre_id=${encodeURIComponent(suState.centre)}`);
    const j = await r.json();
    const list = j.coaches || [];
    if (!list.length) {
        host.innerHTML = '<div class="empty-state">No coaches at that centre yet.</div>';
        return;
    }
    host.innerHTML = list.map(c => `
        <button type="button" class="btn btn-secondary" data-su-coach="${c.id}"
                style="display:flex;align-items:center;gap:10px;width:100%;height:auto;padding:8px;margin-bottom:8px;justify-content:flex-start">
            ${c.photo ? `<img src="${c.photo}" alt="" style="width:36px;height:36px;border-radius:8px;object-fit:cover">`
                      : '<div style="width:36px;height:36px;border-radius:8px;background:var(--bg-subtle)"></div>'}
            <span>${Charts.esc(c.name)}</span>
        </button>`).join('');
    host.querySelectorAll('[data-su-coach]').forEach(b => {
        b.addEventListener('click', () => suPickCoach(b.dataset.suCoach));
    });
}

async function suPickCoach(coachId) {
    const fd = new FormData();
    fd.append('token', suState.token);
    fd.append('coach_id', coachId);
    const r = await fetch('/api/signup/coach', { method: 'POST', body: fd });
    if (!r.ok) return suMsg('Could not select that coach');
    await suSendCode();
}

async function suSendCode() {
    const fd = new FormData();
    fd.append('token', suState.token);
    const r = await fetch('/api/signup/otp/send', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) return suMsg(j.detail || 'Could not send a code');
    suShow(2);
    // The server says whether a message actually went anywhere. Telling
    // somebody to check their phone when no SMS provider is configured leaves
    // them waiting for a message that is never coming, and blaming their signal.
    const note = document.getElementById('su-otp-note');
    if (note) {
        note.textContent = j.sent
            ? 'We sent a 6-digit code to your phone.'
            : 'Text messages are not switched on yet at this centre. Ask your '
              + 'coach or administrator for the code from the system.';
    }
    suMsg('');
}

async function suVerify() {
    const fd = new FormData();
    fd.append('token', suState.token);
    fd.append('code', document.getElementById('su-code').value.trim());
    const r = await fetch('/api/signup/otp/verify', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) return suMsg(j.detail || 'That code is not right');
    suShow(3); suMsg('');
}

function suFace() {
    openClipCapture({
        title: 'Record your face',
        intro: 'Follow the prompts and turn your head as asked.',
        onClip: async (file, ui) => {
            ui.status('Checking\u2026');
            try {
                const fd = new FormData();
                fd.append('token', suState.token);
                fd.append('video', file);
                const res = await fetch('/api/signup/face', { method: 'POST', body: fd });
                const j = await res.json();
                if (!res.ok || j.ok === false) {
                    ui.status(j.message || j.detail || 'Could not use that clip');
                    await ui.resume();
                    return;
                }
                ui.close();
                suShow(4);
            } catch {
                ui.status('Could not reach the server');
                await ui.resume();
            }
        },
    });
}

document.addEventListener('DOMContentLoaded', () => {
    const on = (id, fn) => document.getElementById(id)?.addEventListener('click', (e) => {
        e.preventDefault(); fn();
    });
    on('signup-open', () => openSignup('athlete'));
    on('signup-open-coach', () => openSignup('coach'));
    on('signup-cancel', closeSignup);
    on('su-next-1', suStart);
    on('su-verify', suVerify);
    on('su-resend', suSendCode);
    on('su-face', suFace);
});


/* ---------------------------------------------------------------------------
   Password reset

   The account already carries a phone number that was verified at signup, and
   there is already a one-time-code mechanism next to it. Before this, a
   forgotten password meant finding somebody with admin access.
--------------------------------------------------------------------------- */

function rsMsg(text, bad = true) {
    const el = document.getElementById('rs-msg');
    if (el) {
        el.textContent = text || '';
        el.style.color = bad ? 'var(--red)' : 'var(--text-secondary)';
    }
}

function rsShow(step) {
    ['rs-step-1', 'rs-step-2', 'rs-ok'].forEach((id, i) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', i !== step);
    });
}

function openReset() {
    document.getElementById('login-gate')?.classList.add('hidden');
    document.getElementById('reset-gate')?.classList.remove('hidden');
    rsShow(0); rsMsg('');
}

function closeReset() {
    document.getElementById('reset-gate')?.classList.add('hidden');
    document.getElementById('login-gate')?.classList.remove('hidden');
}

async function rsSend() {
    const u = document.getElementById('rs-user').value.trim();
    if (!u) return rsMsg('Enter your username');
    const fd = new FormData();
    fd.append('username', u);
    try {
        const r = await fetch('/api/auth/reset/start', { method: 'POST', body: fd });
        const j = await r.json();
        if (!r.ok) return rsMsg(j.detail || 'Could not send a code');
        rsShow(1);
        // Deliberately the same words whether or not that username exists -
        // the server answers identically, and so must this.
        rsMsg(j.sent
            ? 'If that account exists, a code is on its way to the phone on it.'
            : 'Text messages are not switched on yet. Ask your coach or '
              + 'administrator for the code from the system.', false);
    } catch { rsMsg('Could not reach the server'); }
}

async function rsComplete() {
    const fd = new FormData();
    fd.append('username', document.getElementById('rs-user').value.trim());
    fd.append('code', document.getElementById('rs-code').value.trim());
    const pw = document.getElementById('rs-pw').value;
    if (pw.length < 6) return rsMsg('Use at least 6 characters');
    fd.append('new_password', pw);
    try {
        const r = await fetch('/api/auth/reset/complete', { method: 'POST', body: fd });
        const j = await r.json();
        if (!r.ok) return rsMsg(j.detail || 'That did not work');
        rsShow(2); rsMsg('');
    } catch { rsMsg('Could not reach the server'); }
}

document.addEventListener('DOMContentLoaded', () => {
    const on = (id, fn) => document.getElementById(id)?.addEventListener('click', (e) => {
        e.preventDefault(); fn();
    });
    on('reset-open', openReset);
    on('reset-cancel', closeReset);
    on('rs-send', rsSend);
    on('rs-done', rsComplete);
});
