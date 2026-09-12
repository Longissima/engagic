<script lang="ts">
	import type { VoteTally, VoteOutcome } from '$lib/api/types';

	interface Props {
		tally: VoteTally | null;
		outcome?: VoteOutcome | null;
		size?: 'small' | 'medium';
		showDetails?: boolean;
	}

	let { tally, outcome, size = 'medium', showDetails = false }: Props = $props();

	const estimate = $derived(!outcome && tally && ((tally.yes ?? 0) + (tally.no ?? 0) > 0)
		? ((tally.yes ?? 0) > (tally.no ?? 0) ? 'Likely passed' : (tally.no ?? 0) > (tally.yes ?? 0) ? 'Likely failed' : 'Tied vote') : null);
	const outcomeLabel = $derived.by(() => {
		if (!outcome || outcome === 'no_vote' || outcome === 'unknown') return estimate;
		const labels: Record<string, string> = {
			passed: 'Passed',
			failed: 'Failed',
			tabled: 'Tabled',
			withdrawn: 'Withdrawn',
			referred: 'Referred',
			amended: 'Amended'
		};
		return labels[outcome] || outcome;
	});

	const variant = $derived.by(() => {
		if (!outcome) return 'neutral';
		if (outcome === 'passed') return 'success';
		if (outcome === 'failed') return 'danger';
		return 'neutral';
	});

	const tallyText = $derived(`${tally?.yes ?? 0}-${tally?.no ?? 0}`);
	const hasVotes = $derived((tally?.yes ?? 0) > 0 || (tally?.no ?? 0) > 0);
</script>

{#if hasVotes || outcomeLabel}
	<span class="vote-badge {variant} {size}" title={estimate ? 'Simple-majority estimate; the recorded outcome is unavailable.' : tally ? `Yes: ${tally.yes ?? 0}, No: ${tally.no ?? 0}${tally.abstain ? `, Abstain: ${tally.abstain}` : ''}${tally.absent ? `, Absent: ${tally.absent}` : ''}` : 'Numerical tally unavailable'}>
		{#if outcomeLabel}
			<span class="outcome">{outcomeLabel}</span>
		{/if}
		{#if tally}<span class="tally">{tallyText}</span>{/if}
		{#if showDetails && (tally?.abstain || tally?.absent)}
			<span class="details">
				{#if tally?.abstain}
					<span class="abstain">{tally?.abstain}A</span>
				{/if}
			</span>
		{/if}
	</span>
{/if}

<style>
	.vote-badge {
		font-family: var(--font-mono);
		font-weight: 600;
		border-radius: var(--radius-sm);
		border: 1px solid;
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		white-space: nowrap;
	}

	.medium {
		font-size: 0.8rem;
		padding: 0.3rem 0.6rem;
	}

	.small {
		font-size: 0.7rem;
		padding: 0.2rem 0.45rem;
	}

	.outcome {
		font-weight: 700;
	}

	.tally {
		opacity: 0.9;
	}

	.details {
		font-size: 0.85em;
		opacity: 0.75;
	}

	/* Success: passed */
	.success {
		background: var(--badge-green-bg);
		border-color: var(--badge-green-border);
		color: var(--badge-green-text);
	}

	/* Danger: failed */
	.danger {
		background: var(--badge-danger-bg);
		border-color: var(--badge-danger-border);
		color: var(--badge-danger-text);
	}

	/* Neutral: no outcome, tabled, etc */
	.neutral {
		background: var(--surface-secondary);
		border-color: var(--border-primary);
		color: var(--text-primary);
	}
</style>
