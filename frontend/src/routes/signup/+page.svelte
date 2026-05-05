<script lang="ts">
	import { signup } from '$lib/api/auth';
	import { page } from '$app/stores';
	import { logger } from '$lib/services/logger';
	import { onMount } from 'svelte';

	let email = $state('');
	let name = $state('');
	let loading = $state(false);
	let success = $state(false);
	let error = $state('');

	// Get city from query params (from 404 page redirect)
	const cityBanana = $derived($page.url.searchParams.get('city') || '');
	const cityDisplayName = $derived($page.url.searchParams.get('name') || '');

	onMount(() => {
		logger.trackEvent('signup_view', { source: cityBanana ? 'city_request' : 'direct' });
	});

	function isValidEmail(email: string): boolean {
		const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
		return emailRegex.test(email);
	}

	async function handleSubmit() {
		error = '';

		if (!email.trim()) {
			error = 'Email is required';
			return;
		}

		if (!isValidEmail(email)) {
			error = 'Please enter a valid email address';
			return;
		}

		if (!name.trim()) {
			error = 'Name is required';
			return;
		}

		loading = true;

		try {
			await signup({
				email: email.trim(),
				name: name.trim(),
				city_banana: cityBanana || undefined
			});
			success = true;
			logger.trackEvent('signup_submit', { source: cityBanana ? 'city_request' : 'direct' });
		} catch (err: Error | unknown) {
			error = err instanceof Error ? err.message : 'Failed to create account';
			logger.error('Signup error', {}, err instanceof Error ? err : undefined);
		} finally {
			loading = false;
		}
	}
</script>

<svelte:head>
	<title>Sign Up - Engagic</title>
	<meta
		name="description"
		content="Create a free Engagic account. Stay informed about local government and make your voice heard."
	/>
</svelte:head>

<div class="page">
	<div class="container">
		<a href="/" class="home-link">← engagic</a>
		{#if success}
			<div class="card success-state">
				<div class="icon-wrapper">
					<div class="icon-check" aria-hidden="true">✓</div>
				</div>
				<h1>Check Your Email</h1>
				<p class="message">
					We've sent a verification link to <strong>{email}</strong>
				</p>
				<p class="hint">
					Click the link in the email to access your dashboard. The link expires in 15 minutes.
				</p>
				{#if cityDisplayName}
					<p class="watching-confirmation">
						You're now watching {cityDisplayName}. We'll email you when we add coverage.
					</p>
				{/if}
			</div>
		{:else}
			<div class="card">
				{#if cityDisplayName}
					<div class="jurisdiction-context">
						<span class="jurisdiction-badge">Watching: {cityDisplayName}</span>
					</div>
					<h1>Get Notified</h1>
					<p class="subtitle">We'll email you when we add {cityDisplayName} to our coverage.</p>
				{:else}
					<h1>Get Started</h1>
					<p class="subtitle">Know what's happening. Have your say. Set up in 30 seconds.</p>
				{/if}

				<form onsubmit={(e) => {e.preventDefault(); handleSubmit();}}>
					<div class="field">
						<label for="name">Name</label>
						<input
							id="name"
							type="text"
							bind:value={name}
							placeholder="Your name"
							disabled={loading}
							required
							class="input"
							autocomplete="name"
							aria-describedby={error ? 'error-message' : undefined}
							aria-invalid={!!error}
						/>
					</div>

					<div class="field">
						<label for="email">Email</label>
						<input
							id="email"
							type="email"
							bind:value={email}
							placeholder="you@example.com"
							disabled={loading}
							required
							class="input"
							autocomplete="email"
							aria-describedby={error ? 'error-message' : undefined}
							aria-invalid={!!error}
						/>
					</div>

					{#if error}
						<div class="error-banner" role="alert" id="error-message">{error}</div>
					{/if}

					<button type="submit" class="btn-primary" disabled={loading}>
						{loading ? 'Creating account...' : 'Create Free Account'}
					</button>

					<p class="footer-text">
						Already have an account? <a href="/login">Log in</a>
					</p>

					<p class="free-forever">
						Free forever. Add cities and keywords after signup.
					</p>
				</form>
			</div>
		{/if}
	</div>
</div>

<style>
	.page {
		min-height: 100vh;
		background: var(--bg-gradient-start);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 2rem;
	}

	.container {
		width: 100%;
		max-width: var(--width-auth);
	}

	.home-link {
		display: inline-block;
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--civic-blue);
		text-decoration: none;
		margin-bottom: 1.5rem;
		transition: color var(--transition-fast);
	}

	.home-link:hover {
		color: var(--civic-accent);
	}

	.card {
		background: var(--civic-white);
		border: 1px solid var(--civic-border);
		border-radius: var(--radius-md);
		padding: 2.5rem;
	}

	.success-state {
		text-align: center;
	}

	.icon-wrapper {
		display: flex;
		justify-content: center;
		margin-bottom: 1.5rem;
	}

	.icon-check {
		width: 64px;
		height: 64px;
		background: var(--success-bg);
		border: 1px solid var(--success-border);
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 2rem;
		color: var(--success-border);
		font-weight: bold;
	}

	.jurisdiction-context {
		margin-bottom: 1rem;
	}

	.jurisdiction-badge {
		display: inline-block;
		padding: 0.5rem 1rem;
		background: var(--badge-info-bg);
		border: 1px solid var(--civic-blue);
		border-radius: var(--radius-pill);
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--civic-blue);
	}

	h1 {
		font-family: var(--font-display);
		font-size: 1.875rem;
		font-weight: 400;
		margin: 0 0 0.75rem 0;
		color: var(--text-primary);
		letter-spacing: -0.02em;
	}

	.subtitle {
		font-size: 1rem;
		color: var(--civic-gray);
		margin: 0 0 2rem 0;
		line-height: 1.5;
	}

	.message {
		font-size: 1.125rem;
		color: var(--civic-gray);
		margin: 0 0 1rem 0;
		line-height: 1.6;
	}

	.message strong {
		color: var(--civic-blue);
		font-weight: 600;
	}

	.hint {
		font-size: 0.875rem;
		color: var(--civic-gray);
		margin: 0;
		line-height: 1.6;
	}

	.watching-confirmation {
		font-size: 0.875rem;
		color: var(--success-text);
		font-weight: 500;
		margin: 1rem 0 0 0;
		padding: 0.75rem 1rem;
		background: var(--success-bg);
		border: 1px solid var(--success-border);
		border-radius: var(--radius-md);
		line-height: 1.5;
	}

	.field {
		margin-bottom: 1.5rem;
	}

	label {
		display: block;
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--civic-dark);
		margin-bottom: 0.5rem;
	}

	.input {
		width: 100%;
		padding: 0.75rem 1rem;
		font-size: 1rem;
		font-family: var(--font-body);
		color: var(--text-primary);
		background: var(--surface-primary);
		border: 2px solid var(--civic-border);
		border-radius: var(--radius-md);
		transition: all var(--transition-normal);
	}

	.input:focus {
		outline: none;
		border: 2px solid var(--civic-blue);
		box-shadow: 0 0 0 3px var(--shadow-sm);
	}

	.input:disabled {
		opacity: 0.5;
		cursor: not-allowed;
		background: var(--civic-light);
	}

	.input::placeholder {
		color: var(--civic-gray);
	}

	.error-banner {
		padding: 1rem;
		background: var(--error-bg);
		border: 1px solid var(--error-border);
		border-radius: var(--radius-md);
		color: var(--error-text);
		font-size: 0.875rem;
		margin-bottom: 1.5rem;
		font-weight: 500;
	}

	.btn-primary {
		width: 100%;
		padding: 1rem 1.5rem;
		font-size: 1rem;
		font-weight: 600;
		background: var(--civic-blue);
		color: white;
		border: none;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all var(--transition-normal);
		font-family: var(--font-body);
	}

	.btn-primary:hover:not(:disabled) {
		background: var(--civic-accent);
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.footer-text {
		text-align: center;
		margin-top: 1.5rem;
		font-size: 0.875rem;
		color: var(--civic-gray);
	}

	.footer-text a {
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 600;
		transition: color var(--transition-normal);
	}

	.footer-text a:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.free-forever {
		text-align: center;
		margin-top: 1rem;
		font-size: 0.8125rem;
		color: var(--civic-gray);
		font-style: italic;
	}

	@media (max-width: 640px) {
		.page {
			padding: 1rem;
		}

		.card {
			padding: 1.5rem;
		}

		h1 {
			font-size: 1.5rem;
		}

		.icon-check {
			width: 56px;
			height: 56px;
			font-size: 1.5rem;
		}
	}

</style>
