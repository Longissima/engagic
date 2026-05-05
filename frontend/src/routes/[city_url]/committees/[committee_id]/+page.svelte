<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getCommittee, getCommitteeVotes } from '$lib/api';
	import SeoHead from '$lib/components/SeoHead.svelte';
	import type { Committee, CommitteeMember, CommitteeVoteRecord } from '$lib/api/types';
	import Footer from '$lib/components/Footer.svelte';
	import { logger } from '$lib/services/logger';
	import VoteBadge from '$lib/components/VoteBadge.svelte';

	const city_banana = $page.params.city_url as string;
	const committee_id = $page.params.committee_id as string;

	let committee = $state<Committee | null>(null);
	let members = $state<CommitteeMember[]>([]);
	let votes = $state<CommitteeVoteRecord[]>([]);
	let cityName = $state<string>('');
	let stateName = $state<string>('');
	let loading = $state(true);
	let error = $state<string | null>(null);

	const chairs = $derived(members.filter(m => m.role?.toLowerCase().includes('chair') && !m.role?.toLowerCase().includes('vice')));
	const viceChairs = $derived(members.filter(m => m.role?.toLowerCase().includes('vice')));
	const regularMembers = $derived(members.filter(m => !m.role || (!m.role.toLowerCase().includes('chair') && !m.role.toLowerCase().includes('vice'))));

	onMount(async () => {
		try {
			const [committeeResponse, votesResponse] = await Promise.all([
				getCommittee(committee_id),
				getCommitteeVotes(committee_id, 50)
			]);

			committee = committeeResponse.committee;
			members = committeeResponse.members || [];
			cityName = committeeResponse.city_name || '';
			stateName = committeeResponse.state || '';
			votes = votesResponse.votes || [];
		} catch (e) {
			logger.error('Failed to load committee', {}, e instanceof Error ? e : undefined);
			error = 'Unable to load committee details.';
		} finally {
			loading = false;
		}
	});

	function formatDate(dateStr: string): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}
</script>

<SeoHead
	title="{committee?.name || 'Committee'} - {cityName || city_banana} - engagic"
	description="Committee details and voting history"
	url="https://engagic.org/{city_banana}/committees/{$page.params.committee_id}"
/>

<div class="container">
	<div class="top-nav">
		<a href="/{city_banana}/committees" class="back-link" data-sveltekit-preload-data="hover">
			&larr; Committees
		</a>
		<a href="/" class="compact-logo" aria-label="Return to engagic homepage" data-sveltekit-preload-data="hover">
			<img src="/icon-64.png" alt="engagic" class="logo-icon" />
		</a>
	</div>

	{#if loading}
		<div class="loading-state">Loading committee...</div>
	{:else if error}
		<div class="error-state">
			<p>{error}</p>
			<a href="/{city_banana}/committees" class="back-cta">Return to committees</a>
		</div>
	{:else if committee}
		<div class="committee-header">
			<div class="committee-identity">
				<h1 class="committee-name">{committee.name}</h1>
				<div class="committee-meta">
					{#if cityName}
						<span class="jurisdiction-name">{cityName}, {stateName}</span>
					{/if}
					{#if committee.status !== 'active'}
						<span class="status-badge inactive">Inactive</span>
					{/if}
				</div>
				{#if committee.description}
					<p class="committee-description">{committee.description}</p>
				{/if}
			</div>

			<div class="committee-stats">
				<div class="stat-card">
					<span class="stat-value">{members.length}</span>
					<span class="stat-label">Members</span>
				</div>
				<div class="stat-card">
					<span class="stat-value">{votes.length}</span>
					<span class="stat-label">Votes</span>
				</div>
			</div>
		</div>

		{#if members.length > 0}
			<section class="roster-section">
				<h2 class="section-title">Current Roster</h2>
				<div class="roster-list">
					{#each chairs as member (member.id)}
						<a
							href="/{city_banana}/council/{member.council_member_id}"
							class="member-card chair"
							data-sveltekit-preload-data="tap"
						>
							<div class="member-info">
								<span class="member-name">{member.member_name}</span>
								<span class="member-role">Chair</span>
							</div>
							{#if member.title || member.district}
								<span class="member-position">
									{member.title || ''}{member.title && member.district ? ' - ' : ''}{member.district ? `District ${member.district}` : ''}
								</span>
							{/if}
						</a>
					{/each}
					{#each viceChairs as member (member.id)}
						<a
							href="/{city_banana}/council/{member.council_member_id}"
							class="member-card vice-chair"
							data-sveltekit-preload-data="tap"
						>
							<div class="member-info">
								<span class="member-name">{member.member_name}</span>
								<span class="member-role">Vice Chair</span>
							</div>
							{#if member.title || member.district}
								<span class="member-position">
									{member.title || ''}{member.title && member.district ? ' - ' : ''}{member.district ? `District ${member.district}` : ''}
								</span>
							{/if}
						</a>
					{/each}
					{#each regularMembers as member (member.id)}
						<a
							href="/{city_banana}/council/{member.council_member_id}"
							class="member-card"
							data-sveltekit-preload-data="tap"
						>
							<div class="member-info">
								<span class="member-name">{member.member_name}</span>
								{#if member.role}
									<span class="member-role">{member.role}</span>
								{/if}
							</div>
							{#if member.title || member.district}
								<span class="member-position">
									{member.title || ''}{member.title && member.district ? ' - ' : ''}{member.district ? `District ${member.district}` : ''}
								</span>
							{/if}
						</a>
					{/each}
				</div>
			</section>
		{:else}
			<div class="empty-section">
				<p>No members assigned to this committee yet.</p>
			</div>
		{/if}

		{#if votes.length > 0}
			<section class="votes-section">
				<h2 class="section-title">Recent Votes</h2>
				<div class="votes-list">
					{#each votes as vote (vote.matter_id + vote.meeting_id)}
						<a
							href="/matter/{vote.matter_id}"
							class="vote-card"
							data-sveltekit-preload-data="tap"
						>
							<div class="vote-info">
								<div class="vote-header">
									{#if vote.matter_file}
										<span class="matter-file">{vote.matter_file}</span>
									{/if}
									{#if vote.appeared_at}
										<span class="vote-date">{formatDate(vote.appeared_at)}</span>
									{/if}
								</div>
								<span class="vote-title">{vote.matter_title}</span>
							</div>
							{#if vote.vote_outcome && vote.vote_tally}
								<VoteBadge tally={vote.vote_tally} outcome={vote.vote_outcome} size="small" />
							{/if}
						</a>
					{/each}
				</div>
			</section>
		{/if}
	{/if}

	<Footer />
</div>

<style>
	.container {
		width: var(--width-detail);
		padding: 2rem 1rem;
		margin: 0 auto;
	}

	.top-nav {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1.5rem;
	}

	.back-link {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 500;
	}

	.back-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.compact-logo {
		transition: transform var(--transition-normal);
	}

	.compact-logo:hover {
		transform: scale(1.05);
	}

	.logo-icon {
		width: 48px;
		height: 48px;
		border-radius: var(--radius-lg);
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
	}

	.loading-state,
	.error-state,
	.empty-section {
		text-align: center;
		padding: 3rem;
		background: var(--surface-secondary);
		border-radius: var(--radius-lg);
		border: 1px solid var(--border-primary);
	}

	.loading-state {
		font-family: var(--font-mono);
		color: var(--civic-gray);
	}

	.error-state p,
	.empty-section p {
		font-family: var(--font-mono);
		color: var(--text-primary);
		margin: 0;
	}

	.back-cta {
		display: inline-block;
		margin-top: 1rem;
		padding: 0.5rem 1rem;
		background: var(--civic-blue);
		color: white;
		border-radius: var(--radius-sm);
		text-decoration: none;
		font-family: var(--font-mono);
		font-size: 0.85rem;
	}

	.committee-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 2rem;
		margin-bottom: 2rem;
		padding-bottom: 1.5rem;
		border-bottom: 1px solid var(--border-primary);
	}

	.committee-identity {
		flex: 1;
	}

	.committee-name {
		font-family: var(--font-display);
		font-size: clamp(1.5rem, 4vw, 2.25rem);
		font-weight: 400;
		color: var(--text-primary);
		margin: 0 0 0.5rem;
		letter-spacing: -0.02em;
		line-height: 1.15;
	}

	.committee-meta {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-bottom: 0.5rem;
	}

	.jurisdiction-name {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--civic-gray);
	}

	.status-badge.inactive {
		font-family: var(--font-body);
		font-size: 0.6rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		padding: 2px 6px;
		background: var(--surface-secondary);
		color: var(--civic-gray);
		border: none;
		border-radius: 2px;
		text-transform: uppercase;
	}

	.committee-description {
		font-family: var(--font-body);
		font-size: 0.95rem;
		color: var(--text-secondary);
		line-height: 1.5;
		margin: 0.5rem 0 0;
	}

	.committee-stats {
		display: flex;
		gap: 1rem;
	}

	.stat-card {
		text-align: center;
		padding: 1rem 1.25rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		min-width: 80px;
	}

	.stat-value {
		display: block;
		font-family: var(--font-display);
		font-size: 1.75rem;
		font-weight: 400;
		color: var(--text-primary);
	}

	.stat-label {
		font-family: var(--font-body);
		font-size: 0.65rem;
		color: var(--civic-gray);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}

	.section-title {
		font-family: var(--font-mono);
		font-size: 1rem;
		font-weight: 700;
		color: var(--text-primary);
		margin: 0 0 1rem;
		text-transform: uppercase;
		letter-spacing: 0.5px;
	}

	.roster-section {
		margin-bottom: 2rem;
	}

	.roster-list {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: 0.75rem;
	}

	.member-card {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 1rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-md);
		text-decoration: none;
		transition: all var(--transition-normal);
	}

	.member-card:hover {
		padding-left: 1.5rem;
	}

	.member-card.chair {
		border-left-color: var(--civic-orange);
		background: linear-gradient(to right, rgba(249, 115, 22, 0.05), transparent);
	}

	.member-card.vice-chair {
		border-left-color: var(--civic-blue);
	}

	.member-info {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.member-name {
		font-family: var(--font-mono);
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.member-role {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		padding: 0.15rem 0.5rem;
		background: var(--surface-secondary);
		color: var(--civic-gray);
		border-radius: var(--radius-xs);
		text-transform: uppercase;
	}

	.member-position {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--civic-gray);
	}

	.votes-section {
		margin-bottom: 2rem;
	}

	.votes-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.vote-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.75rem 1rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-sm);
		text-decoration: none;
		transition: all var(--transition-normal);
	}

	.vote-card:hover {
		padding-left: 1.25rem;
	}

	.vote-info {
		flex: 1;
		min-width: 0;
	}

	.vote-header {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-bottom: 0.25rem;
	}

	.matter-file {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--civic-blue);
	}

	.vote-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--text-secondary);
	}

	.vote-title {
		display: block;
		font-family: var(--font-body);
		font-size: 0.9rem;
		color: var(--text-primary);
		line-height: 1.4;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	@media (max-width: 640px) {
		.container {
			padding: 1rem;
		}

		.committee-header {
			flex-direction: column;
			gap: 1rem;
		}

		.committee-name {
			font-size: 1.5rem;
		}

		.committee-stats {
			width: 100%;
			justify-content: flex-start;
		}

		.stat-card {
			padding: 0.75rem 1rem;
			min-width: auto;
			flex: 1;
		}

		.stat-value {
			font-size: 1.25rem;
		}

		.roster-list {
			grid-template-columns: 1fr;
		}

		.vote-card {
			flex-wrap: wrap;
		}
	}
</style>
