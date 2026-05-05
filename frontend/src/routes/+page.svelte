<script lang="ts">
	import { goto } from '$app/navigation';
	import { apiClient } from '$lib/api/api-client';
	import SeoHead from '$lib/components/SeoHead.svelte';
	import type { SearchResult, CityOption, Meeting } from '$lib/api/types';
	import { isSearchSuccess, isSearchAmbiguous } from '$lib/api/types';
	import { generateCityUrl, generateMeetingSlug } from '$lib/utils/utils';
	import { validateSearchQuery } from '$lib/utils/sanitize';
	import { logger } from '$lib/services/logger';
	import Footer from '$lib/components/Footer.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	let searchQuery = $state('');
	let searchResults: SearchResult | null = $state(null);
	let loading = $state(false);
	let loadingRandom = $state(false);
	let loadingRandomPolicy = $state(false);
	let error = $state('');

	// Snapshot: Preserve search state during navigation
	// When user searches for a city, gets ambiguous results, clicks one, and navigates back,
	// this ensures the search query and results list are restored
	export const snapshot = {
		capture: () => ({
			searchQuery,
			searchResults,
			error,
			scrollY: typeof window !== 'undefined' ? window.scrollY : 0
		}),
		restore: (values: {
			searchQuery: string;
			searchResults: SearchResult | null;
			error: string;
			scrollY: number;
		}) => {
			searchQuery = values.searchQuery;
			searchResults = values.searchResults;
			error = values.error;
			// Restore scroll position after DOM has updated
			if (typeof window !== 'undefined' && typeof values.scrollY === 'number') {
				setTimeout(() => window.scrollTo(0, values.scrollY), 0);
			}
		}
	};

	async function handleSearch() {
		const validationError = validateSearchQuery(searchQuery);
		if (validationError) {
			error = validationError;
			return;
		}

		// Check for state search first - redirect immediately without API call
		const stateDetection = detectStateSearch(searchQuery);
		if (stateDetection.isState && stateDetection.stateCode) {
			logger.trackEvent('state_search', { query: searchQuery, state: stateDetection.stateCode });
			goto(`/state/${stateDetection.stateCode}`);
			return;
		}

		loading = true;
		error = '';
		searchResults = null;

		try {
			const result = await apiClient.searchMeetings(searchQuery.trim());
			searchResults = result;

			// If successful and has city info, navigate to city page with cached data
			if (isSearchSuccess(result)) {
				const cityUrl = generateCityUrl(result.city_name, result.state);
				logger.trackEvent('search_success', { query: searchQuery, city: result.city_name });
				// Pass search results through navigation state to avoid duplicate API call
				goto(`/${cityUrl}`, { state: { cachedSearchResults: result } });
			} else if (isSearchAmbiguous(result)) {
				logger.trackEvent('search_ambiguous', { query: searchQuery });
			} else {
				// City not found
				logger.trackEvent('search_not_found', { query: searchQuery });
			}
		} catch (err) {
			logger.error('Search failed', err as Error, { query: searchQuery });
			error = err instanceof Error ? err.message : 'Search failed. Please try again.';
		} finally {
			loading = false;
		}
	}

	async function handleCityOptionClick(cityOption: CityOption) {
		const cityUrl = generateCityUrl(cityOption.city_name, cityOption.state);

		// Fetch city data once and pass through navigation state
		loading = true;
		try {
			const result = await apiClient.searchMeetings(`${cityOption.city_name}, ${cityOption.state}`);
			goto(`/${cityUrl}`, { state: { cachedSearchResults: result } });
		} catch (err) {
			// Fallback: navigate without cache, let city page handle fetch
			goto(`/${cityUrl}`);
		} finally {
			loading = false;
		}
	}
	
	async function handleRandomMeeting() {
		loadingRandom = true;
		error = '';

		try {
			const result = await apiClient.getRandomMeetingWithItems();
			if (result.meeting) {
				// Extract city name and state from banana
				// banana format is like "planoTX" - city name + state code
				const banana = result.meeting.banana;
				const stateMatch = banana.match(/([A-Z]{2})$/);

				if (stateMatch) {
					// Navigate directly to the meeting detail page
					const cityUrl = banana; // banana is already in the right format

					// Create a Meeting object for slug generation
					const meeting: Meeting = {
						id: result.meeting.id,
						banana: result.meeting.banana,
						title: result.meeting.title,
						date: result.meeting.date,
						packet_url: result.meeting.packet_url
					};

					const meetingSlug = generateMeetingSlug(meeting);
					logger.trackEvent('random_meeting_click', {
						city: banana,
						item_count: result.meeting.item_count
					});

					goto(`/${cityUrl}/${meetingSlug}`);
				} else {
					error = 'Invalid meeting data received';
				}
			}
		} catch (err) {
			logger.error('Random meeting failed', err as Error);
			error = 'Failed to load random meeting. Please try again.';
		} finally {
			loadingRandom = false;
		}
	}

	async function handleRandomPolicy() {
		loadingRandomPolicy = true;
		error = '';

		try {
			const result = await apiClient.getRandomMatter();
			if (result.matter) {
				logger.trackEvent('random_policy_click', {
					matter_id: result.matter.id,
					city: result.matter.banana,
					appearance_count: result.matter.appearance_count
				});

				goto(`/matter/${result.matter.id}`);
			}
		} catch (err) {
			logger.error('Random policy failed', err as Error);
			error = 'Failed to load random policy. Please try again.';
		} finally {
			loadingRandomPolicy = false;
		}
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter') {
			handleSearch();
		}
	}

	// Detect if search is for a state
	const STATE_CODES: Record<string, string> = {
		AL: 'Alabama', AK: 'Alaska', AZ: 'Arizona', AR: 'Arkansas', CA: 'California',
		CO: 'Colorado', CT: 'Connecticut', DE: 'Delaware', FL: 'Florida', GA: 'Georgia',
		HI: 'Hawaii', ID: 'Idaho', IL: 'Illinois', IN: 'Indiana', IA: 'Iowa',
		KS: 'Kansas', KY: 'Kentucky', LA: 'Louisiana', ME: 'Maine', MD: 'Maryland',
		MA: 'Massachusetts', MI: 'Michigan', MN: 'Minnesota', MS: 'Mississippi', MO: 'Missouri',
		MT: 'Montana', NE: 'Nebraska', NV: 'Nevada', NH: 'New Hampshire', NJ: 'New Jersey',
		NM: 'New Mexico', NY: 'New York', NC: 'North Carolina', ND: 'North Dakota', OH: 'Ohio',
		OK: 'Oklahoma', OR: 'Oregon', PA: 'Pennsylvania', RI: 'Rhode Island', SC: 'South Carolina',
		SD: 'South Dakota', TN: 'Tennessee', TX: 'Texas', UT: 'Utah', VT: 'Vermont',
		VA: 'Virginia', WA: 'Washington', WV: 'West Virginia', WI: 'Wisconsin', WY: 'Wyoming'
	};

	// States where the name is also a major city - only match 2-letter codes for these
	const AMBIGUOUS_STATES = new Set(['New York']);

	function detectStateSearch(query: string): { isState: boolean; stateCode?: string; stateName?: string } {
		const trimmed = query.trim();
		const upper = trimmed.toUpperCase();

		// Check if it's a 2-letter state code (always redirect to state)
		if (upper.length === 2 && STATE_CODES[upper]) {
			return { isState: true, stateCode: upper, stateName: STATE_CODES[upper] };
		}

		// Check if it's a full state name (but not ambiguous ones like "New York")
		const stateEntry = Object.entries(STATE_CODES).find(([, name]) =>
			name.toLowerCase() === trimmed.toLowerCase()
		);

		if (stateEntry && !AMBIGUOUS_STATES.has(stateEntry[1])) {
			return { isState: true, stateCode: stateEntry[0], stateName: stateEntry[1] };
		}

		return { isState: false };
	}

</script>

<SeoHead
	title="engagic - Help shape your city"
	description="Stay informed. Make your voice heard. AI summaries of local government meetings and civic participation information."
	url="https://engagic.org"
/>

<div class="container">
	<header class="header anim-entry" style="animation-delay: 0ms;">
		<p class="tagline">Know what your city council is deciding — before they decide it.</p>
	</header>

		<div class="search-section anim-entry" style="animation-delay: 80ms;">
		<input
			type="text"
			class="search-input"
			bind:value={searchQuery}
			onkeydown={handleKeydown}
			placeholder="Enter zipcode, city, or state"
			disabled={loading || loadingRandom || loadingRandomPolicy}
			aria-label="Search for local government meetings"
			aria-invalid={!!error}
			aria-describedby={error ? "search-error" : undefined}
		/>
		<button
			class="search-button"
			onclick={handleSearch}
			disabled={loading || loadingRandom || loadingRandomPolicy || !searchQuery.trim()}
		>
			{loading ? 'Searching...' : 'Search'}
		</button>

		{#if searchResults && !searchResults.success && !searchResults.ambiguous}
			<div class="not-found-inline">
				<p class="not-found-message">{searchResults.message}</p>
				<p class="not-found-cta">
					We don't cover this city yet — but we're growing.
					<a href="/signup">Create an account</a> and request it, and we'll prioritize cities with active watchers.
				</p>
			</div>
		{/if}

		{#if searchResults && isSearchAmbiguous(searchResults)}
			<div class="ambiguous-inline">
				<div class="ambiguous-message">
					{@html searchResults.message}
				</div>
				<div class="jurisdiction-options">
					{#each searchResults.city_options as cityOption}
						<button
							class="jurisdiction-option"
							onclick={() => handleCityOptionClick(cityOption)}
						>
							{cityOption.display_name}
						</button>
					{/each}
				</div>
			</div>
		{/if}

		{#if loading}
			<div class="loading-inline">Searching...</div>
		{/if}

		{#if error}
			<div class="error-inline" id="search-error" role="alert">{error}</div>
		{/if}

		<div class="random-buttons anim-entry" style="animation-delay: 160ms;">
			<button
				class="random-button random-meeting"
				onclick={handleRandomMeeting}
				disabled={loading || loadingRandom || loadingRandomPolicy}
			>
				{loadingRandom ? 'Loading...' : 'Discover a Meeting'}
			</button>

			<button
				class="random-button random-policy"
				onclick={handleRandomPolicy}
				disabled={loading || loadingRandom || loadingRandomPolicy}
			>
				{loadingRandomPolicy ? 'Loading...' : 'Discover a Policy'}
			</button>
		</div>
	</div>

	{#if data.happening?.items && data.happening.items.length > 0}
		<section class="happening-section anim-entry" style="animation-delay: 300ms;">
			<h2 class="happening-title">What's Happening</h2>
			<div class="happening-list">
				{#each data.happening.items as item (item.item_id)}
					<a href="/{item.banana}" class="happening-card">
						<div class="happening-card-header">
							<span class="happening-city">{item.banana}</span>
							{#if item.meeting_date}
								<span class="happening-date">{new Date(item.meeting_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</span>
							{/if}
						</div>
						<h3 class="happening-item-title">{item.item_title || item.meeting_title || 'Agenda Item'}</h3>
						<p class="happening-reason">{item.reason}</p>
					</a>
				{/each}
			</div>
		</section>
	{/if}

	<Footer analytics={data.analytics} />
</div>

<style>
	/* Extracted from app.css - homepage-specific styles */
	.header {
		text-align: center;
		margin-bottom: 2rem;
	}

	.anim-entry {
		animation: fadeSlideUp 0.5s ease-out both;
	}

	.tagline {
		font-family: var(--font-display);
		color: var(--text-primary);
		font-size: clamp(1.4rem, 4vw, 1.8rem);
		font-weight: 400;
		line-height: 1.3;
		letter-spacing: -0.01em;
		margin-bottom: 1rem;
		max-width: 440px;
		margin-left: auto;
		margin-right: auto;
	}

	.search-section {
		margin-bottom: 3rem;
	}

	.search-input {
		width: 100%;
		padding: 1rem 1.25rem;
		font-size: 1.05rem;
		font-family: var(--font-body);
		border: 2px solid var(--border-primary);
		border-radius: var(--radius-md);
		background: transparent;
		color: var(--text-primary);
		transition: border-color 0.2s ease;
	}

	.search-input:focus {
		outline: none;
		border-color: var(--civic-blue);
	}

	.search-button {
		margin-top: 1rem;
		width: 100%;
		padding: 0.85rem;
		font-size: 0.9rem;
		font-family: var(--font-body);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		background: var(--civic-blue);
		color: var(--civic-white);
		border: none;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: background 0.2s ease;
	}

	.search-button:hover {
		background: var(--civic-accent);
	}

	.search-button:active {
		opacity: 0.9;
	}

	.search-button:disabled {
		background: var(--civic-gray);
		cursor: not-allowed;
		opacity: 0.6;
	}

	.random-buttons {
		display: flex;
		gap: 1rem;
		width: 100%;
		margin-top: 1.5rem;
	}

	.random-button {
		flex: 1;
		padding: 0.75rem;
		font-size: 0.85rem;
		font-family: var(--font-body);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--civic-white);
		border: none;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: background 0.2s ease;
	}

	.random-meeting {
		background: var(--random-meeting-bg);
	}

	.random-meeting:hover {
		background: var(--random-meeting-hover);
	}

	.random-policy {
		background: var(--random-policy-bg);
	}

	.random-policy:hover {
		background: var(--random-policy-hover);
	}

	.random-button:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	/* Happening section */
	.happening-section {
		margin-top: 2rem;
		margin-bottom: 2rem;
		padding-top: 1.5rem;
		border-top: 1px solid var(--border-primary);
	}

	.happening-title {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		text-align: left;
		margin: 0 0 1rem 0;
		padding-bottom: 0.5rem;
		border-bottom: 2px solid var(--text-primary);
	}

	.happening-list {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.happening-card {
		display: block;
		background: transparent;
		border: none;
		border-bottom: 1px solid var(--border-primary);
		padding: 0.75rem 0;
		text-decoration: none;
		color: inherit;
		transition: padding-left 0.2s ease;
	}

	.happening-card:hover {
		padding-left: 6px;
	}

	.happening-card-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.4rem;
	}

	.happening-city {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--civic-blue);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.happening-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.happening-item-title {
		font-family: var(--font-display);
		font-size: 1.05rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0 0 0.25rem 0;
		line-height: 1.3;
	}

	.happening-reason {
		font-size: 0.85rem;
		color: var(--text-secondary);
		margin: 0;
		line-height: 1.5;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.not-found-inline {
		margin-top: 1.25rem;
		padding: 1.25rem;
		background: var(--surface-secondary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-md);
		text-align: left;
	}

	.not-found-message {
		font-family: var(--font-body);
		font-size: 1rem;
		color: var(--text-primary);
		margin: 0 0 0.5rem 0;
	}

	.not-found-cta {
		font-family: var(--font-body);
		font-size: 0.95rem;
		color: var(--text-secondary);
		margin: 0;
	}

	.not-found-cta a {
		color: var(--civic-blue);
		text-decoration: underline;
		text-underline-offset: 2px;
	}

	.not-found-cta a:hover {
		color: var(--civic-accent);
	}

	.ambiguous-inline {
		margin-top: 1.25rem;
		padding: 1.25rem;
		background: var(--surface-secondary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-md);
		text-align: left;
	}

	.ambiguous-inline .ambiguous-message {
		font-family: var(--font-body);
		font-size: 1rem;
		color: var(--text-primary);
		margin: 0 0 1rem 0;
		line-height: 1.5;
	}

	.ambiguous-inline .jurisdiction-options {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.ambiguous-inline .jurisdiction-option {
		padding: 0.5rem 1rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 500;
		color: var(--civic-blue);
		cursor: pointer;
		transition: all var(--transition-fast);
	}

	.ambiguous-inline .jurisdiction-option:hover {
		background: var(--civic-blue);
		color: white;
		border-color: var(--civic-blue);
	}

	.loading-inline {
		margin-top: 1rem;
		padding: 0.75rem 1rem;
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--text-secondary);
		text-align: center;
	}

	.error-inline {
		margin-top: 1rem;
		padding: 0.75rem 1rem;
		background: var(--alert-bg);
		border-left: 3px solid var(--alert-icon);
		border-radius: var(--radius-sm);
		font-size: 0.9rem;
		color: var(--alert-text);
	}

	@media (max-width: 640px) {
		.header {
			margin-bottom: 1.5rem;
		}

		.tagline {
			font-size: 1.3rem;
		}

		.search-input {
			padding: 1rem;
			font-size: 1rem;
		}

		.search-button {
			padding: 0.85rem;
			font-size: 0.95rem;
		}

		.random-buttons {
			flex-direction: column;
		}

		.random-button {
			padding: 0.85rem;
			font-size: 0.95rem;
		}

		.bottom-links-row {
			flex-wrap: wrap;
		}
	}
</style>