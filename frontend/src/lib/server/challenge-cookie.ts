/**
 * HMAC-signed cookies for the SSR challenge gate.
 *
 * Uses Web Crypto (SubtleCrypto) so it runs in Cloudflare Workers.
 * Format: `{timestamp}:{sig}` where sig is HMAC-SHA256(secret, `{timestamp}|{ip}`)[0:32].
 * The visitor IP is mixed into the signature, not carried in the cookie, so
 * one solved challenge cannot be shared across a proxy farm for 7 days. An
 * IP change simply redirects to /challenge, which is invisible for humans.
 * Timestamps older than TTL are rejected.
 */

export const COOKIE_NAME = '_ts_human';
export const COOKIE_TTL_SECONDS = 60 * 60 * 24 * 7; // 7 days

async function hmacHex(secret: string, message: string): Promise<string> {
	const encoder = new TextEncoder();
	const key = await crypto.subtle.importKey(
		'raw',
		encoder.encode(secret),
		{ name: 'HMAC', hash: 'SHA-256' },
		false,
		['sign']
	);
	const sig = await crypto.subtle.sign('HMAC', key, encoder.encode(message));
	return Array.from(new Uint8Array(sig))
		.map((b) => b.toString(16).padStart(2, '0'))
		.join('');
}

export async function signCookie(secret: string, timestamp: number, ip: string): Promise<string> {
	const sig = (await hmacHex(secret, `${timestamp}|${ip}`)).slice(0, 32);
	return `${timestamp}:${sig}`;
}

export async function verifyCookie(
	secret: string,
	value: string | undefined,
	ip: string | null | undefined
): Promise<boolean> {
	if (!value || !ip) return false;
	const parts = value.split(':');
	if (parts.length !== 2) return false;
	const ts = parseInt(parts[0], 10);
	if (!Number.isFinite(ts)) return false;
	const nowSec = Math.floor(Date.now() / 1000);
	if (nowSec - ts > COOKIE_TTL_SECONDS) return false;
	if (ts > nowSec + 60) return false; // clock skew guard
	const expected = await signCookie(secret, ts, ip);
	return timingSafeEqual(value, expected);
}

function timingSafeEqual(a: string, b: string): boolean {
	if (a.length !== b.length) return false;
	let result = 0;
	for (let i = 0; i < a.length; i++) {
		result |= a.charCodeAt(i) ^ b.charCodeAt(i);
	}
	return result === 0;
}
