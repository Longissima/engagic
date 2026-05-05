<script lang="ts">
	import { getStateMatters, getStateMeetings } from '$lib/api/index';
	import type { GetStateMattersResponse, GetStateMeetingsResponse, StateMatterSummary, StateMeeting, TopCityStats, GlobalHappeningItem, GlobalHappeningResponse } from '$lib/api/types';
	import { generateMeetingSlug } from '$lib/utils/utils';
	import { onMount } from 'svelte';

	const TOPIC_COLORS: Record<string, string> = {
		'Housing': 'var(--topic-housing)',
		'Transportation': 'var(--topic-transportation)',
		'Public Safety': 'var(--topic-public-safety)',
		'Budget': 'var(--topic-budget)',
		'Environment': 'var(--topic-environment)',
		'Zoning': 'var(--topic-zoning)',
		'Education': 'var(--topic-education)',
		'Infrastructure': 'var(--topic-infrastructure)',
		'Health': 'var(--topic-health)',
		'Business': 'var(--topic-business)',
		'Parks': 'var(--topic-parks)',
		'Utilities': 'var(--topic-utilities)',
		'Labor': 'var(--topic-labor)',
		'Technology': 'var(--topic-technology)',
		'Culture': 'var(--topic-culture)',
		'Governance': 'var(--topic-governance)',
	};

	function topicColor(topic: string): string {
		return TOPIC_COLORS[topic] || 'var(--topic-default)';
	}

	interface Props {
		stateCode: string;
		stateName?: string;
		initialMetrics?: GetStateMattersResponse;
		initialMeetings?: GetStateMeetingsResponse;
		initialHappening?: GlobalHappeningResponse | null;
	}

	let { stateCode, stateName, initialMetrics, initialMeetings, initialHappening }: Props = $props();

	let metrics = $state<GetStateMattersResponse | null>(initialMetrics || null);
	let meetings = $state<GetStateMeetingsResponse | null>(initialMeetings || null);
	let loading = $state(!initialMetrics);
	let meetingsLoading = $state(!initialMeetings);
	let error = $state('');
	let selectedTopic = $state<string | null>(null);
	let showAllMatters = $state(false);
	let showAllCities = $state(false);

	onMount(async () => {
		const promises: Promise<void>[] = [];

		if (!initialMetrics) {
			promises.push(
				(async () => {
					try {
						const result = await getStateMatters(stateCode);
						metrics = result;
					} catch (err) {
						error = err instanceof Error ? err.message : 'Failed to load state metrics';
					} finally {
						loading = false;
					}
				})()
			);
		}

		if (!initialMeetings) {
			promises.push(
				(async () => {
					try {
						const result = await getStateMeetings(stateCode);
						meetings = result;
					} catch (err) {
						console.error('Failed to load state meetings:', err);
					} finally {
						meetingsLoading = false;
					}
				})()
			);
		}

		await Promise.all(promises);
	});

	async function filterByTopic(topic: string) {
		selectedTopic = selectedTopic === topic ? null : topic;
		loading = true;
		try {
			const result = await getStateMatters(stateCode, selectedTopic || undefined);
			metrics = result;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to filter by topic';
		} finally {
			loading = false;
		}
	}

	// Derived: city bananas for this state (for filtering happening items)
	const stateCityBananas = $derived.by(() => {
		if (!metrics?.cities) return new Set<string>();
		return new Set(metrics.cities.map(c => c.banana));
	});

	// Derived: city name lookup by banana
	const cityNameByBanana = $derived.by(() => {
		if (!metrics?.cities) return new Map<string, string>();
		const map = new Map<string, string>();
		metrics.cities.forEach(c => map.set(c.banana, c.name));
		return map;
	});

	// Derived: happening items filtered to this state's cities
	const happeningItems = $derived.by(() => {
		if (!initialHappening?.items || stateCityBananas.size === 0) return [];
		return initialHappening.items
			.filter(item => stateCityBananas.has(item.banana))
			.slice(0, 6);
	});

	const topTopics = $derived.by(() => {
		if (!metrics?.topic_distribution) return [];
		return Object.entries(metrics.topic_distribution)
			.sort(([, a], [, b]) => (b as number) - (a as number))
			.slice(0, 12);
	});

	const topTopicsForFilter = $derived(topTopics.slice(0, 10));

	const totalTopicCount = $derived.by(() => {
		return topTopics.reduce((sum, [, count]) => sum + (count as number), 0);
	});

	const longestTrackedMatters = $derived.by(() => {
		if (!metrics?.matters) return [];
		return metrics.matters
			.filter((m: StateMatterSummary) => m.appearance_count > 1)
			.sort((a: StateMatterSummary, b: StateMatterSummary) => b.appearance_count - a.appearance_count)
			.slice(0, 6);
	});

	const mostActiveCities = $derived.by(() => {
		if (!metrics?.top_cities) return [];
		return metrics.top_cities.slice(0, 8);
	});

	const displayedMatters = $derived(showAllMatters ? longestTrackedMatters : longestTrackedMatters.slice(0, 4));
	const displayedCities = $derived(showAllCities ? mostActiveCities : mostActiveCities.slice(0, 8));

	// Filtered meetings by topic
	const filteredMeetings = $derived.by(() => {
		if (!meetings?.meetings) return [];
		if (!selectedTopic) return meetings.meetings;
		return meetings.meetings.filter(m => m.topics?.includes(selectedTopic!));
	});

	// Filtered happening items by topic
	const filteredHappening = $derived.by(() => {
		if (!selectedTopic) return happeningItems;
		// Happening items don't have topics directly, so show all when there's a topic filter
		return happeningItems;
	});

	function formatDate(dateStr: string): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		const now = new Date();
		const diffDays = Math.floor((now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24));

		if (diffDays === 0) return 'Today';
		if (diffDays === 1) return 'Yesterday';
		if (diffDays < 7) return `${diffDays}d ago`;
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
	}

	function formatMeetingDate(dateStr: string | null): string {
		if (!dateStr) return 'TBD';
		const date = new Date(dateStr);
		const now = new Date();
		const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
		const meetingDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
		const diffDays = Math.floor((meetingDay.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

		if (diffDays === 0) return 'Today';
		if (diffDays === 1) return 'Tomorrow';
		return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
	}

	function formatMeetingTime(dateStr: string | null): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		const hours = date.getHours();
		const minutes = date.getMinutes();
		if (hours === 0 && minutes === 0) return '';
		return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
	}

	function getWeekLabel(): string {
		const now = new Date();
		return `Week of ${now.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}`;
	}

	function coveragePct(n: number, total: number): string {
		if (!total) return '0%';
		return Math.round((n / total) * 100) + '% coverage';
	}
</script>

{#if loading}
	<div class="state-metrics state-loading">
		<div class="loading-text">Loading state-wide activity...</div>
	</div>
{:else if error}
	<div class="state-metrics state-error">
		<div class="error-text">{error}</div>
	</div>
{:else if metrics}
	<div class="state-metrics">
		<!-- Header -->
		<header class="briefing-header">
			<div class="briefing-label">State Briefing</div>
			<h1 class="briefing-title">{stateName || metrics.state}</h1>
			<p class="briefing-subtitle">
				{metrics.cities_count} cities tracked. {getWeekLabel()}.
			</p>
		</header>

		<!-- Coverage Strip -->
		{#if metrics.meeting_stats}
			<div class="coverage-strip">
				<div class="coverage-cell">
					<div class="coverage-value">{metrics.meeting_stats.total_meetings.toLocaleString()}</div>
					<div class="coverage-label">Meetings</div>
					<div class="coverage-sub">since tracking began</div>
				</div>
				<div class="coverage-cell">
					<div class="coverage-value">{metrics.meeting_stats.with_agendas.toLocaleString()}</div>
					<div class="coverage-label">With Agendas</div>
					<div class="coverage-sub">{coveragePct(metrics.meeting_stats.with_agendas, metrics.meeting_stats.total_meetings)}</div>
				</div>
				<div class="coverage-cell">
					<div class="coverage-value">{metrics.meeting_stats.with_summaries.toLocaleString()}</div>
					<div class="coverage-label">Summarized</div>
					<div class="coverage-sub">AI-analyzed</div>
				</div>
				<div class="coverage-cell">
					<div class="coverage-value">{metrics.total_matters.toLocaleString()}</div>
					<div class="coverage-label">Matters</div>
					<div class="coverage-sub">tracked across meetings</div>
				</div>
			</div>
		{/if}

		<!-- Topic Filter Strip -->
		{#if topTopicsForFilter.length > 0}
			<div class="filter-section">
				<div class="section-rule">
					<span>Filter by Topic</span>
					{#if selectedTopic}
						<button class="clear-filter-link" onclick={() => selectedTopic && filterByTopic(selectedTopic)}>
							Clear filter
						</button>
					{/if}
				</div>
				<div class="topic-pills">
					{#each topTopicsForFilter as [topic, count]}
						<button
							class="topic-pill"
							class:active={selectedTopic === topic}
							style="--pill-color: {topicColor(topic)}"
							onclick={() => filterByTopic(topic)}
						>
							{topic}
							<span class="pill-count">{count}</span>
						</button>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Happening This Week -->
		{#if filteredHappening.length > 0}
			<section class="happening-section">
				<div class="section-rule">
					Happening This Week
					{#if selectedTopic}
						<span class="section-rule-accent" style="color: {topicColor(selectedTopic)}">{selectedTopic}</span>
					{/if}
				</div>

				{#each filteredHappening as item}
					{@const cityName = cityNameByBanana.get(item.banana) || item.banana}
					<a href="/{item.banana}" class="happening-item hover-indent">
						<div class="happening-meta">
							<div class="happening-meta-left">
								<span class="happening-city">{cityName}</span>
								<span class="happening-body">{item.meeting_title || 'Meeting'}</span>
							</div>
							<span class="happening-date">{formatMeetingDate(item.meeting_date)}</span>
						</div>
						<h3 class="happening-headline">{item.item_title || 'Agenda Item'}</h3>
						{#if item.reason}
							<p class="happening-reason">{item.reason}</p>
						{/if}
					</a>
				{/each}
			</section>
		{/if}

		<!-- Upcoming Meetings (compact list) -->
		{#if filteredMeetings.length > 0}
			<section class="meetings-section">
				<div class="section-rule">
					<span>
						Upcoming Meetings
						{#if selectedTopic}
							<span class="section-rule-accent" style="color: {topicColor(selectedTopic)}">{selectedTopic}</span>
						{/if}
					</span>
					<span class="section-rule-count">{filteredMeetings.length} of {meetings?.total || filteredMeetings.length}</span>
				</div>

				<div class="meetings-compact-list">
					{#each filteredMeetings.slice(0, 12) as meeting (meeting.id)}
						{@const meetingSlug = generateMeetingSlug(meeting)}
						<a href="/{meeting.city_banana}/{meetingSlug}" class="meeting-row hover-indent">
							<div class="meeting-row-left">
								<span class="meeting-row-city">{meeting.city_name}</span>
								<span class="meeting-row-body">{meeting.title}</span>
								{#if meeting.topics && meeting.topics.length > 0}
									<span class="meeting-row-topics">
										{#each meeting.topics.slice(0, 2) as topic}
											<span class="meeting-row-topic" style="color: {topicColor(topic)}">{topic}</span>
										{/each}
									</span>
								{/if}
							</div>
							<div class="meeting-row-right">
								{#if meeting.participation?.email || meeting.participation?.virtual_url}
									<span class="meeting-row-comment">Comment</span>
								{/if}
								<span class="meeting-row-date">{formatMeetingDate(meeting.date)}</span>
							</div>
						</a>
					{/each}
				</div>

				{#if meetings && (meetings.total > 12 || filteredMeetings.length < meetings.total)}
					<div class="meetings-footer-text">
						{#if selectedTopic}
							Showing {Math.min(filteredMeetings.length, 12)} meetings tagged {selectedTopic}.
						{:else}
							Showing {Math.min(filteredMeetings.length, 12)} of {meetings.total}.
						{/if}
						<a href="/state/{stateCode.toLowerCase()}/meetings" class="view-all-link">View all meetings</a>
					</div>
				{/if}
			</section>
		{:else if meetingsLoading}
			<div class="loading-text">Loading upcoming meetings...</div>
		{:else if meetings}
			<section class="meetings-section">
				<div class="section-rule">Upcoming Meetings</div>
				<p class="empty-text">No upcoming meetings scheduled across {stateName || metrics.state}</p>
			</section>
		{/if}

		{#if metrics.total_matters > 0}
			<!-- Two-Column: Recurring Matters + Topic Distribution -->
			<div class="two-col">
				<!-- Recurring Matters -->
				<section class="matters-col">
					<div class="section-rule">
						Recurring Matters
						<span class="section-rule-sub">Items appearing across multiple meetings</span>
					</div>

					{#if displayedMatters.length === 0}
						<p class="empty-text">No recurring matters for this topic filter.</p>
					{/if}

					{#each displayedMatters as matter}
						<a href="/matter/{matter.id}" class="matter-item hover-indent">
							<div class="matter-item-meta">
								<div class="matter-item-meta-left">
									<span class="matter-item-city">{matter.city_name}</span>
									<span class="matter-item-count">{matter.appearance_count}x</span>
								</div>
								<span class="matter-item-date">Last seen {formatDate(matter.last_seen)}</span>
							</div>
							<h4 class="matter-item-title">{matter.title}</h4>
							{#if matter.canonical_summary}
								<p class="matter-item-summary">
									{matter.canonical_summary.substring(0, 180)}{matter.canonical_summary.length > 180 ? '...' : ''}
								</p>
							{/if}
							<div class="matter-item-footer">
								{#if matter.canonical_topics}
									{@const topics = typeof matter.canonical_topics === 'string' ? matter.canonical_topics.split(',').map((t: string) => t.trim()) : matter.canonical_topics}
									{#each (topics as string[]).slice(0, 2) as topic}
										<span class="matter-item-topic" style="color: {topicColor(topic)}">{topic}</span>
									{/each}
								{/if}
							</div>
						</a>
					{/each}

					{#if longestTrackedMatters.length > 4}
						<button class="show-more-link" onclick={() => showAllMatters = !showAllMatters}>
							{showAllMatters ? 'Show fewer' : `Show all ${longestTrackedMatters.length} matters`}
						</button>
					{/if}
				</section>

				<!-- Topic Distribution -->
				<section class="topics-col">
					<div class="section-rule">Topics</div>
					{#each topTopics as [topic, count]}
						{@const dimmed = selectedTopic && selectedTopic !== topic}
						<button
							class="topic-bar-row"
							class:dimmed
							onclick={() => filterByTopic(topic as string)}
						>
							<div class="topic-bar-header">
								<span class="topic-bar-name" style="color: {topicColor(topic as string)}">{topic}</span>
								<span class="topic-bar-count">{count}</span>
							</div>
							<div class="topic-bar-track">
								<div
									class="topic-bar-fill"
									style="width: {totalTopicCount ? ((count as number) / totalTopicCount) * 100 * 5 : 0}%; background: {topicColor(topic as string)}"
								></div>
							</div>
						</button>
					{/each}
				</section>
			</div>

			<!-- City Grid -->
			{#if displayedCities.length > 0}
				<section class="cities-section">
					<div class="section-rule">Cities</div>
					<div class="cities-grid">
						{#each displayedCities as city}
							<a href="/{city.banana}" class="jurisdiction-cell">
								<div class="jurisdiction-cell-name">{city.name}</div>
								<div class="jurisdiction-cell-stats">{city.matter_count} matters · {city.meeting_count} meetings</div>
							</a>
						{/each}
					</div>
					{#if mostActiveCities.length > 8}
						<button class="show-more-link" onclick={() => showAllCities = !showAllCities}>
							{showAllCities ? 'Show fewer' : `Show all ${mostActiveCities.length} cities`}
						</button>
					{/if}
				</section>
			{/if}
		{:else}
			<div class="no-matters-message">
				<p>No recurring legislative matters found for this state.</p>
				<p class="explanation">We only track matters that appear in multiple meetings (2+ appearances).</p>
			</div>
		{/if}
	</div>
{/if}

<style>
	.state-metrics {
		background: transparent;
		border: none;
		padding: 0;
		margin: 0;
	}

	.state-loading,
	.state-error {
		text-align: center;
		padding: 3rem;
	}

	.loading-text {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--civic-gray);
		text-align: center;
		padding: 1rem 0;
	}

	.error-text {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--civic-red);
	}

	/* Header */
	.briefing-header {
		margin-bottom: 2.5rem;
	}

	.briefing-label {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		margin-bottom: 0.5rem;
	}

	.briefing-title {
		font-family: var(--font-display);
		font-size: clamp(2.25rem, 5vw, 3.25rem);
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1.05;
		margin: 0;
		letter-spacing: -0.02em;
	}

	.briefing-subtitle {
		font-family: var(--font-body);
		font-size: 0.875rem;
		color: var(--civic-gray);
		margin-top: 0.625rem;
		line-height: 1.5;
	}

	/* Coverage Strip */
	.coverage-strip {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 1px;
		background: var(--border-primary);
		margin-bottom: 3.5rem;
	}

	.coverage-cell {
		background: var(--surface-primary);
		padding: 1.25rem 1rem;
	}

	.coverage-value {
		font-family: var(--font-display);
		font-size: 1.75rem;
		color: var(--text-primary);
		line-height: 1;
	}

	.coverage-label {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin-top: 0.375rem;
	}

	.coverage-sub {
		font-family: var(--font-body);
		font-size: 0.7rem;
		color: var(--civic-gray);
		margin-top: 0.125rem;
	}

	/* Section Rule — matches city page .section-title / .matters-title pattern */
	.section-rule {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		padding-bottom: 0.5rem;
		border-bottom: 2px solid var(--text-primary);
		margin-bottom: 1rem;
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.section-rule-accent {
		font-weight: 400;
		margin-left: 0.5rem;
	}

	.section-rule-count {
		font-weight: 400;
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.section-rule-sub {
		font-weight: 400;
		font-size: 0.625rem;
		color: var(--civic-gray);
		margin-left: 0.375rem;
	}

	.clear-filter-link {
		background: none;
		border: none;
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--civic-blue);
		cursor: pointer;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.clear-filter-link:hover {
		color: var(--civic-accent);
	}

	/* Topic Pills */
	.filter-section {
		margin-bottom: 3rem;
	}

	.topic-pills {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
	}

	.topic-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.3rem 0.75rem;
		font-size: 0.7rem;
		font-family: var(--font-body);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--pill-color);
		background: var(--topic-tag-bg);
		border: 1.5px solid var(--topic-tag-border);
		border-radius: var(--radius-xs);
		cursor: pointer;
		transition: all var(--transition-normal);
		white-space: nowrap;
	}

	.topic-pill:hover {
		border-color: var(--border-hover);
	}

	.topic-pill.active {
		color: var(--surface-primary);
		background: var(--pill-color);
		border-color: var(--pill-color);
	}

	.pill-count {
		font-size: 0.625rem;
		opacity: 0.6;
		font-weight: 400;
	}

	.topic-pill.active .pill-count {
		opacity: 0.8;
	}

	/* Hover indent — matches HappeningSection / city page meeting-item pattern */
	.hover-indent {
		transition: padding-left 0.2s ease;
	}

	.hover-indent:hover {
		padding-left: 6px;
	}

	/* Happening Section */
	.happening-section {
		margin-bottom: 4rem;
	}

	.happening-item {
		display: block;
		padding: 1.25rem 0;
		border-bottom: 1px solid var(--border-primary);
		cursor: pointer;
		text-decoration: none;
		color: inherit;
	}

	.happening-meta {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 0.25rem;
	}

	.happening-meta-left {
		display: flex;
		align-items: center;
		gap: 0.625rem;
	}

	.happening-city {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 700;
		color: var(--civic-blue);
	}

	.happening-body {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
		letter-spacing: 0.03em;
	}

	.happening-date {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-gray);
		white-space: nowrap;
		margin-left: 1rem;
	}

	.happening-headline {
		font-family: var(--font-display);
		font-size: 1.3rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0.375rem 0 0.5rem;
		line-height: 1.25;
	}

	.happening-reason {
		font-family: var(--font-body);
		font-size: 0.85rem;
		line-height: 1.6;
		color: var(--text-secondary);
		margin: 0;
	}

	/* Meetings Compact List */
	.meetings-section {
		margin-bottom: 4rem;
	}

	.meetings-compact-list {
		display: grid;
		gap: 0;
	}

	.meeting-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.75rem 0;
		border-bottom: 1px solid var(--border-primary);
		cursor: pointer;
		text-decoration: none;
		color: inherit;
	}

	.meeting-row-left {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex: 1;
		min-width: 0;
	}

	.meeting-row-city {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 700;
		color: var(--civic-blue);
		width: 6.25rem;
		flex-shrink: 0;
	}

	.meeting-row-body {
		font-family: var(--font-body);
		font-size: 0.8rem;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.meeting-row-topics {
		display: flex;
		gap: 0.25rem;
		flex-shrink: 0;
	}

	.meeting-row-topic {
		font-size: 0.55rem;
		font-family: var(--font-body);
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.meeting-row-right {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-shrink: 0;
		margin-left: 0.75rem;
	}

	.meeting-row-comment {
		font-size: 0.55rem;
		font-family: var(--font-body);
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--civic-blue);
	}

	.meeting-row-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
		white-space: nowrap;
	}

	.meetings-footer-text {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--civic-gray);
		padding: 0.875rem 0;
	}

	.view-all-link {
		color: var(--civic-blue);
		cursor: pointer;
		font-weight: 600;
		text-decoration: none;
	}

	.view-all-link:hover {
		text-decoration: underline;
	}

	/* Two Column Layout */
	.two-col {
		display: grid;
		grid-template-columns: 1.3fr 0.7fr;
		gap: 3rem;
		margin-bottom: 4rem;
	}

	/* Recurring Matters */
	.matter-item {
		display: block;
		padding: 1rem 0;
		border-bottom: 1px solid var(--border-primary);
		cursor: pointer;
		text-decoration: none;
		color: inherit;
	}

	.matter-item-meta {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 0.25rem;
	}

	.matter-item-meta-left {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.matter-item-city {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		font-weight: 700;
		color: var(--civic-blue);
	}

	.matter-item-count {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		color: var(--civic-gray);
		background: var(--surface-secondary);
		padding: 0.0625rem 0.375rem;
		border-radius: var(--radius-xs);
	}

	.matter-item-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.matter-item-title {
		font-family: var(--font-display);
		font-size: 1.05rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0.25rem 0 0.375rem;
		line-height: 1.3;
	}

	.matter-item-summary {
		font-family: var(--font-body);
		font-size: 0.8rem;
		line-height: 1.55;
		color: var(--text-secondary);
		margin: 0;
	}

	.matter-item-footer {
		margin-top: 0.375rem;
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}

	.matter-item-topic {
		font-size: 0.625rem;
		font-family: var(--font-body);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}

	.show-more-link {
		background: none;
		border: none;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--civic-blue);
		cursor: pointer;
		padding: 0.875rem 0;
		letter-spacing: 0.03em;
	}

	.show-more-link:hover {
		color: var(--civic-accent);
	}

	/* Topic Distribution Sidebar */
	.topic-bar-row {
		display: block;
		width: 100%;
		padding: 0.375rem 0;
		cursor: pointer;
		transition: opacity 0.2s;
		background: none;
		border: none;
		text-align: left;
	}

	.topic-bar-row.dimmed {
		opacity: 0.35;
	}

	.topic-bar-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}

	.topic-bar-name {
		font-family: var(--font-body);
		font-size: 0.75rem;
		font-weight: 600;
	}

	.topic-bar-count {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	.topic-bar-track {
		height: 3px;
		background: var(--border-primary);
		border-radius: var(--radius-xs);
		overflow: hidden;
		margin-top: 0.25rem;
	}

	.topic-bar-fill {
		height: 100%;
		border-radius: var(--radius-xs);
		transition: width 0.6s ease;
	}

	/* City Grid */
	.cities-section {
		margin-bottom: 4rem;
	}

	.cities-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 1px;
		background: var(--border-primary);
	}

	.jurisdiction-cell {
		background: var(--surface-primary);
		padding: 1.125rem 1rem;
		cursor: pointer;
		transition: background var(--transition-fast);
		text-decoration: none;
		color: inherit;
	}

	.jurisdiction-cell:hover {
		background: var(--surface-secondary);
	}

	.jurisdiction-cell-name {
		font-family: var(--font-display);
		font-size: 1.125rem;
		color: var(--text-primary);
		margin-bottom: 0.375rem;
	}

	.jurisdiction-cell-stats {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--civic-gray);
	}

	/* Empty/no-matters states — matches city page empty-state pattern */
	.empty-text {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--civic-gray);
		padding: 0.75rem 0;
	}

	.no-matters-message {
		margin-top: 2rem;
		padding: 2rem;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		text-align: center;
	}

	.no-matters-message p {
		margin: 0 0 0.5rem 0;
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--text-primary);
	}

	.no-matters-message .explanation {
		font-size: 0.8rem;
		color: var(--civic-gray);
	}

	/* Responsive */
	@media (max-width: 800px) {
		.two-col {
			grid-template-columns: 1fr;
		}

		.cities-grid {
			grid-template-columns: 1fr;
		}
	}

	@media (min-width: 801px) and (max-width: 1000px) {
		.cities-grid {
			grid-template-columns: 1fr 1fr;
		}
	}

	@media (max-width: 640px) {
		.coverage-strip {
			grid-template-columns: repeat(2, 1fr);
		}

		.briefing-title {
			font-size: clamp(1.75rem, 8vw, 2.25rem);
		}

		.happening-headline {
			font-size: 1.125rem;
		}

		.happening-meta {
			flex-direction: column;
			gap: 0.25rem;
		}

		.happening-date {
			margin-left: 0;
		}

		.meeting-row {
			flex-direction: column;
			align-items: flex-start;
			gap: 0.375rem;
		}

		.meeting-row-left {
			flex-wrap: wrap;
		}

		.meeting-row-city {
			width: auto;
		}

		.meeting-row-right {
			margin-left: 0;
		}

		.meeting-row-topics {
			display: none;
		}

		.matter-item-meta {
			flex-direction: column;
			gap: 0.25rem;
		}
	}
</style>
