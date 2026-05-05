<script lang="ts" module>
	// Module-level caches - persist across component instances and navigation
	// Cleared only on full page reload
	const CACHE_DURATION = 120000; // 2 minutes
	const mattersCache = new Map<string, { data: any; timestamp: number }>();
	const happeningCache = new Map<string, { data: any; timestamp: number }>();
</script>

<script lang="ts">
	import { page } from '$app/stores';
	import type { Meeting, CitySearchItemResult, CitySearchMatterResult, HappeningItem } from '$lib/api/index';
	import { getCityMatters, searchCityMeetings, searchCityMatters, getHappeningItems } from '$lib/api/index';
	import MeetingCard from '$lib/components/MeetingCard.svelte';
	import MatterTimeline from '$lib/components/MatterTimeline.svelte';
	import SearchResultCard from '$lib/components/SearchResultCard.svelte';
	import HappeningSection from '$lib/components/HappeningSection.svelte';
	import Footer from '$lib/components/Footer.svelte';
	import WatchCityModal from '$lib/components/WatchCityModal.svelte';
	import SeoHead from '$lib/components/SeoHead.svelte';
	import { logger } from '$lib/services/logger';
	import { authState } from '$lib/stores/auth.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();


	const city_banana = $derived($page.params.city_url ?? '');
	const isWatching = $derived(authState.subscribedCities.includes(city_banana));
	let showPastMeetings = $state(false);
	let isInitialLoad = $state(true);
	let viewMode = $state<'meetings' | 'matters'>('meetings');
	let cityMatters = $state<any>(null);
	let mattersLoading = $state(false);
	let mattersChecked = $state(false);
	let showWatchModal = $state(false);

	// Text search state
	let searchQuery = $state('');
	let searchMeetingsResults = $state<CitySearchItemResult[] | null>(null);
	let searchMattersResults = $state<CitySearchMatterResult[] | null>(null);
	let searchLoading = $state(false);
	let activeSearchQuery = $state(''); // The query that produced current results

	// Happening This Week state
	let happeningItems = $state<HappeningItem[]>([]);

	// Data comes from server-side load function - reactive to navigation
	const searchResults = $derived(data.searchResults);
	const upcomingMeetings = $derived(data.upcomingMeetings || []);
	const pastMeetings = $derived(data.pastMeetings || []);

	// SEO meta
	const cityDisplayName = $derived(searchResults && 'city_name' in searchResults ? `${searchResults.city_name}, ${searchResults.state}` : 'City');
	const cityDesc = $derived(searchResults && 'city_name' in searchResults ? `City council meetings, agendas, and AI summaries for ${searchResults.city_name}, ${searchResults.state}` : 'Local government meetings and agendas');

	// Reset matters and search state when city changes (client-side navigation)
	$effect(() => {
		// Access city_banana to create dependency
		const currentCity = city_banana;
		// Reset matters when city changes
		cityMatters = null;
		mattersChecked = false;
		viewMode = 'meetings';
		// Reset search state
		searchQuery = '';
		searchMeetingsResults = null;
		searchMattersResults = null;
		activeSearchQuery = '';
		// Reset happening items
		happeningItems = [];
	});

	// Auto-show past meetings when city has no upcoming ones
	$effect(() => {
		if (upcomingMeetings.length === 0 && pastMeetings.length > 0) {
			showPastMeetings = true;
		}
	});

	// Fetch happening items for this city (with cache)
	$effect(() => {
		const banana = city_banana;
		if (!banana) return;

		// Check cache first
		const cached = happeningCache.get(banana);
		const now = Date.now();
		if (cached && (now - cached.timestamp) < CACHE_DURATION) {
			happeningItems = cached.data;
			return;
		}

		getHappeningItems(banana)
			.then(response => {
				if (response.success) {
					happeningItems = response.items;
					happeningCache.set(banana, { data: response.items, timestamp: Date.now() });
				}
			})
			.catch(err => {
				// Silently fail - happening items are optional enhancement
				console.debug('Happening items not available:', err);
			});
	});

	// Derived: Check if city has qualifying matters (2+ appearances)
	// Show toggle until we've checked and confirmed there are no matters
	const hasQualifyingMatters = $derived(
		!mattersChecked || (cityMatters && cityMatters.total_count > 0)
	);

	async function loadCityMatters() {
		if (mattersLoading) return; // Already fetching - prevent concurrent requests
		if (cityMatters) return; // Already loaded in component state

		// Check cache first
		const cached = mattersCache.get(city_banana);
		const now = Date.now();
		if (cached && (now - cached.timestamp) < CACHE_DURATION) {
			// Use cached data - instant load
			cityMatters = cached.data;
			mattersChecked = true;
			// If cached data shows no qualifying matters, switch back to meetings view
			if (cached.data.total_count === 0) {
				viewMode = 'meetings';
			}
			return;
		}

		// Fetch fresh data from API
		mattersLoading = true;
		try {
			const result = await getCityMatters(city_banana, 50, 0);
			cityMatters = result;
			mattersChecked = true;

			// Store in cache with timestamp
			mattersCache.set(city_banana, {
				data: result,
				timestamp: now
			});

			// If no qualifying matters, switch back to meetings view
			if (result.total_count === 0) {
				viewMode = 'meetings';
			}
		} catch (err) {
			logger.error('Failed to load city matters', {}, err instanceof Error ? err : undefined);
			// Don't mark as checked on error - allow retry
		} finally {
			mattersLoading = false;
		}
	}

	function switchToMatters() {
		viewMode = 'matters';
		// Fire-and-forget: load data without blocking the interaction
		loadCityMatters().then(() => {
			if (activeSearchQuery) performSearch();
		});
	}

	// Search function - calls appropriate endpoint based on viewMode
	async function performSearch() {
		const query = searchQuery.trim();
		if (!query) {
			// Clear search results
			searchMeetingsResults = null;
			searchMattersResults = null;
			activeSearchQuery = '';
			return;
		}

		searchLoading = true;
		activeSearchQuery = query;

		try {
			if (viewMode === 'meetings') {
				const result = await searchCityMeetings(city_banana, query);
				searchMeetingsResults = result.results;
			} else {
				const result = await searchCityMatters(city_banana, query);
				searchMattersResults = result.results;
			}
		} catch (err) {
			logger.error('Search failed', {}, err instanceof Error ? err : undefined);
		} finally {
			searchLoading = false;
		}
	}

	// Handle Enter key in search input
	function handleSearchKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			performSearch();
		}
	}

	// Clear search and return to default view
	function clearSearch() {
		searchQuery = '';
		searchMeetingsResults = null;
		searchMattersResults = null;
		activeSearchQuery = '';
	}

	// Re-search when switching tabs (if there's an active query)
	function switchToMeetings() {
		viewMode = 'meetings';
		if (activeSearchQuery) {
			// Fire-and-forget: don't block the interaction
			performSearch();
		}
	}

	// Snapshot: Preserve UI state and data during navigation
	export const snapshot = {
		capture: () => ({
			showPastMeetings,
			viewMode,
			scrollY: typeof window !== 'undefined' ? window.scrollY : 0
		}),
		restore: (values: {
			showPastMeetings: boolean;
			viewMode: 'meetings' | 'matters';
			scrollY: number;
		}) => {
			showPastMeetings = values.showPastMeetings;
			viewMode = values.viewMode;
			if (viewMode === 'matters') {
				loadCityMatters();
			}
			isInitialLoad = false;
			if (typeof window !== 'undefined' && typeof values.scrollY === 'number') {
				setTimeout(() => window.scrollTo(0, values.scrollY), 0);
			}
		}
	};

</script>

<SeoHead
	title="{cityDisplayName} - engagic"
	description="{cityDesc}"
	url="https://engagic.org/{city_banana}"
/>

<div class="container">
	<div class="main-content">
		<a href="/" class="compact-logo" aria-label="Return to engagic homepage">
			<img src="/icon-64.png" alt="engagic" class="logo-icon" />
		</a>

	<div class="jurisdiction-header">
		<a href="/" class="back-link">← Back to search</a>
		{#if searchResults && 'city_name' in searchResults}
			<div class="jurisdiction-title-row">
				<h1 class="jurisdiction-title">{searchResults.city_name}, {searchResults.state}</h1>
				<div class="jurisdiction-actions">
					<a href="/{city_banana}/council" class="council-link" data-sveltekit-preload-data="tap">
						Council
					</a>
					<a href="/{city_banana}/committees" class="council-link" data-sveltekit-preload-data="tap">
						Committees
					</a>
					<button
					class="watch-jurisdiction-btn"
					class:watching={isWatching}
					onclick={() => showWatchModal = true}
				>
					{isWatching ? 'Watching' : 'Follow city'}
				</button>
				</div>
			</div>
		{/if}
		{#if isWatching && authState.isAuthenticated}
			<div class="dashboard-bridge">
				<a href="/dashboard" class="dashboard-link">View your matches →</a>
			</div>
		{/if}
	</div>

	{#if searchResults && 'city_name' in searchResults && searchResults.city_name}
		<WatchCityModal
			cityName={searchResults.city_name}
			cityBanana={city_banana}
			{isWatching}
			bind:open={showWatchModal}
			onClose={() => showWatchModal = false}
		/>
	{/if}

	{#if searchResults && searchResults.success}
		<div class="controls-row">
			{#if hasQualifyingMatters}
				<div class="view-toggle" role="tablist" aria-label="View mode selection">
					<button
						class="toggle-btn"
						class:active={viewMode === 'meetings'}
						onclick={() => switchToMeetings()}
						role="tab"
						aria-selected={viewMode === 'meetings'}
						aria-controls="content-panel"
					>
						Meetings
					</button>
					<button
						class="toggle-btn"
						class:active={viewMode === 'matters'}
						onclick={() => switchToMatters()}
						role="tab"
						aria-selected={viewMode === 'matters'}
						aria-controls="content-panel"
					>
						Matters
					</button>
				</div>
			{/if}
			<div class="search-container">
				<input
					type="text"
					class="jurisdiction-search"
					placeholder="Search {viewMode}..."
					bind:value={searchQuery}
					onkeydown={handleSearchKeydown}
				/>
				{#if activeSearchQuery}
					<button class="clear-search" onclick={clearSearch} aria-label="Clear search">
						x
					</button>
				{/if}
			</div>
		</div>

		<div class="source-row">
			{#if searchResults && 'city_name' in searchResults && searchResults.source_url && searchResults.vendor_display_name}
				<div class="source-footnote">
					<span class="attribution-text">Data sourced from</span>
					{#if searchResults.source_urls && searchResults.source_urls.length > 0}
						{#each searchResults.source_urls as source, i}
							{#if i > 0}<span class="source-separator"> | </span>{/if}
							<a
								href={source.url}
								target="_blank"
								rel="noopener noreferrer"
								class="source-link"
							>
								{searchResults.vendor_display_name}{source.body ? ` - ${source.body}` : ''}
							</a>
						{/each}
					{:else}
						<a
							href={searchResults.source_url}
							target="_blank"
							rel="noopener noreferrer"
							class="source-link"
						>
							{searchResults.vendor_display_name}
						</a>
					{/if}
				</div>
			{/if}
			{#if !isWatching}
				<button class="follow-cta-link" onclick={() => showWatchModal = true}>
					Follow to keep data fresh — synced weekly
				</button>
			{/if}
		</div>

		{#if viewMode === 'meetings'}
			{#if searchLoading}
				<div class="loading-matters">
					<p>Searching meetings...</p>
				</div>
			{:else if activeSearchQuery && searchMeetingsResults !== null}
				<!-- Search results view -->
				{#if searchMeetingsResults.length > 0}
					<div class="search-results-header">
						<p class="search-results-count">{searchMeetingsResults.length} result{searchMeetingsResults.length === 1 ? '' : 's'} for "{activeSearchQuery}"</p>
					</div>
					<div class="search-results-list">
						{#each searchMeetingsResults as result}
							<SearchResultCard
								{result}
								query={activeSearchQuery}
								cityUrl={city_banana}
							/>
						{/each}
					</div>
				{:else}
					<div class="no-meetings">
						<p class="empty-state-title">No results</p>
						<p class="empty-state-message">No meetings found for "{activeSearchQuery}"</p>
					</div>
				{/if}
			{:else if searchResults.meetings && searchResults.meetings.length > 0}
				<!-- Default view -->
				{#if happeningItems.length > 0}
					<HappeningSection items={happeningItems} cityUrl={city_banana} />
				{/if}
				{#if upcomingMeetings.length > 0 || pastMeetings.length > 0}
					<div class="meetings-filter">
						{#if upcomingMeetings.length > 0}
							<h2 class="meetings-section-title">Upcoming Meetings</h2>
						{/if}
						{#if pastMeetings.length > 0 && upcomingMeetings.length === 0}
							<h2 class="meetings-section-title">No Upcoming Meetings</h2>
							<div class="no-upcoming-cta">
								<p>Follow to activate weekly AI meeting summaries.</p>
								<button class="cta-button-inline" onclick={() => showWatchModal = true}>Follow city</button>
							</div>
						{/if}
						{#if pastMeetings.length > 0}
							<button
								class="toggle-past-btn"
								onclick={() => showPastMeetings = !showPastMeetings}
								aria-label={showPastMeetings ? `Hide ${pastMeetings.length} past meetings` : `Show ${pastMeetings.length} past meetings`}
								aria-expanded={showPastMeetings}
							>
								{showPastMeetings ? 'Hide' : 'Show'} Past Meetings ({pastMeetings.length})
							</button>
						{/if}
					</div>

					<div class="meeting-list">
				{#each upcomingMeetings as meeting, index}
					<MeetingCard
						{meeting}
						cityUrl={city_banana}
						isPast={false}
						animationDuration={isInitialLoad && index < 3 ? 300 : 0}
						animationDelay={isInitialLoad && index < 3 ? index * 50 : 0}
						onIntroEnd={() => { if (index === upcomingMeetings.length - 1 && !showPastMeetings) isInitialLoad = false; }}
					/>
				{/each}

				{#if showPastMeetings}
					{#if pastMeetings.length > 0}
						<h3 class="past-meetings-divider">Past Meetings</h3>
					{/if}
					{#each pastMeetings as meeting, index}
						<MeetingCard
							{meeting}
							cityUrl={city_banana}
							isPast={true}
							animationDuration={isInitialLoad && index < 3 ? 300 : 0}
							animationDelay={isInitialLoad && index < 3 ? index * 50 : 0}
							onIntroEnd={() => { if (index === pastMeetings.length - 1) isInitialLoad = false; }}
						/>
					{/each}
				{/if}
					</div>
				{:else}
					<div class="no-meetings">
						<p class="empty-state-title">No meetings found</p>
						<p class="empty-state-message">This city might not have any upcoming meetings scheduled yet. Check back soon!</p>
						<div class="request-jurisdiction-cta">
							<p class="cta-text">Want AI summaries for this city?</p>
							<button class="cta-button" onclick={() => showWatchModal = true}>Follow city</button>
							<p class="cta-subtext">Followers activate weekly meeting processing and summaries.</p>
						</div>
					</div>
				{/if}
			{:else}
				<div class="no-meetings">
					<p class="empty-state-title">No meetings found</p>
					<p class="empty-state-message">{'message' in searchResults ? searchResults.message : 'We could not find any meetings for this city. Agendas are typically posted 48 hours before meetings.'}</p>
					<div class="request-jurisdiction-cta">
						<p class="cta-text">Want AI summaries for this city?</p>
						<button class="cta-button" onclick={() => showWatchModal = true}>Follow city</button>
						<p class="cta-subtext">Followers activate weekly meeting processing and summaries.</p>
					</div>
				</div>
			{/if}
		{:else if viewMode === 'matters'}
			{#if searchLoading}
				<div class="loading-matters">
					<p>Searching matters...</p>
				</div>
			{:else if activeSearchQuery && searchMattersResults !== null}
				<!-- Search results view -->
				{#if searchMattersResults.length > 0}
					<div class="search-results-header">
						<p class="search-results-count">{searchMattersResults.length} result{searchMattersResults.length === 1 ? '' : 's'} for "{activeSearchQuery}"</p>
					</div>
					<div class="search-results-list">
						{#each searchMattersResults as result}
							<SearchResultCard
								{result}
								query={activeSearchQuery}
								cityUrl={city_banana}
							/>
						{/each}
					</div>
				{:else}
					<div class="no-meetings">
						<p class="empty-state-title">No results</p>
						<p class="empty-state-message">No matters found for "{activeSearchQuery}"</p>
					</div>
				{/if}
			{:else if mattersLoading}
				<div class="loading-matters">
					<p>Loading matters timeline...</p>
				</div>
			{:else if cityMatters && cityMatters.matters && cityMatters.matters.length > 0}
				<div class="matters-view">
					<div class="matters-header">
						<h2 class="matters-title">Legislative Matters</h2>
						<div class="matters-stats">
							<span class="stat">{cityMatters.total_count} matters tracked</span>
						</div>
					</div>
					<div class="matters-list">
						{#each cityMatters.matters as matter}
							{@const hasMultipleAppearances = matter.appearance_count > 1}
							<a href="/matter/{matter.id}" class="matter-card-link">
								<div class="matter-card">
									<div class="matter-card-header">
										{#if matter.matter_file}
											<span class="matter-file-badge">{matter.matter_file}</span>
										{/if}
										{#if matter.matter_type}
											<span class="matter-type-label">{matter.matter_type}</span>
										{/if}
										{#if hasMultipleAppearances}
											<span class="appearances-badge">{matter.appearance_count} appearances</span>
										{/if}
									</div>
									<h3 class="matter-card-title">{matter.title}</h3>
									{#if matter.canonical_topics && matter.canonical_topics.length > 0}
										<div class="matter-card-topics">
											{#each matter.canonical_topics.slice(0, 4) as topic}
												<span class="matter-topic-tag">{topic}</span>
											{/each}
										</div>
									{/if}
									{#if matter.canonical_summary}
										<div class="matter-card-summary">
											{matter.canonical_summary.substring(0, 200)}{matter.canonical_summary.length > 200 ? '...' : ''}
										</div>
									{/if}
									{#if hasMultipleAppearances && matter.timeline}
										<div class="matter-timeline-container">
											<MatterTimeline
												matterFile={matter.matter_file}
												timelineData={{
													success: true,
													matter: {
														id: matter.id,
														banana: matter.banana,
														matter_id: matter.matter_id,
														matter_file: matter.matter_file,
														matter_type: matter.matter_type,
														title: matter.title,
														canonical_summary: matter.canonical_summary,
														canonical_topics: matter.canonical_topics,
														first_seen: matter.first_seen,
														last_seen: matter.last_seen,
														appearance_count: matter.appearance_count
													},
													timeline: matter.timeline,
													appearance_count: matter.appearance_count
												}}
											/>
										</div>
									{/if}
								</div>
							</a>
						{/each}
					</div>
				</div>
			{:else}
				<div class="no-meetings">
					<p class="empty-state-title">No matters found</p>
					<p class="empty-state-message">This city doesn't have any tracked legislative matters yet.</p>
				</div>
			{/if}
		{/if}
	{/if}
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
		z-index: 10;
		transition: transform var(--transition-normal);
	}

	.compact-logo:hover {
		opacity: 0.8;
	}

	.logo-icon {
		width: 48px;
		height: 48px;
		border-radius: var(--radius-lg);
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
	}

	.jurisdiction-header {
		margin-bottom: 2rem;
		min-height: 80px;
	}

	.back-link {
		display: inline-block;
		margin-bottom: 1rem;
		color: var(--civic-blue);
		text-decoration: none;
		font-family: var(--font-mono);
		font-weight: 500;
	}

	.back-link:hover {
		text-decoration: underline;
	}

	.jurisdiction-title-row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 1rem;
		margin-bottom: 0.75rem;
	}

	.jurisdiction-title {
		font-family: var(--font-display);
		font-size: clamp(2rem, 5vw, 2.75rem);
		color: var(--text-primary);
		margin: 0;
		font-weight: 400;
		letter-spacing: -0.02em;
		line-height: 1.1;
	}

	.jurisdiction-actions {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		flex-shrink: 0;
	}

	.council-link {
		padding: 0;
		font-size: 0.8rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		background: none;
		color: var(--civic-gray);
		border: none;
		text-decoration: none;
		transition: color 0.2s ease;
		white-space: nowrap;
		font-family: var(--font-body);
	}

	.council-link:hover {
		color: var(--civic-blue);
	}

	.watch-jurisdiction-btn {
		padding: 0;
		font-size: 0.8rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		background: none;
		color: var(--civic-blue);
		border: none;
		cursor: pointer;
		transition: color 0.2s ease;
		white-space: nowrap;
		font-family: var(--font-body);
	}

	.watch-jurisdiction-btn:hover {
		color: var(--civic-accent);
	}

	.watch-jurisdiction-btn.watching {
		color: var(--civic-green);
	}

	.watch-jurisdiction-btn.watching:hover {
		color: var(--action-yes-hover);
	}

	.source-footnote {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--civic-gray);
		display: flex;
		align-items: center;
		gap: 0.4rem;
		opacity: 0.7;
	}

	.attribution-text {
		opacity: 0.7;
	}

	.source-link {
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 500;
		transition: all var(--transition-normal);
		border-bottom: 1px solid transparent;
	}

	.source-link:hover {
		color: var(--civic-accent);
		border-bottom-color: var(--civic-accent);
	}

	.dashboard-bridge {
		margin-top: 0.5rem;
	}

	.dashboard-link {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 500;
		transition: color var(--transition-fast);
	}

	.dashboard-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.view-toggle {
		display: flex;
		gap: 0.5rem;
		margin-bottom: 2rem;
		background: var(--surface-secondary);
		padding: 0.35rem;
		border-radius: var(--radius-lg);
		width: fit-content;
	}

	.toggle-btn {
		padding: 0.65rem 1.5rem;
		background: transparent;
		border: none;
		border-radius: var(--radius-md);
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--civic-gray);
		cursor: pointer;
		transition: all var(--transition-normal);
	}

	.toggle-btn:hover {
		color: var(--civic-blue);
		background: rgba(181, 100, 42, 0.08);
	}

	.toggle-btn.active {
		background: var(--civic-blue);
		color: white;
		box-shadow: 0 2px 6px rgba(181, 100, 42, 0.2);
	}

	.controls-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		margin-bottom: 2rem;
		flex-wrap: wrap;
	}

	.controls-row .view-toggle {
		margin-bottom: 0;
	}

	.search-container {
		position: relative;
		display: flex;
		align-items: center;
	}

	.jurisdiction-search {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		padding: 0.6rem 2rem 0.6rem 1rem;
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		background: var(--surface-secondary);
		color: var(--text-primary);
		width: 200px;
		transition: border-color var(--transition-normal), box-shadow var(--transition-normal);
	}

	.jurisdiction-search:focus {
		outline: none;
		border-color: var(--civic-blue);
		box-shadow: 0 0 0 3px rgba(181, 100, 42, 0.08);
	}

	.jurisdiction-search::placeholder {
		color: var(--civic-gray);
	}

	.clear-search {
		position: absolute;
		right: 0.5rem;
		background: none;
		border: none;
		color: var(--civic-gray);
		cursor: pointer;
		font-family: var(--font-mono);
		font-size: 1rem;
		padding: 0.25rem;
		line-height: 1;
	}

	.clear-search:hover {
		color: var(--text-primary);
	}

	.search-results-header {
		margin-bottom: 1rem;
	}

	.search-results-count {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--civic-gray);
	}

	.search-results-list {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.loading-matters {
		text-align: center;
		padding: 4rem 2rem;
		color: var(--civic-gray);
		font-family: var(--font-mono);
	}

	.matters-view {
		margin-top: 1rem;
	}

	.matters-header {
		margin-bottom: 1rem;
		border-bottom: 2px solid var(--text-primary);
		padding-bottom: 0.5rem;
	}

	.matters-title {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		margin: 0 0 0.5rem 0;
	}

	.matters-stats {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--civic-gray);
	}

	.matters-list {
		display: flex;
		flex-direction: column;
	}

	.matter-card-link {
		text-decoration: none;
		color: inherit;
		display: block;
	}

	.matter-card {
		background: transparent;
		border: none;
		border-bottom: 1px solid var(--border-primary);
		padding: 1.5rem 0;
		transition: padding-left 0.2s ease;
		cursor: pointer;
	}

	.matter-card-link:hover .matter-card {
		padding-left: 8px;
	}

	.matter-card-header {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-bottom: 1rem;
		flex-wrap: wrap;
	}

	.matter-file-badge {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		font-weight: 700;
		color: var(--badge-matter-text);
		background: var(--badge-matter-bg);
		border: 1.5px solid var(--badge-matter-border);
		padding: 0.35rem 0.75rem;
		border-radius: var(--radius-md);
	}

	.matter-type-label {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--civic-gray);
		background: var(--surface-secondary);
		padding: 0.3rem 0.65rem;
		border-radius: var(--radius-sm);
		text-transform: capitalize;
	}

	.appearances-badge {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 700;
		color: var(--badge-green-text);
		background: var(--badge-green-bg);
		border: 1px solid var(--badge-green-border);
		padding: 0.3rem 0.65rem;
		border-radius: var(--radius-sm);
	}

	.matter-card-title {
		font-family: var(--font-display);
		font-size: 1.2rem;
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1.3;
		margin: 0 0 0.75rem 0;
		letter-spacing: -0.01em;
	}

	.matter-card-topics {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}

	.matter-topic-tag {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		padding: 0.25rem 0.55rem;
		background: var(--topic-tag-bg);
		color: var(--topic-tag-text);
		border: 1px solid var(--topic-tag-border);
		border-radius: var(--radius-xs);
		font-weight: 500;
	}

	.matter-card-summary {
		font-family: var(--font-body);
		font-size: 0.95rem;
		line-height: 1.6;
		color: var(--text-secondary);
		margin-bottom: 1rem;
	}

	.matter-timeline-container {
		margin-top: 1rem;
	}

	.no-upcoming-cta {
		display: flex;
		align-items: center;
		gap: 1.25rem;
		padding: 1.25rem 1.5rem;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-md);
		margin: 1.25rem 0;
	}

	.no-upcoming-cta p {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 500;
		color: var(--text-primary);
		flex: 1;
	}

	.cta-button-inline {
		padding: 0.5rem 1rem;
		background: var(--civic-blue);
		color: white;
		border: none;
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		transition: all var(--transition-normal);
	}

	.cta-button-inline:hover {
		background: var(--civic-accent);
	}

	.request-jurisdiction-cta {
		margin-top: 2rem;
		padding: 1.5rem;
		background: var(--surface-secondary);
		border: 2px solid var(--civic-blue);
		border-radius: var(--radius-lg);
		text-align: center;
	}

	.cta-text {
		font-family: var(--font-mono);
		font-size: 1rem;
		font-weight: 600;
		color: var(--text-primary);
		margin: 0 0 0.75rem 0;
	}

	.cta-button {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		background: var(--civic-blue);
		color: white;
		border: none;
		border-radius: var(--radius-md);
		font-family: var(--font-mono);
		font-size: 0.95rem;
		font-weight: 500;
		cursor: pointer;
		transition: opacity var(--transition-normal);
	}

	.cta-button:hover {
		opacity: 0.9;
	}

	.cta-subtext {
		font-family: var(--font-body);
		font-size: 0.85rem;
		color: var(--text-tertiary);
		margin: 0.75rem 0 0 0;
	}

	.source-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1.5rem;
		gap: 1rem;
	}

	.follow-cta-link {
		background: none;
		border: none;
		padding: 0;
		font-family: var(--font-mono);
		font-size: 0.8rem;
		font-weight: 700;
		color: var(--civic-blue);
		cursor: pointer;
		white-space: nowrap;
		transition: all var(--transition-normal);
		text-decoration: underline;
		text-decoration-thickness: 2px;
		text-underline-offset: 3px;
	}

	.follow-cta-link:hover {
		color: var(--civic-accent);
		text-decoration-color: var(--civic-accent);
	}

	@media (max-width: 640px) {
		.container {
			width: 100%;
		}

		.compact-logo {
			right: 0.75rem;
		}

		.logo-icon {
			width: 40px;
			height: 40px;
			border-radius: var(--radius-md);
		}

		.jurisdiction-title {
			font-size: 1.5rem;
		}

		.jurisdiction-title-row {
			flex-wrap: wrap;
		}

		.council-link {
			font-size: 0.75rem;
		}

		.watch-jurisdiction-btn {
			font-size: 0.75rem;
		}

		.controls-row {
			flex-direction: column;
			align-items: stretch;
		}

		.view-toggle {
			width: 100%;
			justify-content: center;
		}

		.toggle-btn {
			flex: 1;
			padding: 0.6rem 1rem;
			font-size: 0.85rem;
		}

		.search-container {
			width: 100%;
		}

		.jurisdiction-search {
			width: 100%;
		}

		.matter-card {
			padding: 1rem;
		}

		.matter-card-title {
			font-size: 1rem;
		}

		.no-upcoming-cta {
			flex-direction: column;
			text-align: center;
		}

		.no-upcoming-cta p {
			font-size: 1rem;
		}
	}

	/* Meeting list & filter styles (extracted from app.css) */
	.meeting-list {
		display: flex;
		flex-direction: column;
		width: 100%;
	}

	.meetings-filter {
		margin-bottom: 1.5rem;
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex-wrap: wrap;
		gap: 1rem;
		width: 100%;
	}

	.meetings-section-title {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		margin: 0;
	}

	.toggle-past-btn {
		padding: 0.5rem 1rem;
		background: var(--surface-primary);
		color: var(--civic-blue);
		border: 2px solid var(--civic-blue);
		border-radius: var(--radius-sm);
		cursor: pointer;
		font-family: var(--font-mono);
		font-size: 0.9rem;
		transition: all var(--transition-normal);
	}

	.toggle-past-btn:hover {
		background: var(--civic-blue);
		color: var(--surface-primary);
	}

	.past-meetings-divider {
		margin: 2rem 0 0 0;
		padding-bottom: 0.75rem;
		border-bottom: 2px solid var(--text-primary);
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
	}
</style>
