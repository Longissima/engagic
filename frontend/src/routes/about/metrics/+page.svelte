<script lang="ts">
	import type { PageData } from './$types';
	import type { CityCoverageResponse, CoverageType, JurisdictionClass } from '$lib/api/types';
	import { apiClient } from '$lib/api/api-client';
	import SeoHead from '$lib/components/SeoHead.svelte';

	let { data }: { data: PageData } = $props();
	let activeView: 'overview' | 'coverage' = $state('overview');

	// Coverage is reported per class (jurisdictions.type), against the Census of
	// Governments count of that class; kinds are the detail beneath a class.
	const CLASS_ORDER: JurisdictionClass[] = ['city', 'county', 'school_district', 'special_district', 'state'];
	const CLASS_LABELS: Record<JurisdictionClass, string> = {
		city: 'Cities and towns',
		county: 'Counties',
		school_district: 'School districts',
		special_district: 'Special districts',
		state: 'State bodies'
	};
	const coverage = $derived(data.platformMetrics?.coverage ?? null);
	const classRows = $derived(
		[...(coverage?.by_class ?? [])].sort((a, b) => CLASS_ORDER.indexOf(a.type) - CLASS_ORDER.indexOf(b.type))
	);
	const freshTotal = $derived(classRows.reduce((sum, r) => sum + r.fresh, 0));
	const timeliness = $derived(coverage?.timeliness ?? null);
	let cityCoverage = $state<CityCoverageResponse | null>(null);
	let coverageLoading = $state(false);
	let coverageError = $state(false);

	async function loadCoverage() {
		if (cityCoverage || coverageLoading) return;

		coverageLoading = true;
		coverageError = false;
		try {
			cityCoverage = await apiClient.getCityCoverage();
		} catch (error) {
			console.error('Failed to load jurisdiction coverage:', error);
			coverageError = true;
		} finally {
			coverageLoading = false;
		}
	}

	function showCoverage() {
		activeView = 'coverage';
		void loadCoverage();
	}

	function formatNumber(num: number): string {
		if (num >= 1000000) {
			return (num / 1000000).toFixed(1) + 'M';
		} else if (num >= 1000) {
			return (num / 1000).toFixed(1) + 'K';
		}
		return num.toLocaleString();
	}

	function formatBytes(bytes: number): string {
		const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
		const unit = bytes > 0 ? Math.min(Math.floor(Math.log10(bytes) / 3), units.length - 1) : 0;
		return `${(bytes / 1000 ** unit).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]}`;
	}

	function percent(part: number, whole: number): string {
		if (!whole) return '-';
		const value = (part / whole) * 100;
		return `${value < 10 ? value.toFixed(1) : Math.round(value)}%`;
	}

	function formatGrowth(num: number): string {
		if (num >= 1000) {
			return '+' + (num / 1000).toFixed(1) + 'K this month';
		}
		return '+' + num.toLocaleString() + ' this month';
	}

	function formatPop(num: number): string {
		if (num >= 1000000) {
			return (num / 1000000).toFixed(2) + 'M';
		} else if (num >= 1000) {
			return Math.round(num / 1000).toLocaleString() + 'K';
		}
		return num.toLocaleString();
	}

	function coverageLabel(type: CoverageType): string {
		switch (type) {
			case 'matter': return 'Matter-level';
			case 'item': return 'Item-level';
			case 'monolithic': return 'Meeting-level';
			case 'synced': return 'Synced';
			default: return 'Pending';
		}
	}

	function coverageClass(type: CoverageType): string {
		switch (type) {
			case 'matter': return 'coverage-matter';
			case 'item': return 'coverage-item';
			case 'monolithic': return 'coverage-monolithic';
			case 'synced': return 'coverage-synced';
			default: return 'coverage-pending';
		}
	}

	function sparklinePath(values: number[], width: number = 80, height: number = 24): string {
		if (!values || values.length < 2) return '';
		const max = Math.max(...values);
		const min = Math.min(...values);
		const range = max - min || 1;
		const step = width / (values.length - 1);
		return values
			.map((v, i) => {
				const x = i * step;
				const y = height - ((v - min) / range) * (height - 4) - 2;
				return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}
</script>

<SeoHead
	title="Impact Metrics - engagic"
	description="Real-time metrics on Engagic's coverage, processing, and civic engagement impact"
	url="https://engagic.org/about/metrics"
/>

<article class="metrics-container">
	<div class="view-toggle">
		<button
			class="toggle-btn"
			class:active={activeView === 'overview'}
			onclick={() => activeView = 'overview'}
		>
			Overview
		</button>
		<button
			class="toggle-btn"
			class:active={activeView === 'coverage'}
			onclick={showCoverage}
		>
			Jurisdiction Coverage
		</button>
	</div>

	{#if activeView === 'coverage'}
		{#if cityCoverage}
			<section class="metrics-section">
				<h1 class="primary-heading">Jurisdiction Coverage</h1>
				<p class="section-desc">All jurisdictions with active coverage — cities, counties, school districts, and special-purpose bodies — sorted by population. Coverage depth indicates the granularity of legislative tracking.</p>

				<div class="coverage-summary">
					<div class="summary-item">
						<span class="summary-count">{cityCoverage.summary.matter}</span>
						<span class="summary-label coverage-matter">Matter-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{cityCoverage.summary.item}</span>
						<span class="summary-label coverage-item">Item-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{cityCoverage.summary.monolithic}</span>
						<span class="summary-label coverage-monolithic">Meeting-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{cityCoverage.summary.synced}</span>
						<span class="summary-label coverage-synced">Synced</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{cityCoverage.summary.total}</span>
						<span class="summary-label">Total</span>
					</div>
				</div>

				{#if cityCoverage.summary.by_type}
					{@const bt = cityCoverage.summary.by_type}
					<p class="coverage-breakdown">
						{CLASS_ORDER.filter((c) => bt[c]).map((c) => `${CLASS_LABELS[c]} ${bt[c].toLocaleString()}`).join(' · ')}
					</p>
				{/if}

				<div class="jurisdiction-table-container">
					<table class="jurisdiction-table">
						<thead>
							<tr>
								<th class="col-jurisdiction">Jurisdiction</th>
								<th class="col-type">Type</th>
								<th class="col-coverage">Coverage</th>
								<th class="col-count">Count</th>
								<th class="col-pop">Population</th>
							</tr>
						</thead>
						<tbody>
							{#each cityCoverage.cities as city}
								<tr>
									<td class="col-jurisdiction">{city.name}, {city.state}</td>
									<td class="col-type">{city.kind_label}</td>
									<td class="col-coverage">
										<span class="coverage-badge {coverageClass(city.coverage_type)}">
											{coverageLabel(city.coverage_type)}
										</span>
									</td>
									<td class="col-count">{city.summary_count.toLocaleString()}</td>
									<td class="col-pop">{formatPop(city.population)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>
		{:else if coverageLoading}
			<div class="loading-container">Loading coverage data...</div>
		{:else if coverageError}
			<div class="loading-container error-container">
				<span>Coverage data is temporarily unavailable.</span>
				<button class="toggle-btn" onclick={loadCoverage}>Try again</button>
			</div>
		{/if}
	{:else}
		{#if classRows.length}
			<section class="metrics-section">
				<h1 class="primary-heading">Coverage</h1>
				<p class="section-desc">
					Jurisdictions with at least one summary, against how many of each the Census of Governments counts
					nationally. Onboarded includes bodies we sync but have not summarized yet.
				</p>
				<div class="jurisdiction-table-container class-table-container">
					<table class="jurisdiction-table">
						<thead>
							<tr>
								<th class="col-jurisdiction">Class</th>
								<th class="col-count">Summarized</th>
								<th class="col-count">Onboarded</th>
								<th class="col-count">Nationally</th>
								<th class="col-count">Share</th>
							</tr>
						</thead>
						<tbody>
							{#each classRows as row (row.type)}
								<tr>
									<td class="col-jurisdiction">{CLASS_LABELS[row.type]}</td>
									<td class="col-count">{row.live.toLocaleString()}</td>
									<td class="col-count">{row.total.toLocaleString()}</td>
									<td class="col-count">{row.census ? row.census.toLocaleString() : '-'}</td>
									<td class="col-count">{row.census ? percent(row.live, row.census) : '-'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>

				{#if coverage?.top_kinds.length}
					<div class="kinds">
						<h2 class="kinds-heading">Beyond city hall</h2>
						<ol class="kind-list">
							{#each coverage.top_kinds as k (k.kind)}
								<li class="kind-row">
									<span class="kind-name">{k.label}</span>
									<span class="kind-count">{k.total.toLocaleString()}</span>
									<span class="kind-live">{k.live.toLocaleString()} summarized</span>
								</li>
							{/each}
						</ol>
					</div>
				{/if}
			</section>
		{/if}

		{#if timeliness?.meetings}
			<section class="metrics-section">
				<h2 class="primary-heading">In Time to Act</h2>
				<p class="section-desc">Meetings held in the last 60 days that we saw before they happened.</p>
				<div class="cards-grid">
					<div class="stats-card highlight-card">
						<div class="number-primary">{percent(timeliness.summarized_before, timeliness.meetings)}</div>
						<div class="stat-title">Summarized Beforehand</div>
						<div class="stat-description">
							{timeliness.summarized_before.toLocaleString()} of {timeliness.meetings.toLocaleString()} meetings had a summary before they began
						</div>
					</div>
					<div class="stats-card">
						<div class="number-primary">{timeliness.median_days_notice} days</div>
						<div class="stat-title">Median Notice</div>
						<div class="stat-description">From first seeing a meeting to the meeting itself</div>
					</div>
					<div class="stats-card">
						<div class="number-primary">{timeliness.median_hours_to_summary} h</div>
						<div class="stat-title">Median Time to Summary</div>
						<div class="stat-description">From first seeing a meeting to its first summary</div>
					</div>
					<div class="stats-card">
						<div class="number-primary">{formatNumber(freshTotal)}</div>
						<div class="stat-title">Current Jurisdictions</div>
						<div class="stat-description">Summarized a meeting held in the last 30 days</div>
					</div>
				</div>
			</section>
		{/if}

		{#if data.platformMetrics}
			{@const pm = data.platformMetrics}
			<section class="metrics-section">
				<h2 class="primary-heading">The Record</h2>
				<div class="cards-grid">
					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.content.meetings)}</div>
						<div class="stat-title">Meetings Tracked</div>
						{#if pm.trends.meetings}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(pm.trends.meetings)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
						{/if}
						<div class="stat-description">{formatNumber(pm.processing.summarized_meetings)} summarized · {formatGrowth(pm.growth.meetings_30d)}</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.content.agenda_items)}</div>
						<div class="stat-title">Agenda Items</div>
						{#if pm.trends.items}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(pm.trends.items)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
						{/if}
						<div class="stat-description">{formatGrowth(pm.growth.items_30d)}</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.content.matters)}</div>
						<div class="stat-title">Legislative Matters</div>
						{#if pm.trends.matters}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(pm.trends.matters)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
						{/if}
						<div class="stat-description">Followed across {formatNumber(pm.content.matter_appearances)} appearances · {formatGrowth(pm.growth.matters_30d)}</div>
					</div>

					<div class="stats-card highlight-card">
						<div class="number-primary">{formatNumber(data.analytics?.real_metrics.unique_item_summaries ?? pm.processing.summarized_items)}</div>
						<div class="stat-title">Unique Summaries</div>
						<div class="stat-description">A recurring matter counts once · {pm.processing.item_summary_rate}% of analyzed items summarized</div>
					</div>
				</div>
			</section>

			<section class="metrics-section">
				<h2 class="primary-heading">Accountability</h2>
				<div class="cards-grid">
					<div class="stats-card highlight-card">
						<div class="number-primary">{formatNumber(pm.accountability.votes)}</div>
						<div class="stat-title">Ballots Recorded</div>
						<div class="stat-description">{formatNumber(pm.accountability.votes_with_receipt)} with source quotes ({pm.accountability.vote_receipt_rate}%)</div>
						{#if pm.trends.votes}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(pm.trends.votes)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
							<div class="stat-description">{formatGrowth(pm.growth.votes_30d)}</div>
						{/if}
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.accountability.motions)}</div>
						<div class="stat-title">Motions Recorded</div>
						<div class="stat-description">{formatNumber(pm.accountability.divided_motions)} had both yes and no votes</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.accountability.meetings_with_votes)}</div>
						<div class="stat-title">Meetings with Votes</div>
						<div class="stat-description">Meetings with recorded roll calls</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.accountability.cities_with_votes)}</div>
						<div class="stat-title">Jurisdictions with Vote Data</div>
						<div class="stat-description">{formatNumber(pm.accountability.officials_with_votes)} officials on the record</div>
					</div>
				</div>
			</section>

			<section class="metrics-section">
				<h2 class="primary-heading">Who Decides</h2>
				<div class="cards-grid">
					<a href="/council-members" class="stats-card stats-card-link">
						<div class="number-primary">{formatNumber(pm.civic_infrastructure.council_members)}</div>
						<div class="stat-title">Officials</div>
						<div class="stat-description">Elected and appointed members tracked</div>
						<span class="card-arrow">View by city</span>
					</a>

					<a href="/committees" class="stats-card stats-card-link">
						<div class="number-primary">{formatNumber(pm.civic_infrastructure.committees)}</div>
						<div class="stat-title">Committees</div>
						<div class="stat-description">Councils, boards, and commissions monitored</div>
						<span class="card-arrow">View by city</span>
					</a>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.civic_infrastructure.committee_assignments)}</div>
						<div class="stat-title">Committee Assignments</div>
						<div class="stat-description">Who sits on which body</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(pm.accountability.sponsorships)}</div>
						<div class="stat-title">Sponsorships</div>
						<div class="stat-description">Who authored which legislation</div>
					</div>
				</div>
			</section>

			{#if pm.corpus}
				<section class="metrics-section">
					<h2 class="primary-heading">Source Documents</h2>
					<div class="cards-grid">
						<div class="stats-card">
							<div class="number-primary">{formatNumber(pm.corpus.documents)}</div>
							<div class="stat-title">Unique Documents</div>
							<div class="stat-description">
								Counted once however often cited · {formatNumber(pm.corpus.ocr_documents)} needed OCR
							</div>
						</div>
						<div class="stats-card">
							<div class="number-primary">{formatNumber(pm.corpus.pages)}</div>
							<div class="stat-title">Document Pages</div>
							<div class="stat-description">{formatNumber(pm.corpus.ocr_pages)} read by OCR</div>
						</div>
						<div class="stats-card">
							<div class="number-primary">{formatNumber(pm.civic_infrastructure.minutes_documents)}</div>
							<div class="stat-title">Minutes Parsed</div>
							<div class="stat-description">Read for roll calls and outcomes</div>
						</div>
						<div class="stats-card">
							<div class="number-primary">{formatBytes(pm.corpus.archived_bytes)}</div>
							<div class="stat-title">Archived Originals</div>
							<div class="stat-description">Kept even after a city takes them down</div>
						</div>
					</div>
				</section>
			{/if}
		{/if}

		{#if !data.analytics && !data.platformMetrics}
			<div class="loading-container error-container">
				<span>Metrics are temporarily unavailable.</span>
				<a class="toggle-btn" href="/about/metrics">Try again</a>
			</div>
		{/if}
	{/if}
</article>

<style>
	.metrics-container {
		display: flex;
		flex-direction: column;
		gap: var(--space-3xl);
		padding-bottom: var(--space-3xl);
		color: var(--text-primary);
	}

	.metrics-section {
		display: flex;
		flex-direction: column;
		gap: var(--space-lg);
	}

	.primary-heading {
		font-family: var(--font-display);
		font-size: clamp(1.75rem, 4vw, 2.5rem);
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
		line-height: 1.1;
		letter-spacing: -0.02em;
	}

	h1.primary-heading {
		font-size: clamp(2rem, 5vw, 3rem);
	}

	.cards-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: var(--space-lg);
		max-width: var(--width-state);
	}

	@media (max-width: 1200px) {
		.cards-grid {
			grid-template-columns: repeat(2, 1fr);
		}
	}

	.stats-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-md);
		padding: var(--space-2xl) var(--space-xl);
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		text-align: center;
		min-height: 180px;
		transition: all var(--transition-normal);
	}

	.stats-card:hover {
		border-color: var(--civic-blue);
	}

	.stats-card.highlight-card {
		border-color: var(--civic-blue);
	}

	.stats-card.highlight-card .number-primary {
		color: var(--civic-blue);
	}

	.stats-card-link {
		text-decoration: none;
		cursor: pointer;
		position: relative;
	}

	.stats-card-link:hover {
		opacity: 0.9;
	}

	.card-arrow {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--civic-blue);
		opacity: 0;
		transition: opacity var(--transition-fast);
		margin-top: var(--space-xs);
	}

	.stats-card-link:hover .card-arrow {
		opacity: 1;
	}

	.number-primary {
		font-family: var(--font-display);
		font-size: 3rem;
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1;
	}

	.stat-title {
		font-family: var(--font-mono);
		font-size: 1.1rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.stat-description {
		font-size: 0.9rem;
		color: var(--text-secondary);
		line-height: 1.5;
	}

	.sparkline {
		width: 80px;
		height: 24px;
		overflow: visible;
	}

	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-3xl);
		font-family: var(--font-mono);
		color: var(--text-secondary);
	}

	.error-container {
		flex-direction: column;
		gap: var(--space-md);
	}

	@media (max-width: 768px) {
		.cards-grid {
			grid-template-columns: repeat(2, 1fr);
			gap: var(--space-sm);
		}

		.stats-card {
			padding: var(--space-md) var(--space-sm);
			min-height: 120px;
			gap: var(--space-xs);
		}

		.number-primary {
			font-size: 1.75rem;
		}

		.stat-title {
			font-size: 0.85rem;
		}

		.stat-description {
			font-size: 0.75rem;
			line-height: 1.3;
		}

		.metrics-container {
			gap: var(--space-xl);
		}

		.metrics-section {
			gap: var(--space-sm);
		}
	}

	@media (max-width: 640px) {
		h1.primary-heading {
			font-size: 1.5rem;
		}

		.primary-heading {
			font-size: 1.25rem;
		}
	}

	/* View toggle */
	.view-toggle {
		display: flex;
		gap: var(--space-xs);
		padding: var(--space-xs);
		background: var(--surface-secondary);
		border-radius: var(--radius-md);
		width: fit-content;
	}

	.toggle-btn {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 500;
		padding: var(--space-sm) var(--space-lg);
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast);
	}

	.toggle-btn:hover {
		color: var(--text-primary);
	}

	.toggle-btn.active {
		background: var(--surface-primary);
		color: var(--civic-blue);
		box-shadow: 0 1px 3px var(--shadow-sm);
	}

	/* Section description */
	.section-desc {
		font-size: 0.95rem;
		color: var(--text-secondary);
		margin: 0 0 var(--space-md) 0;
		line-height: 1.5;
	}

	/* Coverage summary */
	.coverage-summary {
		display: flex;
		gap: var(--space-xl);
		flex-wrap: wrap;
		margin-bottom: var(--space-lg);
	}

	.summary-item {
		display: flex;
		align-items: baseline;
		gap: var(--space-sm);
	}

	.summary-count {
		font-family: var(--font-mono);
		font-size: 1.5rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.summary-label {
		font-size: 0.9rem;
		color: var(--text-secondary);
	}

	/* City table */
	.jurisdiction-table-container {
		overflow-x: auto;
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		max-height: 600px;
		overflow-y: auto;
	}

	.jurisdiction-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.9rem;
	}

	.jurisdiction-table thead {
		position: sticky;
		top: 0;
		background: var(--surface-secondary);
		z-index: 1;
	}

	.jurisdiction-table th {
		font-family: var(--font-mono);
		font-weight: 600;
		text-align: left;
		padding: var(--space-md) var(--space-lg);
		border-bottom: 2px solid var(--border-primary);
		color: var(--text-secondary);
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.jurisdiction-table td {
		padding: var(--space-md) var(--space-lg);
		border-bottom: 1px solid var(--border-subtle);
		color: var(--text-primary);
	}

	.jurisdiction-table tbody tr:hover {
		background: var(--surface-secondary);
	}

	.col-city,
	.col-jurisdiction {
		min-width: 200px;
	}

	.col-type {
		min-width: 110px;
		font-size: 0.85rem;
		color: var(--text-secondary);
		white-space: nowrap;
	}

	.col-coverage {
		min-width: 140px;
	}

	.class-table-container {
		max-height: none;
		max-width: var(--width-state);
	}

	.kinds {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
		max-width: var(--width-state);
	}

	.kinds-heading {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-secondary);
		margin: var(--space-md) 0 0 0;
	}

	.kind-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		column-gap: var(--space-xl);
	}

	.kind-row {
		display: grid;
		grid-template-columns: 1fr auto auto;
		align-items: baseline;
		gap: var(--space-md);
		padding: var(--space-sm) 0;
		border-bottom: 1px solid var(--border-subtle);
		font-size: 0.9rem;
	}

	.kind-count {
		font-family: var(--font-mono);
		font-weight: 600;
	}

	.kind-live {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--text-secondary);
		min-width: 7em;
		text-align: right;
	}

	@media (max-width: 640px) {
		.kind-list {
			grid-template-columns: 1fr;
		}
	}

	.coverage-breakdown {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-secondary);
		margin: 0 0 var(--space-lg) 0;
		opacity: 0.75;
	}

	.col-count {
		min-width: 90px;
		text-align: right;
		font-family: var(--font-mono);
	}

	.jurisdiction-table th.col-count {
		text-align: right;
	}

	.col-pop {
		min-width: 100px;
		text-align: right;
		font-family: var(--font-mono);
	}

	.jurisdiction-table th.col-pop {
		text-align: right;
	}

	/* Coverage badges */
	.coverage-badge {
		display: inline-block;
		padding: var(--space-xs) var(--space-sm);
		border-radius: var(--radius-sm);
		font-size: 0.8rem;
		font-weight: 500;
	}

	.coverage-matter {
		background: var(--coverage-matter-bg);
		color: var(--coverage-matter-text);
	}

	.coverage-item {
		background: var(--coverage-item-bg);
		color: var(--coverage-item-text);
	}

	.coverage-monolithic {
		background: var(--coverage-monolithic-bg);
		color: var(--coverage-monolithic-text);
	}

	.coverage-synced {
		background: var(--coverage-synced-bg);
		color: var(--coverage-synced-text);
	}

	.coverage-pending {
		background: var(--coverage-pending-bg);
		color: var(--coverage-pending-text);
	}

	@media (max-width: 768px) {
		.view-toggle {
			width: 100%;
		}

		.toggle-btn {
			flex: 1;
			text-align: center;
		}

		.coverage-summary {
			gap: var(--space-md);
		}

		.summary-count {
			font-size: 1.25rem;
		}

		.jurisdiction-table th,
		.jurisdiction-table td {
			padding: var(--space-sm) var(--space-md);
		}

		.col-city,
		.col-jurisdiction {
			min-width: 150px;
		}

		.col-type {
			min-width: 90px;
			font-size: 0.8rem;
		}
	}
</style>
