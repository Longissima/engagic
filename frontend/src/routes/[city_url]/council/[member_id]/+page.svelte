<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getCouncilMemberVotes, getMemberCommittees } from '$lib/api';
	import SeoHead from '$lib/components/SeoHead.svelte';
	import type { CouncilMember, VoteRecord, VoteStatistics, CommitteeAssignment } from '$lib/api/types';
	import Footer from '$lib/components/Footer.svelte';
	import { logger } from '$lib/services/logger';

	// Route params are always defined on this page
	const city_banana = $page.params.city_url as string;
	const member_id = $page.params.member_id as string;

	let member = $state<CouncilMember | null>(null);
	let votingRecord = $state<VoteRecord[]>([]);
	let statistics = $state<VoteStatistics | null>(null);
	let committees = $state<CommitteeAssignment[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Computed stats
	const totalVotes = $derived(statistics ? statistics.yes + statistics.no + statistics.abstain + statistics.absent + (statistics.present ?? 0) + (statistics.recused ?? 0) + (statistics.not_voting ?? 0) : 0);
	const yesPercent = $derived(totalVotes > 0 && statistics ? Math.round((statistics.yes / totalVotes) * 100) : 0);
	const noPercent = $derived(totalVotes > 0 && statistics ? Math.round((statistics.no / totalVotes) * 100) : 0);
	const abstainPercent = $derived(totalVotes > 0 && statistics ? Math.round((statistics.abstain / totalVotes) * 100) : 0);
	const absentPercent = $derived(totalVotes > 0 && statistics ? Math.round((statistics.absent / totalVotes) * 100) : 0);

	onMount(async () => {
		try {
			const [votesResponse, committeesResponse] = await Promise.all([
				getCouncilMemberVotes(member_id, 100),
				getMemberCommittees(member_id, true).catch(() => ({ committees: [] }))
			]);
			member = votesResponse.member;
			votingRecord = votesResponse.voting_record;
			statistics = votesResponse.statistics;
			committees = committeesResponse.committees || [];
		} catch (e) {
			logger.error('Failed to load council member', {}, e instanceof Error ? e : undefined);
			error = 'Unable to load council member profile.';
		} finally {
			loading = false;
		}
	});

	function formatDate(dateStr: string): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function formatDateShort(dateStr: string): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
	}

	function getVoteClass(vote: string): string {
		switch (vote) {
			case 'yes': return 'vote-yes';
			case 'no': return 'vote-no';
			case 'abstain': return 'vote-abstain';
			case 'absent': return 'vote-absent';
			default: return 'vote-unknown';
		}
	}

	function getVoteLabel(vote: string): string {
		switch (vote) {
			case 'yes': return 'Yes';
			case 'no': return 'No';
			case 'abstain': return 'Abstain';
			case 'absent': return 'Absent';
			case 'present': return 'Present';
			default: return vote;
		}
	}
</script>

<SeoHead
	title="{member?.name || 'Council Member'} - {city_banana} - engagic"
	description="Voting record for {member?.name || 'council member'}"
	url="https://engagic.org/{city_banana}/council/{$page.params.member_id}"
	type="profile"
/>

<div class="container">
	<div class="top-nav">
		<a href="/{city_banana}/council" class="back-link" data-sveltekit-preload-data="hover">
			&larr; Council Roster
		</a>
		<a href="/" class="compact-logo" aria-label="Return to engagic homepage" data-sveltekit-preload-data="hover">
			<img src="/icon-64.png" alt="engagic" class="logo-icon" />
		</a>
	</div>

	{#if loading}
		<div class="loading-state">Loading council member profile...</div>
	{:else if error}
		<div class="error-state">
			<p>{error}</p>
			<a href="/{city_banana}/council" class="back-cta">Return to council roster</a>
		</div>
	{:else if member}
		<div class="member-header">
			<div class="member-identity">
				<h1 class="member-name">{member.name}</h1>
				<div class="member-meta">
					{#if member.title}
						<span class="member-title">{member.title}</span>
					{/if}
					{#if member.district}
						<span class="member-district">District {member.district}</span>
					{/if}
					{#if member.status !== 'active'}
						<span class="status-badge former">Former Member</span>
					{/if}
				</div>
				<div class="member-tenure">
					Active: {formatDateShort(member.first_seen)} - {member.status === 'active' ? 'Present' : formatDateShort(member.last_seen)}
				</div>
			</div>

			<div class="member-stats">
				<div class="stat-card">
					<span class="stat-value">{member.vote_count.toLocaleString()}</span>
					<span class="stat-label">Votes Cast</span>
				</div>
				<div class="stat-card">
					<span class="stat-value">{member.sponsorship_count.toLocaleString()}</span>
					<span class="stat-label">Bills Sponsored</span>
				</div>
			</div>
		</div>

		{#if statistics && totalVotes > 0}
			<section class="voting-breakdown">
				<h2 class="section-title">Voting Breakdown</h2>
				<div class="vote-bars">
					<div class="vote-bar-row">
						<span class="vote-type yes">Yes</span>
						<div class="bar-track">
							<div class="bar-fill yes" style="width: {yesPercent}%"></div>
						</div>
						<span class="vote-count">{statistics.yes} ({yesPercent}%)</span>
					</div>
					<div class="vote-bar-row">
						<span class="vote-type no">No</span>
						<div class="bar-track">
							<div class="bar-fill no" style="width: {noPercent}%"></div>
						</div>
						<span class="vote-count">{statistics.no} ({noPercent}%)</span>
					</div>
					{#if statistics.abstain > 0}
						<div class="vote-bar-row">
							<span class="vote-type abstain">Abstain</span>
							<div class="bar-track">
								<div class="bar-fill abstain" style="width: {abstainPercent}%"></div>
							</div>
							<span class="vote-count">{statistics.abstain} ({abstainPercent}%)</span>
						</div>
					{/if}
					{#if statistics.absent > 0}
						<div class="vote-bar-row">
							<span class="vote-type absent">Absent</span>
							<div class="bar-track">
								<div class="bar-fill absent" style="width: {absentPercent}%"></div>
							</div>
							<span class="vote-count">{statistics.absent} ({absentPercent}%)</span>
						</div>
					{/if}
				</div>
			</section>
		{/if}

		{#if committees.length > 0}
			<section class="committees-section">
				<h2 class="section-title">Committee Assignments</h2>
				<div class="committees-list">
					{#each committees as committee (committee.committee_id)}
						<a
							href="/{city_banana}/committees/{committee.committee_id}"
							class="committee-chip"
							data-sveltekit-preload-data="tap"
						>
							<span class="committee-name">{committee.committee_name}</span>
							{#if committee.role}
								<span class="committee-role">{committee.role}</span>
							{/if}
						</a>
					{/each}
				</div>
			</section>
		{/if}

		{#if votingRecord.length > 0}
			<section class="voting-record">
				<h2 class="section-title">Recent Votes</h2>
				<div class="vote-table">
					{#each votingRecord as vote (vote.id)}
						<a
							href="/matter/{vote.matter_id}"
							class="vote-row"
							data-sveltekit-preload-data="tap"
						>
							<div class="vote-info">
								<div class="vote-matter-header">
									{#if vote.matter_file}
										<span class="matter-file">{vote.matter_file}</span>
									{/if}
									{#if vote.matter_type}
										<span class="matter-type">{vote.matter_type}</span>
									{/if}
								</div>
								<span class="vote-title">{vote.title}</span>
								<span class="vote-date">{formatDate(vote.vote_date)}</span>
							</div>
							<span class="vote-value {getVoteClass(vote.vote)}">{getVoteLabel(vote.vote)}</span>
						</a>
					{/each}
				</div>
			</section>
		{:else}
			<div class="empty-state">
				<p>No voting record available for this council member.</p>
			</div>
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
	.empty-state {
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
	.empty-state p {
		font-family: var(--font-mono);
		color: var(--text-primary);
		margin: 0 0 1rem;
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

	/* Member Header */
	.member-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 2rem;
		margin-bottom: 2rem;
		padding-bottom: 1.5rem;
		border-bottom: 1px solid var(--border-primary);
	}

	.member-identity {
		flex: 1;
	}

	.member-name {
		font-family: var(--font-display);
		font-size: clamp(1.5rem, 4vw, 2.25rem);
		font-weight: 400;
		color: var(--text-primary);
		margin: 0 0 0.5rem;
		letter-spacing: -0.02em;
		line-height: 1.15;
	}

	.member-meta {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
		margin-bottom: 0.5rem;
	}

	.member-title {
		font-family: var(--font-mono);
		font-size: 0.9rem;
		color: var(--civic-blue);
		font-weight: 600;
	}

	.member-district {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--civic-gray);
	}

	.status-badge.former {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		padding: 0.2rem 0.6rem;
		background: var(--surface-secondary);
		color: var(--civic-gray);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-lg);
		text-transform: uppercase;
	}

	.member-tenure {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-secondary);
	}

	.member-stats {
		display: flex;
		gap: 1rem;
	}

	.stat-card {
		text-align: center;
		padding: 1rem 1.25rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		min-width: 100px;
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

	/* Voting Breakdown */
	.section-title {
		font-family: var(--font-mono);
		font-size: 1rem;
		font-weight: 700;
		color: var(--text-primary);
		margin: 0 0 1rem;
		text-transform: uppercase;
		letter-spacing: 0.5px;
	}

	.voting-breakdown {
		margin-bottom: 2rem;
		padding: 1.25rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-lg);
	}

	.vote-bars {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.vote-bar-row {
		display: flex;
		align-items: center;
		gap: 1rem;
	}

	.vote-type {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		font-weight: 600;
		width: 60px;
		text-align: right;
	}

	.vote-type.yes { color: var(--vote-yes-text); }
	.vote-type.no { color: var(--vote-no-text); }
	.vote-type.abstain { color: var(--vote-abstain-text); }
	.vote-type.absent { color: var(--vote-absent-text); }

	.bar-track {
		flex: 1;
		height: 16px;
		background: var(--surface-secondary);
		border-radius: var(--radius-md);
		overflow: hidden;
	}

	.bar-fill {
		height: 100%;
		border-radius: var(--radius-md);
		transition: width var(--transition-slow);
	}

	.bar-fill.yes { background: var(--vote-yes-text); }
	.bar-fill.no { background: var(--vote-no-text); }
	.bar-fill.abstain { background: var(--vote-abstain-text); }
	.bar-fill.absent { background: var(--vote-absent-text); }

	.vote-count {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-secondary);
		width: 80px;
	}

	/* Committee Assignments */
	.committees-section {
		margin-bottom: 2rem;
		padding: 1.25rem;
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-lg);
	}

	.committees-list {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.committee-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		text-decoration: none;
		transition: all var(--transition-normal);
	}

	.committee-chip:hover {
		background: var(--civic-blue);
		border-color: var(--civic-blue);
	}

	.committee-chip:hover .committee-name,
	.committee-chip:hover .committee-role {
		color: white;
	}

	.committee-chip .committee-name {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.committee-chip .committee-role {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		padding: 0.1rem 0.4rem;
		background: var(--civic-blue);
		color: white;
		border-radius: var(--radius-xs);
		text-transform: uppercase;
	}

	/* Voting Record Table */
	.voting-record {
		margin-bottom: 2rem;
	}

	.vote-table {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.vote-row {
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

	.vote-row:hover {
		padding-left: 1.25rem;
	}

	.vote-info {
		flex: 1;
		min-width: 0;
	}

	.vote-matter-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.25rem;
	}

	.matter-file {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--civic-blue);
	}

	.matter-type {
		font-family: var(--font-mono);
		font-size: 0.65rem;
		padding: 0.1rem 0.4rem;
		background: var(--surface-secondary);
		color: var(--civic-gray);
		border-radius: var(--radius-xs);
		text-transform: uppercase;
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

	.vote-date {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--text-secondary);
	}

	.vote-value {
		flex-shrink: 0;
		font-family: var(--font-mono);
		font-size: 0.8rem;
		font-weight: 700;
		padding: 0.3rem 0.75rem;
		border-radius: var(--radius-sm);
	}

	.vote-yes {
		background: var(--vote-yes-bg);
		color: var(--vote-yes-text);
		border: 1px solid var(--vote-yes-text);
	}

	.vote-no {
		background: var(--vote-no-bg);
		color: var(--vote-no-text);
		border: 1px solid var(--vote-no-text);
	}

	.vote-abstain {
		background: var(--vote-abstain-bg);
		color: var(--vote-abstain-text);
		border: 1px solid var(--vote-abstain-text);
	}

	.vote-absent {
		background: var(--surface-secondary);
		color: var(--civic-gray);
		border: 1px solid var(--border-primary);
	}

	.vote-unknown {
		background: var(--surface-secondary);
		color: var(--text-secondary);
		border: 1px solid var(--border-primary);
	}


	@media (max-width: 640px) {
		.container {
			padding: 1rem;
		}

		.member-header {
			flex-direction: column;
			gap: 1rem;
		}

		.member-name {
			font-size: 1.5rem;
		}

		.member-stats {
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

		.vote-bar-row {
			flex-wrap: wrap;
		}

		.vote-type {
			width: 50px;
			text-align: left;
		}

		.vote-count {
			width: 100%;
			margin-top: 0.25rem;
		}

		.vote-row {
			flex-wrap: wrap;
		}

		.vote-value {
			margin-top: 0.5rem;
		}
	}
</style>
