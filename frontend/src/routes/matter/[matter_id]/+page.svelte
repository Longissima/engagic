<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { marked } from 'marked';
	import { cleanSummary } from '$lib/utils/markdown-utils';
	import SeoHead from '$lib/components/SeoHead.svelte';
	import { truncateForMeta } from '$lib/utils/utils';
	import { getMatterSponsors, getCityCouncilMembers } from '$lib/api';
	import type { CouncilMember, MatterVotesResponse } from '$lib/api/types';
	import VoteBadge from '$lib/components/VoteBadge.svelte';
	import MatterTimeline from '$lib/components/MatterTimeline.svelte';
	import StatusBadge from '$lib/components/StatusBadge.svelte';
	import Footer from '$lib/components/Footer.svelte';
	import { logger } from '$lib/services/logger';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const matter = $derived(data.timeline.matter);
	const timeline = $derived(data.timeline.timeline);
	const firstAppearance = $derived(timeline[0]);
	const latestAppearance = $derived(timeline[timeline.length - 1]);

	// Sponsors loaded from dedicated endpoint (not all council members)
	let sponsors = $state<CouncilMember[]>([]);

	// Council members for vote display (separate from sponsors)
	let councilMembers = $state<CouncilMember[]>([]);

	onMount(async () => {
		const matterId = $page.params.matter_id;
		if (!matterId) return;

		logger.trackEvent('matter_view', { matter_id: matterId });

		// Load sponsors for this specific matter (efficient: only sponsors, not all members)
		try {
			const response = await getMatterSponsors(matterId);
			sponsors = response.sponsors || [];
		} catch (e) {
			console.debug('Could not load sponsors:', e);
		}

		// Load council members only if there are votes to display
		if (data.votes?.votes?.length && matter.banana) {
			try {
				const response = await getCityCouncilMembers(matter.banana);
				councilMembers = response.council_members || [];
			} catch (e) {
				console.debug('Could not load council members for vote display:', e);
			}
		}
	});

	function formatDate(dateStr: string): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			month: 'long',
			day: 'numeric',
			year: 'numeric'
		});
	}

	const topics = $derived(data.timeline.matter.canonical_topics || []);
	const attachments = $derived(data.timeline.matter.attachments || []);

	// Votes data from server load
	const votesData = $derived(data.votes as MatterVotesResponse | null);
	const hasVotes = $derived((votesData?.motions?.length ?? 0) > 0);

	// Collapsible sections
	let attachmentsExpanded = $state(false);
	let votesExpanded = $state(false);

	// Find council member by ID for vote display
	function findCouncilMemberById(id: string): CouncilMember | undefined {
		return councilMembers.find(m => m.id === id);
	}

	// OG meta data
	const ogTitle = $derived(
		matter.matter_file ? `${matter.matter_file}: ${matter.title}` : matter.title
	);
	const ogDescription = $derived(
		truncateForMeta(matter.canonical_summary) || 'Legislative matter tracked by engagic'
	);
	// JSON-LD structured data for Legislation schema
	const jsonLd = $derived.by(() => {
		const ld: Record<string, unknown> = {
			'@context': 'https://schema.org',
			'@type': 'Legislation',
			name: matter.title,
			url: `https://engagic.org/matter/${data.matterId}`
		};

		if (matter.matter_file) {
			ld.legislationIdentifier = matter.matter_file;
		}

		if (matter.canonical_summary) {
			ld.abstract = truncateForMeta(matter.canonical_summary, 500);
		}

		if (matter.first_seen) {
			ld.dateCreated = matter.first_seen;
		}

		if (firstAppearance) {
			ld.legislationPassedBy = {
				'@type': 'GovernmentOrganization',
				name: `${firstAppearance.city_name} City Council`,
				address: {
					'@type': 'PostalAddress',
					addressLocality: firstAppearance.city_name,
					addressRegion: firstAppearance.state
				}
			};
		}

		return JSON.stringify(ld);
	});

	// Snapshot: Preserve scroll position during navigation
	export const snapshot = {
		capture: () => ({
			scrollY: typeof window !== 'undefined' ? window.scrollY : 0
		}),
		restore: (values: { scrollY: number }) => {
			if (typeof window !== 'undefined' && typeof values.scrollY === 'number') {
				setTimeout(() => window.scrollTo(0, values.scrollY), 0);
			}
		}
	};
</script>

<SeoHead
	title="{ogTitle}"
	description="{ogDescription}"
	url="https://engagic.org/matter/{data.matterId}"
	type="article"
>
	{#snippet extra()}
		{@html `<script type="application/ld+json">${jsonLd}</script>`}
	{/snippet}
</SeoHead>

<div class="matter-page">
	<div class="matter-container">
		<div class="breadcrumb">
			<a href="/" class="breadcrumb-link" data-sveltekit-preload-data="hover">← engagic</a>
			{#if firstAppearance}
				<span class="breadcrumb-separator">•</span>
				<a href="/{firstAppearance.banana}" class="breadcrumb-link" data-sveltekit-preload-data="hover">{firstAppearance.city_name}</a>
			{/if}
		</div>

		<div class="matter-header">
			<div class="header-badges">
				{#if matter.matter_file}
					<div class="matter-badge primary">{matter.matter_file}</div>
				{/if}
				{#if matter.matter_type}
					<div class="matter-badge type">{matter.matter_type}</div>
				{/if}
				{#if matter.status}
					<StatusBadge status={matter.status} />
				{/if}
				<div class="matter-badge count">
					{data.timeline.appearance_count} appearance{data.timeline.appearance_count === 1 ? '' : 's'}
				</div>
			</div>

			<h1 class="matter-title">{matter.title}</h1>

			<div class="matter-meta">
				{#if firstAppearance}
					<div class="meta-item">
						<span class="meta-label">City:</span>
						<span class="meta-value">{firstAppearance.city_name}, {firstAppearance.state}</span>
					</div>
				{/if}
				<div class="meta-item">
					<span class="meta-label">First Seen:</span>
					<span class="meta-value">{formatDate(matter.first_seen)}</span>
				</div>
				{#if matter.final_vote_date}
					<div class="meta-item">
						<span class="meta-label">Final Vote:</span>
						<span class="meta-value">{formatDate(matter.final_vote_date)}</span>
					</div>
				{:else}
					<div class="meta-item">
						<span class="meta-label">Latest Activity:</span>
						<span class="meta-value">{formatDate(matter.last_seen)}</span>
					</div>
				{/if}
			</div>

			{#if topics.length > 0}
				<div class="matter-topics">
					{#each topics as topic}
						<span class="topic-tag">{topic}</span>
					{/each}
				</div>
			{/if}
		</div>

		{#if matter.canonical_summary}
			<div class="matter-summary-section">
				<h2 class="section-title">Summary</h2>
				<div class="matter-summary">
					{@html marked(cleanSummary(matter.canonical_summary))}
				</div>
			</div>
		{/if}

		{#if attachments.length > 0}
			<div class="attachments-section">
				<button class="section-header" onclick={() => attachmentsExpanded = !attachmentsExpanded}>
					<h2 class="section-title">Attachments ({attachments.length})</h2>
					<span class="expand-icon" class:expanded={attachmentsExpanded}></span>
				</button>
				{#if attachmentsExpanded}
					<div class="attachments-list">
						{#each attachments as attachment}
							<a href={attachment.portal_url || attachment.url} target="_blank" rel="noopener noreferrer" class="attachment-link">
								<span class="attachment-name">{attachment.name}</span>
							</a>
						{/each}
					</div>
				{/if}
			</div>
		{/if}

		{#if sponsors.length > 0}
			<div class="sponsors-section">
				<h2 class="section-title">Sponsors</h2>
				<div class="sponsors-list">
					{#each sponsors as sponsor, i (sponsor.id)}
						<a
							href="/{matter.banana}/council/{sponsor.id}"
							class="sponsor-link"
							data-sveltekit-preload-data="tap"
						>{sponsor.name}</a>
						{#if i < sponsors.length - 1}<span class="sponsor-separator">, </span>{/if}
					{/each}
				</div>
			</div>
		{/if}

		{#if hasVotes && votesData}
			<div class="voting-section">
				<button class="section-header" onclick={() => votesExpanded = !votesExpanded}>
					<div class="vote-header-content">
						<h2 class="section-title">Voting Record</h2>
						<VoteBadge
							tally={votesData.tally}
							outcome={votesData.outcome}
							size="small"
							showDetails={true}
						/>
					</div>
					<span class="expand-icon" class:expanded={votesExpanded}></span>
				</button>

				{#if votesExpanded}
					{#each votesData.votes_by_meeting ?? [] as meetingVotes (meetingVotes.meeting_id)}
						<div class="committee-vote-group">
							<h3>{meetingVotes.committee || meetingVotes.meeting_title || 'Meeting'}</h3>
							{#if meetingVotes.meeting_date}<p>{formatDate(meetingVotes.meeting_date)}</p>{/if}
							{#each meetingVotes.motions as motion}
								<div class="vote-list">
									<p>{motion.source === 'minutes' ? 'Minutes' : 'Vendor record'}: {motion.motion_text || `Motion ${motion.motion_index + 1}`}</p>
									<VoteBadge tally={motion.tally} outcome={motion.outcome} size="small" />
									{#if !motion.votes.length}<p>Individual votes are unavailable.</p>{/if}
									{#each motion.votes as vote (vote.id)}
										{@const member = findCouncilMemberById(vote.council_member_id)}
										<div class="vote-row">
											{#if member}
												<a href="/{matter.banana}/council/{member.id}" class="voter-link" data-sveltekit-preload-data="tap">{member.name}</a>
											{:else}<span class="voter-name">Unknown member</span>{/if}
											<span class="vote-value {vote.vote}">{vote.vote}</span>
										</div>
									{/each}
								</div>
							{/each}
						</div>
					{/each}
				{/if}
			</div>
		{/if}

		<div class="deliberation-cta">
			<a href="/deliberate/{data.matterId}" class="deliberate-link">
				Join Community Discussion
			</a>
		</div>

		<div class="timeline-section">
			<h2 class="section-title">Legislative Journey</h2>
			<svelte:boundary onerror={(e) => logger.error('Timeline error', {}, e instanceof Error ? e : undefined)}>
				<MatterTimeline timelineData={data.timeline} matterFile={matter.matter_file} />
				{#snippet failed(error: unknown, reset: () => void)}
					<div class="error-message">
						<p>Unable to load legislative timeline</p>
						<p class="error-detail">{error instanceof Error ? error.message : String(error)}</p>
					</div>
				{/snippet}
			</svelte:boundary>
		</div>
	</div>

	<Footer />
</div>

<style>
	.matter-page {
		width: 100%;
		min-height: 100vh;
		display: flex;
		flex-direction: column;
		padding: 2rem 1rem;
	}

	.matter-container {
		width: 100%;
		max-width: var(--width-detail);
		margin: 0 auto;
	}

	.breadcrumb {
		margin-bottom: 1.5rem;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.breadcrumb-link {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--text-link);
		text-decoration: none;
		transition: color var(--transition-normal);
		font-weight: 500;
	}

	.breadcrumb-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.breadcrumb-separator {
		color: var(--civic-gray);
	}

	.matter-header {
		background: transparent;
		border: none;
		border-bottom: 2px solid var(--text-primary);
		padding: 0 0 1.5rem;
		margin-bottom: 2rem;
	}

	.header-badges {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		margin-bottom: 1.5rem;
	}

	.matter-badge {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		font-weight: 700;
		padding: 0.4rem 0.8rem;
		border-radius: var(--radius-md);
		border: 1px solid;
	}

	.matter-badge.primary {
		background: var(--badge-blue-bg);
		border-color: var(--badge-blue-border);
		color: var(--badge-blue-text);
	}

	.matter-badge.type {
		background: var(--surface-secondary);
		border-color: var(--border-primary);
		color: var(--text-secondary);
	}

	.matter-badge.count {
		background: var(--badge-green-bg);
		border-color: var(--badge-green-border);
		color: var(--badge-green-text);
	}

	.matter-title {
		font-family: var(--font-display);
		font-size: clamp(1.25rem, 3vw, 1.75rem);
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1.25;
		letter-spacing: -0.01em;
		margin: 0 0 1.5rem 0;
	}

	.matter-meta {
		display: flex;
		flex-wrap: wrap;
		gap: 1.5rem;
		margin-bottom: 1rem;
	}

	.meta-item {
		display: flex;
		gap: 0.5rem;
		font-family: var(--font-mono);
		font-size: 0.85rem;
	}

	.meta-label {
		color: var(--civic-gray);
		font-weight: 600;
	}

	.meta-value {
		color: var(--text-primary);
	}

	.matter-topics {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-top: 1rem;
	}

	.topic-tag {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		font-weight: 500;
		padding: 0.25rem 0.55rem;
		background: var(--topic-tag-bg);
		color: var(--topic-tag-text);
		border: 1px solid var(--topic-tag-border);
		border-radius: var(--radius-xs);
		transition: all var(--transition-normal);
	}

	.matter-summary-section {
		background: transparent;
		border: none;
		border-left: 3px solid var(--item-summary-border);
		padding: 0 0 0 1.5rem;
		margin-bottom: 1.5rem;
	}

	.attachments-section,
	.sponsors-section {
		background: transparent;
		border: none;
		border-top: 1px solid var(--border-primary);
		border-radius: 0;
		padding: 1.25rem 0;
		margin-bottom: 0;
	}

	.voting-section {
		background: transparent;
		border: none;
		border-top: 2px solid var(--text-primary);
		border-radius: 0;
		padding: 1.5rem 0;
		margin-bottom: 1.5rem;
	}

	.timeline-section {
		background: transparent;
		border: none;
		border-top: 2px solid var(--text-primary);
		border-radius: 0;
		padding: 1.5rem 0;
		margin-bottom: 1.5rem;
	}

	.deliberation-cta {
		margin-bottom: 1.5rem;
	}

	.deliberate-link {
		display: block;
		text-align: center;
		padding: 0.85rem 1.5rem;
		background: var(--action-deliberate);
		color: white;
		border-radius: var(--radius-md);
		font-family: var(--font-body);
		font-size: 0.85rem;
		font-weight: 600;
		text-decoration: none;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		transition: background 0.2s ease;
	}

	.deliberate-link:hover {
		background: var(--action-deliberate-hover);
	}

	.section-title {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--civic-gray);
		margin: 0;
	}

	.section-header {
		width: 100%;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0;
		background: none;
		border: none;
		cursor: pointer;
		text-align: left;
	}

	.section-header:hover .section-title {
		color: var(--civic-blue);
	}

	.vote-header-content {
		display: flex;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.expand-icon {
		width: 0;
		height: 0;
		border-left: 6px solid transparent;
		border-right: 6px solid transparent;
		border-top: 8px solid var(--civic-gray);
		transition: transform var(--transition-normal);
		flex-shrink: 0;
	}

	.expand-icon.expanded {
		transform: rotate(180deg);
	}

	.matter-summary {
		font-family: var(--font-body);
		font-size: 1.05rem;
		line-height: 1.8;
		color: var(--text-primary);
		-webkit-font-smoothing: antialiased;
		-moz-osx-font-smoothing: grayscale;
	}

	.matter-summary :global(h1),
	.matter-summary :global(h2),
	.matter-summary :global(h3),
	.matter-summary :global(h4) {
		font-family: var(--font-display);
		color: var(--text-primary);
		margin-top: 2.5rem;
		margin-bottom: 1rem;
		line-height: 1.2;
		font-weight: 400;
	}

	.matter-summary :global(h1) { font-size: 1.75rem; }
	.matter-summary :global(h2) { font-size: 1.5rem; }
	.matter-summary :global(h3) { font-size: 1.25rem; }
	.matter-summary :global(h4) { font-size: 1.1rem; }

	.matter-summary :global(h1:first-child),
	.matter-summary :global(h2:first-child),
	.matter-summary :global(h3:first-child),
	.matter-summary :global(h4:first-child) {
		margin-top: 0;
	}

	.matter-summary :global(p) {
		margin: 1.5rem 0;
	}

	.matter-summary :global(ul),
	.matter-summary :global(ol) {
		margin: 1.5rem 0;
		padding-left: 2rem;
	}

	.matter-summary :global(li) {
		margin: 0.5rem 0;
	}

	.matter-summary :global(blockquote) {
		margin: 2rem 0;
		padding-left: 1.5rem;
		border-left: 4px solid var(--text-secondary);
		color: var(--text-secondary);
		font-style: italic;
	}

	.matter-summary :global(code) {
		font-family: var(--font-mono);
		font-size: 0.9em;
		background: var(--surface-secondary);
		color: var(--text-primary);
		padding: 0.2rem 0.4rem;
		border-radius: 3px;
	}

	.matter-summary :global(pre) {
		margin: 2rem 0;
		padding: 1.5rem;
		background: var(--surface-secondary);
		border-radius: var(--radius-md);
		overflow-x: auto;
	}

	.matter-summary :global(pre code) {
		background: none;
		padding: 0;
	}

	.matter-summary :global(strong) {
		font-weight: 700;
		color: var(--text-primary);
	}

	.matter-summary :global(em) {
		font-style: italic;
	}

	.matter-summary :global(hr) {
		margin: 2.5rem 0;
		border: none;
		border-top: 1px solid var(--border-primary);
	}

	.matter-summary :global(a) {
		color: var(--text-link);
		text-decoration: underline;
		transition: color var(--transition-normal);
	}

	.matter-summary :global(a:hover) {
		color: var(--civic-accent);
	}

	.matter-summary :global(img) {
		max-width: 100%;
		height: auto;
		border-radius: var(--radius-md);
		margin: 2rem 0;
	}

	.attachments-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-top: 1rem;
	}

	.attachment-link {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.75rem 1rem;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		text-decoration: none;
		color: var(--text-primary);
		font-family: var(--font-mono);
		font-size: 0.85rem;
		transition: all var(--transition-normal);
	}

	.attachment-link:hover {
		background: var(--civic-blue);
		border-color: var(--civic-blue);
		color: var(--civic-white);
	}

	.attachment-icon {
		font-size: 1.1rem;
		flex-shrink: 0;
	}

	.attachment-name {
		flex: 1;
		font-weight: 500;
	}

	.sponsors-list {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--text-primary);
		line-height: 1.6;
	}

	.sponsor-link {
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 500;
		transition: color var(--transition-normal);
	}

	.sponsor-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.sponsor-name {
		color: var(--text-primary);
	}

	.sponsor-separator {
		color: var(--civic-gray);
	}

	/* Voting section styles */
	.vote-summary {
		margin-bottom: 1rem;
	}

	.vote-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-top: 1rem;
	}

	.vote-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.5rem 0;
		border-bottom: 1px solid var(--border-primary);
	}

	.voter-link {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 500;
	}

	.voter-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.voter-name {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--text-primary);
	}

	.vote-value {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		padding: 0.2rem 0.5rem;
		border-radius: var(--radius-xs);
		text-transform: uppercase;
	}

	.vote-value.yes { background: var(--vote-yes-bg); color: var(--vote-yes-text); }
	.vote-value.no { background: var(--vote-no-bg); color: var(--vote-no-text); }
	.vote-value.abstain { background: var(--vote-abstain-bg); color: var(--vote-abstain-text); }
	.vote-value.absent { background: var(--vote-absent-bg); color: var(--vote-absent-text); }

	/* Votes grouped by committee */
	.votes-by-committee {
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
		margin-top: 1rem;
	}

	.committee-vote-group {
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		overflow: hidden;
	}

	.committee-vote-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		padding: 0.75rem 1rem;
		background: var(--surface-secondary);
		border-bottom: 1px solid var(--border-primary);
	}

	.committee-info {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.committee-name {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--civic-purple);
	}

	.committee-name-link {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--civic-purple);
		text-decoration: none;
		transition: all var(--transition-fast);
	}

	.committee-name-link:hover {
		text-decoration: underline;
		color: var(--civic-blue);
	}

	.meeting-name {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--text-primary);
	}


	.committee-vote-group .vote-list {
		margin-top: 0;
		padding: 0.5rem;
	}

	.error-message {
		padding: 1.5rem;
		background: var(--surface-secondary);
		border: 1.5px solid var(--civic-red);
		border-radius: var(--radius-md);
		text-align: center;
	}

	.error-message p {
		margin: 0.5rem 0;
		color: var(--text-primary);
		font-family: var(--font-mono);
		font-size: 0.9rem;
	}

	.error-detail {
		color: var(--civic-red);
		font-size: 0.85rem;
	}

	@media (max-width: 768px) {
		.matter-page {
			padding: 1rem 0.5rem;
		}

		.matter-header {
			padding: 1.5rem;
		}

		.matter-title {
			font-size: 1.1rem;
		}

		.matter-meta {
			flex-direction: column;
			gap: 0.75rem;
		}

		.matter-summary-section {
			padding: 1.5rem;
		}

		.attachments-section,
		.sponsors-section {
			padding: 1rem;
		}

		.voting-section,
		.timeline-section {
			padding: 1.25rem;
		}
	}
</style>
