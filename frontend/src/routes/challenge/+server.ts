/**
 * POST /challenge
 *
 * Frontend challenge endpoint:
 *   1. Receives a Turnstile token from the browser
 *   2. Forwards it to the backend /api/turnstile/verify for siteverify
 *   3. On success, sets an HMAC-signed cookie that gates SSR content
 *      via hooks.server.ts
 *
 * Runs on the Cloudflare Pages worker. Requires env vars:
 *   CHALLENGE_COOKIE_SECRET    (any random 32+ char string)
 *   SSR_AUTH_SECRET            (must match the API service)
 *
 * Note: TURNSTILE_SECRET_KEY is NOT needed here -- siteverify happens
 * on the backend which already has it.
 */

import type { RequestHandler } from './$types';
import { COOKIE_NAME, COOKIE_TTL_SECONDS, signCookie } from '$lib/server/challenge-cookie';
import { config } from '$lib/api/config';

export const POST: RequestHandler = async ({ request, cookies, platform }) => {
	const cookieSecret = platform?.env?.CHALLENGE_COOKIE_SECRET;
	if (!cookieSecret) {
		return new Response(JSON.stringify({ error: 'not_configured' }), {
			status: 503,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	// This request is handled by a Cloudflare Worker, so the subsequent fetch
	// to api.engagic.org no longer carries the visitor's CF-Connecting-IP.
	// Cloudflare replaces it with its fixed cross-zone Worker address. Forward
	// the original value in our private header and authenticate it with the same
	// shared secret used by SSR API requests. The backend must ignore this
	// header unless X-SSR-Auth validates, otherwise clients could spoof their IP.
	const clientIp = request.headers.get('CF-Connecting-IP');
	const ssrAuthSecret = platform?.env?.SSR_AUTH_SECRET;
	if (!clientIp || !ssrAuthSecret) {
		return new Response(JSON.stringify({ error: 'ip_forwarding_not_configured' }), {
			status: 503,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	let body: unknown;
	try {
		body = await request.json();
	} catch {
		return new Response(JSON.stringify({ error: 'invalid_body' }), {
			status: 400,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	const token = typeof body === 'object' && body && 'token' in body ? (body as { token: unknown }).token : null;
	if (typeof token !== 'string' || !token || token.length > 2048) {
		return new Response(JSON.stringify({ error: 'invalid_token' }), {
			status: 400,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	// Proxy to backend for siteverify. Backend /api/turnstile/verify is already
	// exempt from its own Turnstile middleware and from rate limiting. SSR auth
	// here authenticates the forwarded visitor IP; it is not a Turnstile bypass.
	//
	// Cloudflare siteverify rejects any token used more than once
	// (error code: timeout-or-duplicate). Previously the browser also called
	// /api/turnstile/verify directly with the same token, which meant one of
	// the two calls always failed. Now /challenge is the single siteverify
	// path, and the session_token is returned to the browser for use as
	// X-Turnstile-Token on API requests.
	let verifySuccess = false;
	let sessionToken: string | null = null;
	try {
		const resp = await fetch(`${config.apiBaseUrl}/api/turnstile/verify`, {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'X-Forwarded-Client-IP': clientIp,
				'X-SSR-Auth': ssrAuthSecret
			},
			body: JSON.stringify({ token })
		});
		const data = (await resp.json()) as { success?: boolean; session_token?: string };
		verifySuccess = resp.ok && !!data.success;
		sessionToken = data.session_token ?? null;
	} catch {
		return new Response(JSON.stringify({ error: 'backend_unreachable' }), {
			status: 502,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	if (!verifySuccess || !sessionToken) {
		return new Response(JSON.stringify({ error: 'turnstile_failed' }), {
			status: 403,
			headers: { 'Content-Type': 'application/json' }
		});
	}

	const now = Math.floor(Date.now() / 1000);
	const value = await signCookie(cookieSecret, now, clientIp);
	cookies.set(COOKIE_NAME, value, {
		path: '/',
		httpOnly: true,
		secure: true,
		sameSite: 'lax',
		maxAge: COOKIE_TTL_SECONDS
	});

	return new Response(JSON.stringify({ success: true, session_token: sessionToken }), {
		headers: { 'Content-Type': 'application/json' }
	});
};
