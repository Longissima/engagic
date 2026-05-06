<script lang="ts">
	import { onMount } from 'svelte';
	import { apiClient } from '$lib/api/api-client';
	import type { AnalyticsData } from '$lib/api/types';

	let { analytics }: { analytics?: AnalyticsData | null } = $props();

	let clientAnalytics: AnalyticsData | null = $state(null);

	function getMetrics() {
		const source = analytics ?? clientAnalytics;
		return source?.real_metrics ?? null;
	}

	function compact(n: number): string {
		if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
		return n.toLocaleString();
	}

	const metrics = $derived(getMetrics());

	onMount(async () => {
		if (!analytics) {
			try {
				clientAnalytics = await apiClient.getAnalytics();
			} catch {
				// Graceful degradation — footer just won't show stats
			}
		}
	});
</script>

<footer class="site-footer">
	<div class="footer-brand">engagic is open-source (AGPL-3.0)</div>

	<nav class="footer-links" aria-label="Footer navigation">
		<a href="/about/general" class="footer-link">How it works</a>
		<span class="footer-sep" aria-hidden="true">&bull;</span>
		<a href="/country" class="footer-link">Coverage map</a>
		<span class="footer-sep" aria-hidden="true">&bull;</span>
		<a href="/about/community" class="footer-link">Community</a>
		<span class="footer-sep" aria-hidden="true">&bull;</span>
		<a href="/about/donate" class="footer-link">Donate</a>
		<span class="footer-sep" aria-hidden="true">&bull;</span>
		<a href="https://github.com/engagic" class="footer-link" target="_blank" rel="noopener noreferrer">GitHub</a>
		<span class="footer-sep" aria-hidden="true">&bull;</span>
		<a href="/about/terms" class="footer-link">Terms</a>
	</nav>

	{#if metrics}
		{@const byType = metrics.by_type?.live ?? metrics.by_type?.total}
		{@const meetingCount = metrics.meetings_with_summary ?? metrics.meetings_tracked}
		<p class="footer-stats">
			{#if byType}
				{compact(byType.city)} cities &middot; {compact(byType.county)} counties &middot; {compact(byType.school_district)} school districts &middot; {compact(meetingCount)} meetings
			{:else}
				Tracking {compact(metrics.live_jurisdictions ?? metrics.cities_covered)} jurisdictions &bull; {compact(meetingCount)} meetings analyzed
			{/if}
		</p>
	{/if}

	<p class="footer-tagline">made with love and rizz</p>
</footer>

<style>
	.site-footer {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-sm);
		padding: var(--space-2xl) 0 var(--space-xl);
		margin-top: var(--space-3xl);
		border-top: 1px solid var(--border-primary);
	}

	.footer-brand {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--text-secondary);
		text-align: center;
	}

	.footer-links {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		justify-content: center;
	}

	.footer-link {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--civic-blue);
		text-decoration: none;
		transition: color var(--transition-fast);
	}

	.footer-link:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	.footer-sep {
		color: var(--civic-gray);
		font-size: 0.7rem;
		opacity: 0.4;
	}

	.footer-stats {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--text-secondary);
		margin: 0;
		opacity: 0.7;
	}

	.footer-tagline {
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--text-secondary);
		margin: 0;
		opacity: 0.5;
	}
</style>
