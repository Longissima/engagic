<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { authState } from '$lib/stores/auth.svelte';
	import {
		getDashboard,
		addKeywordToDigest,
		removeKeywordFromDigest,
		addCityToDigest,
		removeCityFromDigest,
		requestCity,
		type Digest,
		type DigestMatch,
		type CityActivityItem
	} from '$lib/api/dashboard';
	import { apiClient } from '$lib/api/api-client';
	import { ApiError, isSearchSuccess, isSearchAmbiguous, type CityOption } from '$lib/api/types';
	import { generateMeetingSlug } from '$lib/utils/utils';
	import { toastStore } from '$lib/stores/toast.svelte';
	import Footer from '$lib/components/Footer.svelte';

	let loading = $state(true);
	let error = $state<string | null>(null);

	let stats = $state({
		active_digests: 0,
		total_matches: 0,
		matches_this_week: 0,
		cities_tracked: 0
	});

	let digests = $state<Digest[]>([]);
	let recentMatches = $state<DigestMatch[]>([]);
	let cityActivity = $state<CityActivityItem[]>([]);

	// Per-digest editing state
	let editingCityForDigest = $state<string | null>(null);
	let citySearchQuery = $state('');
	let citySearchResults = $state<CityOption[]>([]);
	let citySearchLoading = $state(false);
	let citySearchError = $state<string | null>(null);

	// Keyword input state (keyed by digest id)
	let newKeywords = $state<Record<string, string>>({});
	let keywordLoading = $state<Record<string, boolean>>({});
	let keywordError = $state<Record<string, string | null>>({});

	// City operation state
	let cityLoading = $state<Record<string, boolean>>({});
	let cityError = $state<Record<string, string | null>>({});

	onMount(async () => {
		if (!authState.isAuthenticated) {
			goto('/login');
			return;
		}

		try {
			const data = await getDashboard(authState.accessToken!);
			stats = data.stats;
			digests = data.digests;
			recentMatches = data.recent_matches;
			cityActivity = data.city_activity || [];
		} catch (err) {
			if (err instanceof ApiError && (err.statusCode === 401 || err.statusCode === 403)) {
				await authState.logout();
				goto('/login?expired=true');
				return;
			}
			error = err instanceof Error ? err.message : 'Failed to load dashboard';
		} finally {
			loading = false;
		}
	});

	async function handleLogout() {
		await authState.logout();
		goto('/');
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		});
	}

	function formatTime(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMins = Math.floor(diffMs / 60000);
		const diffHours = Math.floor(diffMins / 60);
		const diffDays = Math.floor(diffHours / 24);

		if (diffMins < 60) return `${diffMins}m ago`;
		if (diffHours < 24) return `${diffHours}h ago`;
		if (diffDays < 7) return `${diffDays}d ago`;
		return formatDate(dateStr);
	}

	function getMatchLink(match: DigestMatch): string {
		if (!match.meeting_id || !match.city_banana) return '';
		const meeting = {
			id: match.meeting_id,
			title: match.meeting_title || '',
			date: match.meeting_date || ''
		};
		return `/${match.city_banana}/${generateMeetingSlug(meeting as any)}`;
	}

	// Keyword management
	async function handleAddKeyword(digestId: string) {
		const keyword = newKeywords[digestId]?.trim();
		if (!keyword) return;

		const digest = digests.find((d) => d.id === digestId);
		if (!digest) return;
		if (digest.criteria.keywords.length >= 3) {
			keywordError[digestId] = 'Maximum 3 keywords allowed';
			setTimeout(() => (keywordError[digestId] = null), 3000);
			return;
		}

		keywordLoading[digestId] = true;
		keywordError[digestId] = null;

		try {
			const updated = await addKeywordToDigest(authState.accessToken!, digestId, keyword);
			digests = digests.map((d) => (d.id === digestId ? updated : d));
			newKeywords[digestId] = '';
		} catch (err) {
			keywordError[digestId] = err instanceof Error ? err.message : 'Failed to add keyword';
			setTimeout(() => (keywordError[digestId] = null), 3000);
		} finally {
			keywordLoading[digestId] = false;
		}
	}

	async function handleRemoveKeyword(digestId: string, keyword: string) {
		keywordLoading[digestId] = true;
		keywordError[digestId] = null;

		try {
			const updated = await removeKeywordFromDigest(authState.accessToken!, digestId, keyword);
			digests = digests.map((d) => (d.id === digestId ? updated : d));
		} catch (err) {
			keywordError[digestId] = err instanceof Error ? err.message : 'Failed to remove keyword';
			setTimeout(() => (keywordError[digestId] = null), 3000);
		} finally {
			keywordLoading[digestId] = false;
		}
	}

	// City management
	function startCityEdit(digestId: string) {
		editingCityForDigest = digestId;
		citySearchQuery = '';
		citySearchResults = [];
		citySearchError = null;
	}

	function cancelCityEdit() {
		editingCityForDigest = null;
		citySearchQuery = '';
		citySearchResults = [];
		citySearchError = null;
	}

	async function handleCitySearch() {
		if (!citySearchQuery.trim() || !editingCityForDigest) return;

		citySearchLoading = true;
		citySearchError = null;
		citySearchResults = [];

		try {
			const result = await apiClient.searchMeetings(citySearchQuery.trim());
			if (isSearchSuccess(result)) {
				// Direct match - add city immediately
				await handleAddCity(editingCityForDigest, result.banana);
			} else if (isSearchAmbiguous(result)) {
				citySearchResults = result.city_options;
			} else {
				citySearchError = 'City not found. You can request coverage below.';
			}
		} catch (err) {
			citySearchError = err instanceof Error ? err.message : 'Search failed';
		} finally {
			citySearchLoading = false;
		}
	}

	async function handleAddCity(digestId: string, cityBanana: string) {
		cityLoading[digestId] = true;
		cityError[digestId] = null;

		try {
			const updated = await addCityToDigest(authState.accessToken!, digestId, cityBanana);
			digests = digests.map((d) => (d.id === digestId ? updated : d));
			stats.cities_tracked = new Set(digests.reduce<string[]>((acc, d) => acc.concat(d.cities), [])).size;
			cancelCityEdit();
		} catch (err) {
			cityError[digestId] = err instanceof Error ? err.message : 'Failed to add city';
			setTimeout(() => (cityError[digestId] = null), 3000);
		} finally {
			cityLoading[digestId] = false;
		}
	}

	async function handleRemoveCity(digestId: string, cityBanana: string) {
		cityLoading[digestId] = true;
		cityError[digestId] = null;

		try {
			const updated = await removeCityFromDigest(authState.accessToken!, digestId, cityBanana);
			digests = digests.map((d) => (d.id === digestId ? updated : d));
			stats.cities_tracked = new Set(digests.reduce<string[]>((acc, d) => acc.concat(d.cities), [])).size;
		} catch (err) {
			cityError[digestId] = err instanceof Error ? err.message : 'Failed to remove city';
			setTimeout(() => (cityError[digestId] = null), 3000);
		} finally {
			cityLoading[digestId] = false;
		}
	}

	async function handleRequestCity() {
		if (!citySearchQuery.trim() || !editingCityForDigest) return;

		cityLoading[editingCityForDigest] = true;

		try {
			// Convert search query to banana format (rough approximation)
			const banana = citySearchQuery.trim().replace(/[^a-zA-Z]/g, '').toLowerCase();
			await requestCity(authState.accessToken!, banana);
			citySearchError = null;
			cancelCityEdit();
			toastStore.success('City requested — we\'ll notify you when coverage is added.');
		} catch (err) {
			citySearchError = err instanceof Error ? err.message : 'Failed to request city';
		} finally {
			if (editingCityForDigest) {
				cityLoading[editingCityForDigest] = false;
			}
		}
	}

	function handleKeywordKeydown(event: KeyboardEvent, digestId: string) {
		if (event.key === 'Enter') {
			event.preventDefault();
			handleAddKeyword(digestId);
		}
	}

	function handleCitySearchKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter') {
			event.preventDefault();
			handleCitySearch();
		} else if (event.key === 'Escape') {
			cancelCityEdit();
		}
	}

	// Unified card state
	let expandedDigest = $state<string | null>(null);

	function getMatchesForDigest(digestId: string): DigestMatch[] {
		return recentMatches.filter((m) => m.alert_id === digestId);
	}

	function isDigestExpanded(digest: Digest): boolean {
		if (expandedDigest === digest.id) return true;
		if (digest.cities.length === 0 || digest.criteria.keywords.length === 0) return true;
		return false;
	}

	function toggleDigestEdit(digestId: string) {
		if (expandedDigest === digestId) {
			expandedDigest = null;
			if (editingCityForDigest === digestId) cancelCityEdit();
		} else {
			expandedDigest = digestId;
		}
	}

	// City activity: happening items + upcoming meetings, deduplicated against keyword matches
	function getCityActivityForDigest(digest: Digest): CityActivityItem[] {
		const matchMeetingIds = new Set(
			getMatchesForDigest(digest.id).map((m) => m.meeting_id)
		);
		return cityActivity
			.filter((item) => digest.cities.includes(item.banana))
			.filter((item) => !matchMeetingIds.has(item.meeting_id));
	}

	function formatUpcomingDate(dateStr: string | null): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			weekday: 'short',
			month: 'short',
			day: 'numeric'
		});
	}
</script>

<svelte:head>
	<title>Dashboard - Engagic</title>
</svelte:head>

<div class="dashboard">
	{#if loading}
		<div class="loading">
			<div class="spinner"></div>
			<p>Loading your dashboard...</p>
		</div>
	{:else if error}
		<div class="error-banner" role="alert">
			<p>{error}</p>
		</div>
	{:else}
		<div class="account-meta anim-entry" style="animation-delay: 0ms;">
			<span class="user-email">{authState.user?.email}</span>
			<span class="meta-sep" aria-hidden="true">&middot;</span>
			<button onclick={handleLogout} class="btn-logout">Log Out</button>
		</div>

		<header class="header anim-entry" style="animation-delay: 60ms;">
			<h1>Your Civic Pulse</h1>
			{#if stats.total_matches > 0}
				<p class="stats-line">{stats.matches_this_week} matches this week</p>
			{/if}
		</header>

		{#if digests.length > 0}
			<div class="digest-list anim-entry" style="animation-delay: 140ms;">
				{#each digests as digest (digest.id)}
					{@const matches = getMatchesForDigest(digest.id)}
					{@const expanded = isDigestExpanded(digest)}
					{@const activity = getCityActivityForDigest(digest)}

					<div class="digest-card">
						<!-- Summary: name + frequency -->
						<div class="digest-summary">
							<h2 class="digest-name">{digest.name}</h2>
							<span class="digest-freq">{digest.frequency}</span>
						</div>

						{#if !expanded}
							<!-- Collapsed: cities · keywords · edit -->
							<div class="digest-watching">
								{#each digest.cities as city, i}
									{#if i > 0}<span class="watching-sep">,</span>{/if}
									<a href="/{city}" class="watching-city">{city}</a>
								{/each}
								{#if digest.cities.length > 0 && digest.criteria.keywords.length > 0}
									<span class="watching-sep">&middot;</span>
								{/if}
								{#each digest.criteria.keywords as keyword, i}
									{#if i > 0}<span class="watching-sep">,</span>{/if}
									<span class="watching-keyword">{keyword}</span>
								{/each}
								<button class="btn-edit" onclick={() => toggleDigestEdit(digest.id)}>edit</button>
							</div>
						{/if}

						{#if expanded}
							<!-- Expanded: full editing controls -->
							<div class="digest-edit">
								<!-- City editing -->
								<div class="edit-field">
									<span class="edit-label">City</span>
									{#if editingCityForDigest === digest.id}
										<div class="jurisdiction-edit">
											<div class="jurisdiction-search-row">
												<input
													type="text"
													bind:value={citySearchQuery}
													onkeydown={handleCitySearchKeydown}
													placeholder="Search for a city..."
													class="field-input"
													disabled={citySearchLoading}
												/>
												<button
													onclick={handleCitySearch}
													class="btn-action"
													disabled={citySearchLoading || !citySearchQuery.trim()}
												>
													{citySearchLoading ? '...' : 'Search'}
												</button>
												<button onclick={cancelCityEdit} class="btn-secondary">Cancel</button>
											</div>
											{#if citySearchResults.length > 0}
												<div class="jurisdiction-results">
													{#each citySearchResults as city}
														<button
															class="jurisdiction-result"
															onclick={() => handleAddCity(digest.id, city.banana)}
															disabled={cityLoading[digest.id]}
														>
															{city.city_name}, {city.state}
														</button>
													{/each}
												</div>
											{/if}
											{#if citySearchError}
												<div class="field-error">
													{citySearchError}
													{#if citySearchError.includes('not found')}
														<button onclick={handleRequestCity} class="btn-link">
															Request this city
														</button>
													{/if}
												</div>
											{/if}
										</div>
									{:else if digest.cities.length > 0}
										<span class="tag-row">
											{#each digest.cities as city}
												<span class="tag">
													<a href="/{city}" class="tag-link">{city}</a>
													<button
														class="tag-remove"
														onclick={() => handleRemoveCity(digest.id, city)}
														disabled={cityLoading[digest.id]}
														aria-label="Remove {city}"
													><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></button>
												</span>
											{/each}
											<button onclick={() => startCityEdit(digest.id)} class="btn-inline">
												Change
											</button>
										</span>
									{:else}
										<button onclick={() => startCityEdit(digest.id)} class="btn-inline">
											+ Add a city
										</button>
									{/if}
									{#if cityError[digest.id]}
										<span class="field-error">{cityError[digest.id]}</span>
									{/if}
								</div>

								<!-- Keyword editing -->
								<div class="edit-field">
									<span class="edit-label">Keywords</span>
									<div class="keywords-container">
										{#if digest.criteria.keywords.length > 0}
											<div class="tag-row">
												{#each digest.criteria.keywords as keyword}
													<span class="tag">
														{keyword}
														<button
															class="tag-remove"
															onclick={() => handleRemoveKeyword(digest.id, keyword)}
															disabled={keywordLoading[digest.id]}
															aria-label="Remove {keyword}"
														><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></button>
													</span>
												{/each}
											</div>
										{/if}
										{#if digest.criteria.keywords.length < 3}
											<div class="keyword-input-row">
												<input
													type="text"
													bind:value={newKeywords[digest.id]}
													onkeydown={(e) => handleKeywordKeydown(e, digest.id)}
													placeholder="e.g. housing, zoning, budget"
													maxlength="50"
													class="field-input field-input-sm"
													disabled={keywordLoading[digest.id]}
												/>
												<button
													onclick={() => handleAddKeyword(digest.id)}
													class="btn-action"
													disabled={keywordLoading[digest.id] || !newKeywords[digest.id]?.trim()}
												>
													{keywordLoading[digest.id] ? '...' : 'Add'}
												</button>
											</div>
										{:else}
											<span class="limit-note">3 of 3 keywords</span>
										{/if}
										{#if keywordError[digest.id]}
											<span class="field-error">{keywordError[digest.id]}</span>
										{/if}
									</div>
								</div>

								{#if expandedDigest === digest.id}
									<div class="edit-done">
										<button class="btn-edit" onclick={() => toggleDigestEdit(digest.id)}>done</button>
									</div>
								{/if}
							</div>
						{/if}

						<!-- Keyword matches -->
						{#if matches.length > 0}
							<div class="digest-matches">
								{#each matches as match (match.id)}
									{@const link = getMatchLink(match)}
									{#if link}
										<a href={link} class="match-card">
											<div class="match-head">
												<span class="match-city">{match.city_name || match.city_banana}</span>
												<span class="match-time">{formatTime(match.created_at)}</span>
											</div>
											<h3 class="match-title">{match.meeting_title}</h3>
											{#if match.item_title}
												<p class="match-detail">{match.item_title}</p>
											{/if}
											<div class="match-meta">
												{#if match.matched_criteria.keyword}
													<span class="matched-keyword">"{match.matched_criteria.keyword}"</span>
												{/if}
											</div>
										</a>
									{:else}
										<div class="match-card match-card-static">
											<div class="match-head">
												<span class="match-city">{match.city_name || match.city_banana}</span>
												<span class="match-time">{formatTime(match.created_at)}</span>
											</div>
											<h3 class="match-title">{match.meeting_title}</h3>
											{#if match.item_title}
												<p class="match-detail">{match.item_title}</p>
											{/if}
											<div class="match-meta">
												{#if match.matched_criteria.keyword}
													<span class="matched-keyword">"{match.matched_criteria.keyword}"</span>
												{/if}
											</div>
										</div>
									{/if}
								{/each}
							</div>
						{/if}

						<!-- City activity: happening items + upcoming meetings -->
						{#if matches.length > 0 && activity.length > 0}
							<div class="activity-divider">coming up</div>
						{/if}

						{#if activity.length > 0}
							<div class="digest-activity">
								{#each activity as item (item.meeting_id + (item.item_title || ''))}
									<a href="/{item.banana}/{generateMeetingSlug({id: item.meeting_id, title: item.meeting_title, date: item.meeting_date || ''} as any)}" class="activity-card">
										<div class="activity-head">
											<span class="activity-city">{item.banana}</span>
											<span class="activity-date">{formatUpcomingDate(item.meeting_date)}</span>
										</div>
										<h3 class="activity-title">{item.meeting_title}</h3>
										{#if item.item_title}
											<p class="activity-item">{item.item_title}</p>
										{/if}
										{#if item.reason}
											<p class="activity-reason">{item.reason}</p>
										{/if}
									</a>
								{/each}
							</div>
						{:else if matches.length === 0}
							<p class="no-activity">No upcoming activity yet.</p>
						{/if}
					</div>
				{/each}
			</div>
		{:else}
			<div class="empty-state anim-entry" style="animation-delay: 140ms;">
				<p class="empty-lead">You're not watching anything yet.</p>
				<p class="empty-body">
					Search for your <a href="/">city</a>, click <strong>Watch this city</strong>, and we'll track what matters to you.
				</p>
			</div>
		{/if}
	{/if}

	<Footer />
</div>

<style>
	.dashboard {
		max-width: 720px;
		padding: 4rem 1rem;
		min-height: 100vh;
		display: flex;
		flex-direction: column;
		margin: 0 auto;
	}

	.anim-entry {
		animation: fadeSlideUp 0.5s ease-out both;
	}

	/* ── Account meta ── */

	.account-meta {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-bottom: var(--space-sm);
	}

	.user-email {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-gray);
	}

	.meta-sep {
		color: var(--civic-gray);
		font-size: 0.65rem;
		opacity: 0.4;
	}

	.btn-logout {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-blue);
		background: none;
		border: none;
		cursor: pointer;
		padding: 0;
		transition: color var(--transition-fast);
	}

	.btn-logout:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	/* ── Header ── */

	.header {
		margin-bottom: var(--space-2xl);
	}

	h1 {
		font-family: var(--font-display);
		font-size: clamp(1.5rem, 4vw, 2rem);
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
		letter-spacing: -0.02em;
		line-height: 1.2;
	}

	.stats-line {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-gray);
		margin: var(--space-xs) 0 0 0;
	}

	/* ── Loading / Error ── */

	.loading {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 4rem 1rem;
		animation: fadeSlideUp 0.5s ease-out both;
	}

	.loading p {
		font-family: var(--font-display);
		font-size: 1.1rem;
		font-style: italic;
		color: var(--text-secondary);
		margin: 0;
	}

	.spinner {
		width: 28px;
		height: 28px;
		margin-bottom: 1rem;
		border: 2px solid var(--border-primary);
		border-top-color: var(--civic-blue);
		border-radius: 50%;
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	.error-banner {
		background: var(--alert-bg);
		border-left: 3px solid var(--alert-icon);
		border-radius: var(--radius-sm);
		padding: 1.25rem;
		color: var(--alert-text);
	}

	.error-banner p { margin: 0; }

	/* ── Digest cards ── */

	.digest-card {
		padding: var(--space-lg) 0;
		border-bottom: 1px solid var(--border-primary);
	}

	.digest-card:first-child {
		padding-top: 0;
	}

	.digest-summary {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}

	.digest-name {
		font-family: var(--font-display);
		font-size: 1.15rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
	}

	.digest-freq {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	/* ── Collapsed: watching line ── */

	.digest-watching {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		margin-top: 0.25rem;
		flex-wrap: wrap;
		font-family: var(--font-mono);
		font-size: 0.8125rem;
	}

	.watching-city {
		color: var(--civic-blue);
		text-decoration: none;
		transition: color var(--transition-fast);
	}

	.watching-city:hover {
		text-decoration: underline;
	}

	.watching-keyword {
		color: var(--text-secondary);
	}

	.watching-sep {
		color: var(--civic-gray);
		opacity: 0.5;
	}

	.btn-edit {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-blue);
		background: none;
		border: none;
		cursor: pointer;
		padding: 0;
		margin-left: auto;
		transition: color var(--transition-fast);
	}

	.btn-edit:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	/* ── Expanded: editing controls ── */

	.digest-edit {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
		margin-top: var(--space-sm);
		padding-left: var(--space-md);
		border-left: 2px solid var(--border-primary);
	}

	.edit-field {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		font-size: 0.875rem;
	}

	.edit-label {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--civic-gray);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.edit-done {
		display: flex;
		justify-content: flex-end;
	}

	/* ── Tags ── */

	.tag-row {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.tag {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.25rem 0.5rem;
		background: var(--badge-blue-bg);
		color: var(--badge-blue-text);
		border: 1px solid var(--badge-blue-border);
		border-radius: 2px;
		font-family: var(--font-mono);
		font-size: 0.8125rem;
	}

	.tag-link {
		color: inherit;
		text-decoration: none;
		transition: color var(--transition-fast);
	}

	.tag-link:hover {
		color: var(--civic-blue);
		text-decoration: underline;
	}

	.tag-remove {
		background: none;
		border: none;
		color: inherit;
		cursor: pointer;
		padding: 0 0.1rem;
		opacity: 0.45;
		transition: opacity var(--transition-fast);
		display: inline-flex;
		align-items: center;
		line-height: 1;
	}

	.tag-remove:hover { opacity: 1; }

	/* ── Form controls ── */

	.jurisdiction-edit { margin-top: 0.25rem; }

	.jurisdiction-search-row,
	.keyword-input-row {
		display: flex;
		gap: 0.5rem;
	}

	.jurisdiction-results {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		margin: 0.5rem 0;
	}

	.jurisdiction-result {
		text-align: left;
		padding: 0.5rem 0.75rem;
		background: transparent;
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-sm);
		color: var(--text-link);
		cursor: pointer;
		font-size: 0.875rem;
		font-family: var(--font-body);
		transition: all var(--transition-fast);
	}

	.jurisdiction-result:hover {
		background: var(--surface-secondary);
		border-color: var(--civic-blue);
	}

	.field-input {
		flex: 1;
		padding: 0.5rem 0.75rem;
		font-size: 0.875rem;
		font-family: var(--font-body);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-primary);
		transition: border-color var(--transition-fast);
	}

	.field-input-sm {
		max-width: 220px;
		padding: 0.375rem 0.5rem;
	}

	.field-input:focus {
		outline: none;
		border-color: var(--civic-blue);
	}

	.field-input::placeholder {
		color: var(--civic-gray);
		font-style: italic;
	}

	.btn-action {
		padding: 0.375rem 0.75rem;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 500;
		background: var(--civic-blue);
		color: white;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: background var(--transition-fast);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.btn-action:hover:not(:disabled) { background: var(--civic-accent); }
	.btn-action:disabled { opacity: 0.4; cursor: not-allowed; }

	.btn-secondary {
		padding: 0.375rem 0.75rem;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		background: transparent;
		color: var(--civic-gray);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: all var(--transition-fast);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.btn-secondary:hover {
		background: var(--surface-secondary);
		color: var(--text-primary);
	}

	.btn-inline {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--civic-blue);
		background: transparent;
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-sm);
		padding: 0.25rem 0.5rem;
		cursor: pointer;
		transition: all var(--transition-fast);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.btn-inline:hover {
		background: var(--surface-secondary);
		border-color: var(--civic-blue);
	}

	.btn-link {
		background: none;
		border: none;
		color: var(--civic-blue);
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
		font-size: inherit;
		padding: 0;
		margin-left: 0.5rem;
		transition: color var(--transition-fast);
	}

	.btn-link:hover { color: var(--civic-accent); }

	.keywords-container {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.limit-note {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--civic-gray);
	}

	.field-error {
		display: block;
		margin-top: 0.25rem;
		font-size: 0.8125rem;
		color: var(--civic-red);
	}

	/* ── Inline matches (happening-card pattern) ── */

	.digest-matches {
		margin-top: var(--space-md);
	}

	.match-card {
		display: block;
		padding: 0.75rem 0;
		border-bottom: 1px solid var(--border-primary);
		text-decoration: none;
		color: inherit;
		transition: padding-left 0.2s ease;
	}

	.match-card:hover { padding-left: 6px; }

	.match-card-static { cursor: default; }
	.match-card-static:hover { padding-left: 0; }

	.match-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.4rem;
	}

	.match-city {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--civic-blue);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.match-time {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.match-title {
		font-family: var(--font-display);
		font-size: 1.05rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0 0 0.25rem 0;
		line-height: 1.3;
	}

	.match-detail {
		font-size: 0.85rem;
		color: var(--text-secondary);
		margin: 0;
		line-height: 1.5;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.match-meta {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.5rem;
	}

	.matched-keyword {
		font-family: var(--font-mono);
		padding: 2px 8px;
		background: var(--badge-blue-bg);
		border-radius: 2px;
		font-size: 0.625rem;
		color: var(--badge-blue-text);
		font-weight: 500;
	}

	/* ── City activity (happening + upcoming meetings) ── */

	.activity-divider {
		font-family: var(--font-mono);
		font-size: 0.6rem;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: var(--civic-gray);
		opacity: 0.5;
		margin: var(--space-md) 0 var(--space-xs) 0;
	}

	.digest-activity {
		margin-top: var(--space-sm);
	}

	.activity-card {
		display: block;
		padding: 0.75rem 0;
		border-bottom: 1px solid var(--border-primary);
		text-decoration: none;
		color: inherit;
		transition: padding-left 0.2s ease;
	}

	.activity-card:hover { padding-left: 6px; }
	.activity-card:last-child { border-bottom: none; }

	.activity-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.4rem;
	}

	.activity-city {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--civic-blue);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.activity-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.activity-title {
		font-family: var(--font-display);
		font-size: 1.05rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0 0 0.25rem 0;
		line-height: 1.3;
	}

	.activity-item {
		font-size: 0.875rem;
		color: var(--text-primary);
		margin: 0 0 0.25rem 0;
		line-height: 1.4;
	}

	.activity-reason {
		font-size: 0.85rem;
		color: var(--text-secondary);
		margin: 0;
		line-height: 1.5;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	/* ── No activity ── */

	.no-activity {
		font-family: var(--font-body);
		font-size: 0.9rem;
		color: var(--civic-gray);
		margin: var(--space-md) 0 0 0;
		font-style: italic;
		line-height: 1.5;
	}

	/* ── Empty state (no digests) ── */

	.empty-state {
		padding: var(--space-lg) var(--space-md);
		border-left: 2px solid var(--border-primary);
	}

	.empty-lead {
		font-family: var(--font-display);
		font-size: 1.15rem;
		color: var(--text-primary);
		margin: 0 0 0.5rem 0;
		line-height: 1.4;
	}

	.empty-body {
		font-size: 1rem;
		color: var(--text-secondary);
		margin: 0;
		line-height: 1.7;
	}

	.empty-body a {
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 600;
		transition: color var(--transition-fast);
	}

	.empty-body a:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.empty-body strong {
		color: var(--text-primary);
	}

	/* ── Responsive ── */

	@media (max-width: 640px) {
		.dashboard {
			padding: 2rem 1rem;
		}

		.digest-summary {
			flex-direction: column;
			gap: 0.25rem;
		}

		.digest-edit {
			padding-left: var(--space-sm);
		}

		.jurisdiction-search-row {
			flex-wrap: wrap;
		}

		.field-input {
			width: 100%;
			flex: none;
		}

		.field-input-sm {
			max-width: none;
		}
	}
</style>
