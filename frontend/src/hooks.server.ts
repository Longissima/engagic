import type { Handle, HandleServerError } from '@sveltejs/kit';
import { COOKIE_NAME, verifyCookie } from '$lib/server/challenge-cookie';

export const handleError: HandleServerError = async ({ error, event }) => {
	console.error('SSR error:', event.url.pathname, error);
	return {
		message: 'Internal Error'
	};
};

// Paths that do NOT require the SSR Turnstile gate.
// Everything else is gated -- content routes are protected by default.
const UNGATED_PATTERNS: RegExp[] = [
	/^\/$/,
	/^\/about(\/|$)/,
	/^\/login(\/|$)/,
	/^\/signup(\/|$)/,
	/^\/auth(\/|$)/,
	/^\/challenge(\/|$)/,
	/^\/tiles(\/|$)/,
	/^\/dashboard(\/|$)/,
	/^\/funnel(\/|$)/,
	/^\/sitemap\.xml$/,
	/^\/robots\.txt$/,
	/^\/favicon/,
	/^\/_app\//
];

function requiresGate(pathname: string): boolean {
	return !UNGATED_PATTERNS.some((p) => p.test(pathname));
}

export const handle: Handle = async ({ event, resolve }) => {
	event.locals.clientIp = event.request.headers.get('cf-connecting-ip');
	event.locals.ssrAuthSecret = event.platform?.env?.SSR_AUTH_SECRET ?? null;

	// SSR Turnstile gate: require signed cookie before serving content pages.
	// Only guards GET navigations -- POST/PUT etc. fall through to their handlers.
	if (event.request.method === 'GET' && requiresGate(event.url.pathname)) {
		const cookieSecret = event.platform?.env?.CHALLENGE_COOKIE_SECRET;
		if (cookieSecret) {
			const cookie = event.cookies.get(COOKIE_NAME);
			const valid = await verifyCookie(cookieSecret, cookie, event.locals.clientIp);
			if (!valid) {
				const returnTo = event.url.pathname + event.url.search;
				return new Response(null, {
					status: 303,
					headers: {
						Location: `/challenge?return=${encodeURIComponent(returnTo)}`
					}
				});
			}
		}
		// If CHALLENGE_COOKIE_SECRET is not set, fail open during rollout.
		// Flip to fail-closed once the env var is confirmed in CF Pages.
	}

	return resolve(event, {
		preload: ({ type, path }) => {
			if (type === 'font') return path.includes('ibm-plex-mono');
			return type === 'js' || type === 'css';
		}
	});
};
