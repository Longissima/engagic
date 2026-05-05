<script lang="ts">
	import type { PageData } from './$types';
	import type { CoverageType, JurisdictionType } from '$lib/api/types';
	import SeoHead from '$lib/components/SeoHead.svelte';

	let { data }: { data: PageData } = $props();
	let activeView: 'overview' | 'coverage' = $state('overview');

	function formatNumber(num: number): string {
		if (num >= 1000000) {
			return (num / 1000000).toFixed(1) + 'M';
		} else if (num >= 1000) {
			return (num / 1000).toFixed(1) + 'K';
		}
		return num.toLocaleString();
	}

	function formatPopulation(num: number): string {
		if (num >= 1000000) {
			return (num / 1000000).toFixed(1) + 'M people';
		} else if (num >= 1000) {
			return Math.round(num / 1000) + 'K people';
		}
		return num.toLocaleString() + ' people';
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

	function jurisdictionLabel(type: JurisdictionType): string {
		switch (type) {
			case 'city': return 'City';
			case 'county': return 'County';
			case 'school_district': return 'School District';
			default: return type;
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
			onclick={() => activeView = 'coverage'}
		>
			Jurisdiction Coverage
		</button>
	</div>

	{#if activeView === 'coverage'}
		{#if data.cityCoverage}
			<section class="metrics-section">
				<h1 class="primary-heading">Jurisdiction Coverage</h1>
				<p class="section-desc">All jurisdictions with active coverage — cities, counties, and school districts — sorted by population. Coverage depth indicates the granularity of legislative tracking.</p>

				<div class="coverage-summary">
					<div class="summary-item">
						<span class="summary-count">{data.cityCoverage.summary.matter}</span>
						<span class="summary-label coverage-matter">Matter-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{data.cityCoverage.summary.item}</span>
						<span class="summary-label coverage-item">Item-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{data.cityCoverage.summary.monolithic}</span>
						<span class="summary-label coverage-monolithic">Meeting-level</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{data.cityCoverage.summary.synced}</span>
						<span class="summary-label coverage-synced">Synced</span>
					</div>
					<div class="summary-item">
						<span class="summary-count">{data.cityCoverage.summary.total}</span>
						<span class="summary-label">Total</span>
					</div>
				</div>

				{#if data.cityCoverage.summary.by_type}
					{@const bt = data.cityCoverage.summary.by_type}
					<p class="coverage-breakdown">{bt.city.toLocaleString()} cities · {bt.county.toLocaleString()} counties · {bt.school_district.toLocaleString()} school districts</p>
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
							{#each data.cityCoverage.cities as city}
								<tr>
									<td class="col-jurisdiction">{city.name}, {city.state}</td>
									<td class="col-type">{jurisdictionLabel(city.type)}</td>
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
		{:else}
			<div class="loading-container">Loading coverage data...</div>
		{/if}
	{:else if data.analytics}
		<section class="metrics-section">
			<h1 class="primary-heading">Coverage Overview</h1>
			<div class="cards-grid">
				<div class="stats-card">
					<div class="stat-numbers-split">
						<span class="number-primary">{formatNumber(data.analytics.real_metrics.frequently_updated_cities)}</span>
						<span class="number-separator">/</span>
						<span class="number-secondary">{formatNumber(data.analytics.real_metrics.cities_covered)}</span>
					</div>
					<div class="stat-title">Frequently Updated Cities</div>
					<div class="stat-description">
						{#if data.analytics.real_metrics.frequently_updated_population > 0}
							{formatPopulation(data.analytics.real_metrics.frequently_updated_population)}
						{:else}
							Cities with 7+ meetings with summaries
						{/if}
					</div>
				</div>

				<div class="stats-card">
					<div class="number-primary">{formatNumber(data.analytics.real_metrics.meetings_tracked)}</div>
					<div class="stat-title">Meetings Tracked</div>
					{#if data.platformMetrics?.trends.meetings}
						<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
							<path d={sparklinePath(data.platformMetrics.trends.meetings)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
						</svg>
						<div class="stat-description">{formatGrowth(data.platformMetrics.growth.meetings_30d)}</div>
					{:else}
						<div class="stat-description">
							{#if data.analytics.real_metrics.population_with_data > 0}
								{formatPopulation(data.analytics.real_metrics.population_with_data)}
							{:else}
								City council sessions monitored
							{/if}
						</div>
					{/if}
				</div>

				<div class="stats-card">
					<div class="number-primary">{formatNumber(data.analytics.real_metrics.matters_tracked)}</div>
					<div class="stat-title">Legislative Matters</div>
					{#if data.platformMetrics?.trends.matters}
						<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
							<path d={sparklinePath(data.platformMetrics.trends.matters)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
						</svg>
						<div class="stat-description">{formatGrowth(data.platformMetrics.growth.matters_30d)}</div>
					{:else}
						<div class="stat-description">Across {formatNumber(data.analytics.real_metrics.agenda_items_processed)} agenda items</div>
					{/if}
				</div>

				<div class="stats-card">
					<div class="number-primary">{formatNumber(data.analytics.real_metrics.unique_item_summaries)}</div>
					<div class="stat-title">Unique Summaries</div>
					<div class="stat-description">
						{#if data.analytics.real_metrics.population_with_summaries > 0}
							{formatPopulation(data.analytics.real_metrics.population_with_summaries)} with summaries
						{:else}
							Across {formatNumber(data.analytics.real_metrics.meetings_with_items)} item-level meetings
						{/if}
					</div>
				</div>
			</div>
		</section>

		{#if data.platformMetrics}
			<section class="metrics-section">
				<h2 class="primary-heading">Civic Infrastructure</h2>
				<div class="cards-grid">
					<a href="/council-members" class="stats-card stats-card-link">
						<div class="number-primary">{formatNumber(data.platformMetrics.civic_infrastructure.council_members)}</div>
						<div class="stat-title">Council Members</div>
						<div class="stat-description">Elected officials tracked</div>
						<span class="card-arrow">View by city</span>
					</a>

					<a href="/committees" class="stats-card stats-card-link">
						<div class="number-primary">{formatNumber(data.platformMetrics.civic_infrastructure.committees)}</div>
						<div class="stat-title">Committees</div>
						<div class="stat-description">Legislative bodies monitored</div>
						<span class="card-arrow">View by city</span>
					</a>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.civic_infrastructure.committee_assignments)}</div>
						<div class="stat-title">Committee Assignments</div>
						<div class="stat-description">Member-to-committee relationships</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.content.matter_appearances)}</div>
						<div class="stat-title">Matter Appearances</div>
						<div class="stat-description">Items tracked across meetings</div>
					</div>
				</div>
			</section>

			<section class="metrics-section">
				<h2 class="primary-heading">Accountability Data</h2>
				<div class="cards-grid">
					<div class="stats-card highlight-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.accountability.votes)}</div>
						<div class="stat-title">Votes Recorded</div>
						{#if data.platformMetrics.trends.votes}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(data.platformMetrics.trends.votes)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
							<div class="stat-description">{formatGrowth(data.platformMetrics.growth.votes_30d)}</div>
						{:else}
							<div class="stat-description">Individual voting records captured</div>
						{/if}
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.accountability.sponsorships)}</div>
						<div class="stat-title">Sponsorships</div>
						<div class="stat-description">Legislation authorship tracked</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.accountability.cities_with_votes)}</div>
						<div class="stat-title">Cities with Vote Data</div>
						<div class="stat-description">Full voting record coverage</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.accountability.officials_with_votes)}</div>
						<div class="stat-title">Officials Tracked</div>
						<div class="stat-description">Individual voting records on file</div>
					</div>
				</div>
			</section>

			<section class="metrics-section">
				<h2 class="primary-heading">AI Processing</h2>
				<div class="cards-grid">
					<div class="stats-card highlight-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.processing.summarized_items)}</div>
						<div class="stat-title">Substantive Items</div>
						<div class="stat-description">Identified from {formatNumber(data.platformMetrics.processing.items_analyzed)} analyzed</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.content.agenda_items)}</div>
						<div class="stat-title">Agenda Items Ingested</div>
						{#if data.platformMetrics.trends.items}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(data.platformMetrics.trends.items)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
							<div class="stat-description">{formatGrowth(data.platformMetrics.growth.items_30d)}</div>
						{:else}
							<div class="stat-description">Total items collected from agendas</div>
						{/if}
					</div>

					<div class="stats-card">
						<div class="stat-numbers-split">
							<span class="number-primary">{data.platformMetrics.processing.item_summary_rate}%</span>
						</div>
						<div class="stat-title">Substantive Rate</div>
						<div class="stat-description">Of analyzed items require AI summaries</div>
					</div>

					<div class="stats-card">
						<div class="number-primary">{formatNumber(data.platformMetrics.processing.summarized_meetings)}</div>
						<div class="stat-title">Meetings Summarized</div>
						{#if data.platformMetrics.trends.meetings}
							<svg class="sparkline" viewBox="0 0 80 24" preserveAspectRatio="none">
								<path d={sparklinePath(data.platformMetrics.trends.meetings)} fill="none" stroke="var(--civic-blue)" stroke-width="1.5" />
							</svg>
							<div class="stat-description">{formatGrowth(data.platformMetrics.growth.meetings_30d)} ingested</div>
						{:else}
							<div class="stat-description">Full meeting summaries generated</div>
						{/if}
					</div>
				</div>
			</section>
		{/if}

	{:else}
		<div class="loading-container">Loading metrics...</div>
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

	.stat-numbers-split {
		display: flex;
		align-items: baseline;
		gap: 0.25rem;
	}

	.number-primary {
		font-family: var(--font-display);
		font-size: 3rem;
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1;
	}

	.number-separator {
		font-family: var(--font-mono);
		font-size: 2rem;
		color: var(--text-secondary);
	}

	.number-secondary {
		font-family: var(--font-mono);
		font-size: 2rem;
		font-weight: 500;
		color: var(--text-secondary);
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

		.number-separator {
			font-size: 1.25rem;
		}

		.number-secondary {
			font-size: 1.25rem;
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
