<script lang="ts">
	import { page } from '$app/stores';
	import Footer from '$lib/components/Footer.svelte';
	import { parseCityUrl } from '$lib/utils/utils';
	import { authState } from '$lib/stores/auth.svelte';
	import { requestCity } from '$lib/api/dashboard';

	const error = $derived($page.error);
	const status = $derived($page.status);

	// Extract city info from URL for the signup link
	const cityUrl = $derived($page.params.city_url || '');
	const parsed = $derived(parseCityUrl(cityUrl));
	const cityBanana = $derived(parsed ? `${parsed.cityName.toLowerCase().replace(/\s+/g, '')}${parsed.state}` : '');
	// Capitalize city name for display (URL has it lowercase)
	const cityNameCapitalized = $derived(parsed ? parsed.cityName.charAt(0).toUpperCase() + parsed.cityName.slice(1) : '');
	const cityDisplay = $derived(parsed ? `${cityNameCapitalized}, ${parsed.state}` : cityUrl);

	// Auth state for logged-in users
	const isLoggedIn = $derived(authState.isAuthenticated);

	// Request state
	let requestSent = $state(false);
	let requestLoading = $state(false);
	let requestError = $state('');

	async function handleRequestCity() {
		if (!cityBanana || !authState.accessToken) return;

		requestLoading = true;
		requestError = '';

		try {
			await requestCity(authState.accessToken, cityBanana);
			requestSent = true;
		} catch (err) {
			requestError = err instanceof Error ? err.message : 'Failed to submit request';
		} finally {
			requestLoading = false;
		}
	}
</script>

<svelte:head>
	<title>{status} - engagic</title>
</svelte:head>

<div class="container">
	<div class="main-content">
		<a href="/" class="compact-logo">engagic</a>

		<div class="error-container">
			<h1 class="error-code">{status}</h1>

			{#if status === 404}
				<h2 class="error-title">City Not Found</h2>
				<p class="error-message">
					{error?.message || 'We could not find this city in our database.'}
				</p>
				<p class="error-hint">
					Agendas are typically posted 48 hours before meetings. If this is a valid city,
					please check back later or try searching by zipcode.
				</p>
				<div class="request-jurisdiction-cta">
					<p class="cta-text">Want us to track {cityDisplay || 'this city'}?</p>
					{#if requestSent}
						<p class="cta-success">You're now watching {cityDisplay}. We'll email you when it's added.</p>
					{:else if isLoggedIn}
						<button
							class="cta-button"
							onclick={handleRequestCity}
							disabled={requestLoading}
						>
							{requestLoading ? 'Adding to watchlist...' : 'Watch this city'}
						</button>
						{#if requestError}
							<p class="cta-error">{requestError}</p>
						{/if}
					{:else}
						<a href="/signup?city={encodeURIComponent(cityBanana)}&name={encodeURIComponent(cityDisplay)}" class="cta-link">
							Sign up for alerts when we add it
						</a>
					{/if}
					<p class="cta-subtext">Cities with active watchers get priority coverage.</p>
				</div>
			{:else}
				<h2 class="error-title">Something Went Wrong</h2>
				<p class="error-message">
					{error?.message || 'An unexpected error occurred.'}
				</p>
			{/if}

			<div class="error-actions">
				<a href="/" class="btn-primary">← Back to Search</a>
			</div>
		</div>
	</div>

	<Footer />
</div>

<style>
	.container {
		width: var(--width-meetings);
		position: relative;
	}

	.compact-logo {
		position: absolute;
		top: 0;
		right: 1rem;
		font-family: var(--font-mono);
		font-size: 1.1rem;
		font-weight: 700;
		color: var(--civic-blue);
		text-decoration: none;
		z-index: 10;
	}

	.compact-logo:hover {
		opacity: 0.8;
	}

	.error-container {
		text-align: center;
		padding: 4rem 2rem;
		max-width: 600px;
		margin: 0 auto;
	}

	.error-code {
		font-family: var(--font-mono);
		font-size: 6rem;
		font-weight: 700;
		color: var(--civic-blue);
		margin: 0;
		line-height: 1;
	}

	.error-title {
		font-family: var(--font-mono);
		font-size: 1.75rem;
		font-weight: 600;
		color: var(--civic-dark);
		margin: 1rem 0;
	}

	.error-message {
		font-family: var(--font-body);
		font-size: 1.1rem;
		color: var(--text-secondary);
		margin: 1rem 0;
		line-height: 1.6;
	}

	.error-hint {
		font-family: var(--font-body);
		font-size: 0.95rem;
		color: var(--text-tertiary);
		margin: 1.5rem 0;
		line-height: 1.6;
	}

	.request-jurisdiction-cta {
		margin: 2rem 0;
		padding: 1.5rem;
		background: var(--surface-secondary);
		border: 2px solid var(--civic-blue);
		border-radius: var(--radius-lg);
	}

	.cta-text {
		font-family: var(--font-mono);
		font-size: 1rem;
		font-weight: 600;
		color: var(--civic-dark);
		margin: 0 0 0.5rem 0;
	}

	.cta-link {
		display: inline-block;
		color: var(--civic-blue);
		font-family: var(--font-mono);
		font-size: 0.95rem;
		font-weight: 500;
		text-decoration: underline;
		text-underline-offset: 3px;
	}

	.cta-link:hover {
		opacity: 0.8;
	}

	.cta-button {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		background: var(--civic-blue);
		color: white;
		border: none;
		border-radius: 0.5rem;
		font-family: var(--font-mono);
		font-size: 0.95rem;
		font-weight: 500;
		cursor: pointer;
		transition: opacity 0.2s;
	}

	.cta-button:hover:not(:disabled) {
		opacity: 0.9;
	}

	.cta-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.cta-success {
		color: var(--civic-green);
		font-family: var(--font-mono);
		font-size: 0.95rem;
		font-weight: 500;
		margin: 0.5rem 0;
	}

	.cta-error {
		color: var(--civic-red);
		font-family: var(--font-body);
		font-size: 0.85rem;
		margin: 0.5rem 0 0 0;
	}

	.cta-subtext {
		font-family: var(--font-body);
		font-size: 0.85rem;
		color: var(--text-tertiary);
		margin: 0.75rem 0 0 0;
	}

	.error-actions {
		margin-top: 2rem;
	}

	.btn-primary {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		background: var(--civic-blue);
		color: white;
		text-decoration: none;
		border-radius: 0.5rem;
		font-family: var(--font-mono);
		font-weight: 500;
		transition: opacity 0.2s;
	}

	.btn-primary:hover {
		opacity: 0.9;
	}

	@media (max-width: 640px) {
		.container {
			width: 100%;
		}

		.compact-logo {
			font-size: 0.95rem;
			right: 0.75rem;
		}

		.error-container {
			padding: 3rem 1rem;
		}

		.error-code {
			font-size: 4rem;
		}

		.error-title {
			font-size: 1.5rem;
		}

		.error-message {
			font-size: 1rem;
		}
	}
</style>
