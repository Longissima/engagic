/**
 * Turnstile bot verification manager.
 *
 * Three-stage behaviour, matching Cloudflare's managed widget:
 *   1. Cached session (localStorage, shared across tabs) -> no challenge at all.
 *   2. No session -> run the widget invisibly; most browsers pass silently.
 *   3. Cloudflare demands interaction -> the widget is lifted into a centered
 *      modal overlay for the click, then hidden again.
 *
 * Every solve counts against Cloudflare's per-visitor trust, so the widget is
 * rendered lazily and only when a token is actually needed. Rendering it on
 * every tab open (the previous behaviour) is what escalated visitors into
 * interactive challenges in the first place.
 *
 * Session tokens are issued by POST /challenge (single siteverify round-trip
 * that also sets the SSR gate cookie), last 30 minutes server-side, and are
 * bound to the client IP. On 403 from the API the manager re-verifies.
 *
 * Concurrency contract:
 *   - Each Turnstile callback token is single-use at siteverify. Waiters form
 *     a FIFO queue and each arriving token goes to exactly one consumer.
 *   - acquireSession() is serialized via an inflight promise so concurrent
 *     403s share one widget run rather than racing on reset().
 *   - Any token parked before reset() is stale and discarded.
 */

import { setExtraHeaders, getExtraHeaders } from './api/api-client';

declare global {
	interface Window {
		turnstile?: {
			render: (container: string | HTMLElement, options: Record<string, unknown>) => string;
			reset: (widgetId: string) => void;
			remove: (widgetId: string) => void;
			ready: (callback: () => void) => void;
		};
	}
}

export const SITE_KEY = '0x4AAAAAAC8k9WNTYMFPIDOj';
export const SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

const STORAGE_KEY = 'engagic.turnstile.session';
// Server TTL is 30 min; reuse only while comfortably inside it.
const SESSION_REUSE_SECONDS = 25 * 60;

let widgetId: string | null = null;
let parkedToken: string | null = null;
let sessionToken: string | null = null;
let scriptLoaded = false;
let initialized = false;

const tokenWaiters: Array<(token: string) => void> = [];
let acquireInflight: Promise<boolean> | null = null;

let initReadyResolve: (() => void) | null = null;
const initReady: Promise<void> = new Promise((r) => {
	initReadyResolve = r;
});

// --- Session persistence -------------------------------------------------

// Token format is `${unixSeconds}:${sig}`; age is derivable without a
// separate stored timestamp.
function sessionAge(token: string): number {
	const issued = Number.parseInt(token.split(':', 1)[0], 10);
	if (!Number.isFinite(issued)) return Number.POSITIVE_INFINITY;
	return Math.floor(Date.now() / 1000) - issued;
}

function readStoredSession(): string | null {
	try {
		const token = window.localStorage.getItem(STORAGE_KEY);
		if (!token) return null;
		if (sessionAge(token) > SESSION_REUSE_SECONDS) {
			window.localStorage.removeItem(STORAGE_KEY);
			return null;
		}
		return token;
	} catch {
		return null;
	}
}

/** Persist a session token for other tabs and the post-/challenge redirect. */
export function storeSession(token: string): void {
	try {
		window.localStorage.setItem(STORAGE_KEY, token);
	} catch {
		// Storage unavailable (private mode, blocked) -- session stays in memory.
	}
}

function clearStoredSession(): void {
	try {
		window.localStorage.removeItem(STORAGE_KEY);
	} catch {
		// ignore
	}
}

function applySession(token: string): void {
	sessionToken = token;
	setExtraHeaders({ ...getExtraHeaders(), 'X-Turnstile-Token': token });
	storeSession(token);
}

// --- Script + widget -----------------------------------------------------

export function loadScript(): Promise<void> {
	if (scriptLoaded || window.turnstile) {
		scriptLoaded = true;
		return Promise.resolve();
	}
	return new Promise((resolve, reject) => {
		const script = document.createElement('script');
		script.src = SCRIPT_SRC;
		script.async = true;
		script.onload = () => {
			scriptLoaded = true;
			resolve();
		};
		script.onerror = () => reject(new Error('turnstile script load failed'));
		document.head.appendChild(script);
	});
}

function handOutToken(token: string): void {
	const next = tokenWaiters.shift();
	if (next) {
		next(token);
	} else {
		parkedToken = token;
	}
}

function failAllWaiters(): void {
	parkedToken = null;
	while (tokenWaiters.length) {
		tokenWaiters.shift()?.('');
	}
}

// The overlay is the container itself: hidden (off-layout, not display:none,
// because Turnstile refuses to run inside a display:none subtree) until
// Cloudflare signals that interaction is required.
const OVERLAY_ID = 'turnstile-overlay';
const WIDGET_ID = 'turnstile-widget';

function ensureOverlay(): HTMLElement {
	let overlay = document.getElementById(OVERLAY_ID);
	if (overlay) return overlay;

	overlay = document.createElement('div');
	overlay.id = OVERLAY_ID;
	overlay.setAttribute('role', 'dialog');
	overlay.setAttribute('aria-label', 'Human verification');
	overlay.innerHTML = `
		<div class="turnstile-card">
			<p class="turnstile-message">Quick check to keep bots off engagic.</p>
			<div id="${WIDGET_ID}"></div>
		</div>`;

	const style = document.createElement('style');
	style.textContent = `
		#${OVERLAY_ID} {
			position: fixed; inset: 0; z-index: 2147483000;
			display: flex; align-items: center; justify-content: center;
			background: rgba(0, 0, 0, 0.45);
			opacity: 0; pointer-events: none;
			transition: opacity 120ms ease;
		}
		#${OVERLAY_ID}.visible { opacity: 1; pointer-events: auto; }
		#${OVERLAY_ID} .turnstile-card {
			background: var(--bg-primary, #fff);
			color: var(--text-primary, #222);
			border-radius: 8px;
			padding: 1.25rem 1.5rem;
			box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
			max-width: 92vw;
			text-align: center;
		}
		#${OVERLAY_ID} .turnstile-message {
			font-family: var(--font-mono, monospace);
			font-size: 0.85rem;
			color: var(--text-secondary, #666);
			margin: 0 0 0.75rem;
		}
		#${WIDGET_ID} { display: flex; justify-content: center; min-width: 300px; }`;
	document.head.appendChild(style);
	document.body.appendChild(overlay);
	return overlay;
}

function showOverlay(): void {
	ensureOverlay().classList.add('visible');
}

function hideOverlay(): void {
	document.getElementById(OVERLAY_ID)?.classList.remove('visible');
}

function renderWidget(): void {
	if (!window.turnstile) return;
	ensureOverlay();
	if (widgetId) window.turnstile.remove(widgetId);

	widgetId = window.turnstile.render(`#${WIDGET_ID}`, {
		sitekey: SITE_KEY,
		size: 'flexible',
		appearance: 'interaction-only',
		callback: (token: string) => {
			hideOverlay();
			handOutToken(token);
		},
		'before-interactive-callback': () => showOverlay(),
		'after-interactive-callback': () => hideOverlay(),
		'error-callback': () => {
			hideOverlay();
			failAllWaiters();
		},
		'expired-callback': () => {
			parkedToken = null;
		},
	});
}

function resetWidget(): void {
	if (!window.turnstile || !widgetId) return;
	parkedToken = null;
	window.turnstile.reset(widgetId);
}

function waitForToken(): Promise<string> {
	if (parkedToken) {
		const t = parkedToken;
		parkedToken = null;
		return Promise.resolve(t);
	}
	return new Promise((resolve) => {
		tokenWaiters.push(resolve);
	});
}

/**
 * Exchange a challenge token for a session. Single siteverify round-trip:
 * /challenge sets the SSR gate cookie AND returns the API session_token.
 * Cloudflare only accepts each token once, so the browser must never also
 * call /api/turnstile/verify with it.
 */
export async function exchangeForSession(turnstileToken: string): Promise<string | null> {
	try {
		const resp = await fetch('/challenge', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ token: turnstileToken }),
			credentials: 'same-origin',
		});
		if (!resp.ok) return null;
		const data = await resp.json();
		return data.session_token || null;
	} catch {
		return null;
	}
}

/** Run the widget (render or reset) and exchange its token for a session. */
function acquireSession(): Promise<boolean> {
	if (acquireInflight) return acquireInflight;
	acquireInflight = (async () => {
		try {
			await loadScript();
			if (widgetId) {
				resetWidget();
			} else {
				renderWidget();
			}
			const token = await waitForToken();
			if (!token) return false;
			const session = await exchangeForSession(token);
			if (!session) return false;
			applySession(session);
			return true;
		} catch {
			return false;
		} finally {
			acquireInflight = null;
		}
	})();
	return acquireInflight;
}

// --- Public API ----------------------------------------------------------

/**
 * Initialize on app startup. Reuses a stored session when one exists so a
 * new tab or a post-/challenge redirect costs no additional solve.
 * Idempotent.
 */
export async function initTurnstile(): Promise<void> {
	if (initialized || !SITE_KEY) return;
	if (typeof window === 'undefined') return;
	initialized = true;

	try {
		const stored = readStoredSession();
		if (stored) {
			applySession(stored);
			return;
		}
		await acquireSession();
	} finally {
		initReadyResolve?.();
		initReadyResolve = null;
	}
}

/** Resolves when initTurnstile has settled (success or give-up). */
export function waitForInit(): Promise<void> {
	return initReady;
}

/**
 * Re-verify after a 403 turnstile_required: drop the stale session and run
 * the widget again. Concurrent callers share one inflight run.
 */
export function reverify(): Promise<boolean> {
	clearStoredSession();
	sessionToken = null;
	return acquireSession();
}

export function hasSession(): boolean {
	return !!sessionToken;
}
