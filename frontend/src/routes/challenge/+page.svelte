<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { SITE_KEY, SCRIPT_SRC, storeSession } from '$lib/turnstile';

	let status = $state('Verifying...');
	let showWidget = $state(false);

	onMount(async () => {
		const returnTo = $page.url.searchParams.get('return') || '/';

		// Reject open redirects
		const safeReturn = returnTo.startsWith('/') && !returnTo.startsWith('//') ? returnTo : '/';

		await new Promise<void>((resolve) => {
			const s = document.createElement('script');
			s.src = SCRIPT_SRC;
			s.onload = () => resolve();
			s.onerror = () => {
				status = 'Could not load challenge. Check your connection and refresh.';
				resolve();
			};
			document.head.appendChild(s);
		});

		const turnstile = window.turnstile;
		if (!turnstile) return;

		turnstile.render('#challenge-widget', {
			sitekey: SITE_KEY,
			size: 'flexible',
			appearance: 'interaction-only',
			callback: async (token: string) => {
				status = 'Verified. Redirecting...';
				try {
					const resp = await fetch('/challenge', {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ token })
					});
					if (resp.ok) {
						// Persist the API session so the app shell does not run a
						// second solve right after this redirect.
						const data = await resp.json().catch(() => null);
						if (data?.session_token) storeSession(data.session_token);
						window.location.href = safeReturn;
					} else {
						status = 'Verification failed. Refresh to retry.';
					}
				} catch {
					status = 'Network error. Refresh to retry.';
				}
			},
			'error-callback': () => {
				status = 'Challenge error. Refresh to retry.';
				showWidget = true;
			},
			'before-interactive-callback': () => {
				showWidget = true;
				status = 'Please complete the challenge below.';
			}
		});
	});
</script>

<svelte:head>
	<title>Verifying... | engagic</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<main>
	<div class="challenge">
		<h1>engagic</h1>
		<p class="status" class:visible={showWidget}>{status}</p>
		<div id="challenge-widget" class:visible={showWidget}></div>
	</div>
</main>

<style>
	main {
		min-height: 100vh;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 1rem;
	}
	.challenge {
		text-align: center;
		max-width: 400px;
	}
	h1 {
		font-family: var(--font-display, 'Crimson Pro', serif);
		font-size: 2.5rem;
		font-weight: 400;
		margin: 0 0 1.5rem;
		color: var(--text-primary, #222);
	}
	.status {
		font-family: var(--font-mono, monospace);
		font-size: 0.9rem;
		color: var(--text-secondary, #666);
		margin: 0 0 1rem;
	}
	#challenge-widget {
		margin-top: 1rem;
		display: none;
	}
	#challenge-widget.visible {
		display: flex;
		justify-content: center;
	}
</style>
