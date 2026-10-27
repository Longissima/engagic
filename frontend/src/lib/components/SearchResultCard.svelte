<script lang="ts">
	import { highlightMatch, sanitizeHtml } from '$lib/utils/sanitize';
	import { marked } from 'marked';
	import type { CitySearchResult, CitySearchItemResult, CitySearchMatterResult } from '$lib/api/types';
	import { generateMeetingSlug } from '$lib/utils/utils';
	import { generateAnchorId } from '$lib/utils/anchor';
	import { attachmentHref, attachmentKey, attachmentPagesLabel } from '$lib/utils/attachments';

	interface Props {
		result: CitySearchResult;
		query: string;
		cityUrl: string;
	}

	let { result, query, cityUrl }: Props = $props();
	let expanded = $state(false);

	function formatDate(dateStr: string | undefined): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}

	function isItemResult(r: CitySearchResult): r is CitySearchItemResult {
		return r.type === 'item';
	}

	function isMatterResult(r: CitySearchResult): r is CitySearchMatterResult {
		return r.type === 'matter';
	}

	function getItemLink(): string {
		if (isItemResult(result)) {
			const meeting = {
				id: result.meeting_id,
				title: result.meeting_title || '',
				date: result.meeting_date || ''
			};
			const slug = generateMeetingSlug(meeting as any);
			const anchor = generateAnchorId({
				id: result.item_id,
				agenda_number: result.agenda_number,
				matter_file: result.matter_file
			});
			return `/${cityUrl}/${slug}?item=${anchor}`;
		}
		return '';
	}

	function getHighlightedContext(): string {
		if (result.context?.includes('<mark>')) {
			return sanitizeHtml(result.context);
		}
		return highlightMatch(result.context, query);
	}

	// Memoized markdown rendering - computed once per result change
	const renderedSummary = $derived.by(() => {
		if (isItemResult(result) && result.summary) {
			return marked(result.summary) as string;
		}
		if (isMatterResult(result) && result.canonical_summary) {
			return marked(result.canonical_summary) as string;
		}
		return '';
	});

	const topics = $derived.by(() => {
		if (isItemResult(result)) {
			return result.topics || [];
		}
		if (isMatterResult(result)) {
			return result.canonical_topics || [];
		}
		return [];
	});

	const attachments = $derived.by(() => {
		return result.attachments || [];
	});
</script>

<div class="result-card" class:expanded>
	<button
		class="result-card-header"
		onclick={() => expanded = !expanded}
		aria-expanded={expanded}
	>
		<div class="result-content">
			<!-- Legislative Context Bar -->
			<div class="legislative-context">
				{#if isItemResult(result)}
					{#if result.item_sequence}
						<span class="agenda-position">Item {result.item_sequence}</span>
					{/if}
					{#if result.agenda_number}
						<span class="agenda-number">{result.agenda_number}</span>
					{/if}
				{/if}
				{#if result.matter_file}
					<span class="matter-badge">
						{#if result.matter_type}{result.matter_type}: {/if}{result.matter_file}
					</span>
				{/if}
				{#if attachments.length > 0}
					<span class="attachment-count">
						{attachments.length} doc{attachments.length !== 1 ? 's' : ''}
					</span>
				{/if}
			</div>

			<!-- Highlighted context snippet -->
			<p class="result-context">
				{@html getHighlightedContext()}
			</p>

			<!-- Title -->
			{#if isItemResult(result) && result.item_title}
				<h4 class="result-title">{result.item_title}</h4>
			{:else if isMatterResult(result) && result.title}
				<h4 class="result-title">{result.title}</h4>
			{/if}

			<!-- Meta info -->
			<div class="result-meta">
				{#if isItemResult(result)}
					{#if result.meeting_date}
						<span class="result-date">{formatDate(result.meeting_date)}</span>
						<span class="separator">-</span>
					{/if}
					{#if result.meeting_title}
						<span class="meeting-title">{result.meeting_title}</span>
					{/if}
				{:else if isMatterResult(result)}
					{#if result.appearance_count && result.appearance_count > 1}
						<span class="appearance-count">{result.appearance_count} appearances</span>
						<span class="separator">-</span>
					{/if}
					<span class="result-date">Last seen: {formatDate(result.last_seen)}</span>
				{/if}
			</div>
		</div>

		<div class="expand-icon" aria-hidden="true">
			{expanded ? '-' : '+'}
		</div>
	</button>

	{#if expanded}
		<div class="result-expanded">
			<!-- Topics -->
			{#if topics.length > 0}
				<div class="topics-row">
					{#each topics as topic (topic)}
						<span class="topic-tag">{topic}</span>
					{/each}
				</div>
			{/if}

			<!-- Sponsors (matters only) -->
			{#if isMatterResult(result) && result.sponsors && result.sponsors.length > 0}
				<div class="sponsors-row">
					<span class="label">Sponsors:</span>
					{#each result.sponsors as sponsor (sponsor)}
						<span class="sponsor-name">{sponsor}</span>
					{/each}
				</div>
			{/if}

			<!-- Full Summary -->
			{#if renderedSummary}
				<div class="full-summary">
					{@html renderedSummary}
				</div>
			{:else}
				<div class="no-summary">
					No summary available.
				</div>
			{/if}

			<!-- Attachments -->
			{#if attachments.length > 0}
				<div class="attachments-section">
					<span class="label">Documents:</span>
					<div class="attachment-list">
						{#each attachments as attachment (attachmentKey(attachment))}
							<a
								href={attachmentHref(attachment)}
								target="_blank"
								rel="noopener noreferrer"
								class="attachment-link"
								onclick={(e) => e.stopPropagation()}
							>
								{attachment.name || 'View Document'}{attachmentPagesLabel(attachment)}
							</a>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Actions -->
			<div class="result-actions">
				{#if isItemResult(result)}
					<a
						href={getItemLink()}
						class="action-link primary"
						onclick={(e) => e.stopPropagation()}
					>
						View Item
					</a>
					{#if result.agenda_url}
						<a
							href={result.agenda_url}
							target="_blank"
							rel="noopener noreferrer"
							class="action-link secondary"
							onclick={(e) => e.stopPropagation()}
						>
							Official Agenda
						</a>
					{/if}
				{:else if isMatterResult(result)}
					<a
						href="/matter/{result.id}"
						class="action-link primary"
						onclick={(e) => e.stopPropagation()}
						data-sveltekit-preload-data="tap"
					>
						View Matter
					</a>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.result-card {
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		overflow: hidden;
		transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
	}

	.result-card.expanded {
		border-color: var(--civic-blue);
		box-shadow: 0 4px 12px var(--shadow-md);
	}

	.result-card-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
		width: 100%;
		padding: 1rem;
		background: transparent;
		border: none;
		text-align: left;
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.result-card-header:hover {
		background: var(--surface-secondary);
	}

	.result-content {
		flex: 1;
		min-width: 0;
	}

	.legislative-context {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
		align-items: center;
		margin-bottom: 0.75rem;
	}

	.agenda-position {
		display: inline-block;
		padding: 2px 8px;
		background: var(--civic-blue);
		color: white;
		border-radius: var(--radius-xs);
		font-size: 0.75rem;
		font-weight: 600;
		font-family: var(--font-mono);
	}

	.agenda-number {
		display: inline-block;
		padding: 2px 8px;
		background: var(--surface-secondary);
		color: var(--text-secondary);
		border-radius: var(--radius-xs);
		font-size: 0.75rem;
		font-weight: 500;
		font-family: var(--font-mono);
	}

	.matter-badge {
		display: inline-block;
		padding: 2px 8px;
		background: var(--surface-hover);
		border: 1px solid var(--civic-blue);
		color: var(--civic-blue);
		border-radius: var(--radius-xs);
		font-size: 0.75rem;
		font-weight: 600;
		font-family: var(--font-mono);
	}

	.attachment-count {
		display: inline-block;
		padding: 2px 8px;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		color: var(--text-secondary);
		border-radius: var(--radius-xs);
		font-size: 0.75rem;
		font-weight: 500;
	}

	.result-context {
		margin: 0 0 0.75rem 0;
		line-height: 1.6;
		color: var(--text-primary);
		font-size: 1rem;
	}

	.result-context :global(mark) {
		background: var(--highlight-bg);
		color: var(--highlight-text);
		padding: 2px 4px;
		border-radius: 3px;
		font-weight: 500;
	}

	.result-title {
		font-size: 0.9rem;
		font-weight: 500;
		margin: 0 0 0.5rem 0;
		color: var(--text-secondary);
	}

	.result-meta {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
		align-items: center;
		font-size: 0.8rem;
		color: var(--civic-gray);
	}

	.separator {
		color: var(--civic-gray);
	}

	.meeting-title {
		color: var(--text-secondary);
	}

	.appearance-count {
		color: var(--civic-blue);
		font-weight: 500;
	}

	.expand-icon {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-xs);
		color: var(--text-primary);
		font-size: 1.25rem;
		font-weight: bold;
		transition: all var(--transition-fast);
	}

	.result-card-header:hover .expand-icon {
		background: var(--surface-hover);
		border-color: var(--civic-blue);
		color: var(--civic-blue);
	}

	/* Expanded section */
	.result-expanded {
		padding: 1rem;
		padding-top: 0.75rem;
		border-top: 1px solid var(--border-primary);
		animation: slideDown var(--transition-fast);
	}

	@keyframes slideDown {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.topics-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		margin-bottom: 0.75rem;
	}

	.topic-tag {
		display: inline-block;
		padding: 2px 8px;
		background: var(--surface-hover);
		color: var(--civic-blue);
		border-radius: var(--radius-pill);
		font-size: 0.75rem;
		font-weight: 500;
	}

	.sponsors-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
		margin-bottom: 0.75rem;
	}

	.label {
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--text-secondary);
	}

	.sponsor-name {
		display: inline-block;
		padding: 2px 8px;
		background: var(--surface-secondary);
		border: 1px solid var(--border-primary);
		color: var(--text-secondary);
		border-radius: var(--radius-xs);
		font-size: 0.75rem;
		font-weight: 500;
	}

	.full-summary {
		padding: 1rem;
		background: var(--surface-primary);
		border-left: 3px solid var(--civic-blue);
		border-radius: var(--radius-xs);
		margin-bottom: 0.75rem;
		font-size: 0.9rem;
		line-height: 1.7;
		color: var(--text-primary);
	}

	.full-summary :global(p) {
		margin: 0.5rem 0;
	}

	.full-summary :global(ul),
	.full-summary :global(ol) {
		margin: 0.5rem 0;
		padding-left: 1.5rem;
	}

	.full-summary :global(li) {
		margin: 0.25rem 0;
	}

	.full-summary :global(h1),
	.full-summary :global(h2),
	.full-summary :global(h3),
	.full-summary :global(h4),
	.full-summary :global(h5),
	.full-summary :global(h6) {
		font-family: var(--font-mono);
		font-weight: 600;
		color: var(--text-primary);
		margin: 1rem 0 0.5rem 0;
		line-height: 1.3;
	}

	.full-summary :global(h1) { font-size: 1.25rem; }
	.full-summary :global(h2) { font-size: 1.1rem; }
	.full-summary :global(h3) { font-size: 1rem; }
	.full-summary :global(h4),
	.full-summary :global(h5),
	.full-summary :global(h6) { font-size: 0.9rem; }

	.full-summary :global(blockquote) {
		margin: 0.75rem 0;
		padding: 0.5rem 1rem;
		border-left: 3px solid var(--civic-blue);
		background: var(--surface-secondary);
		font-style: italic;
	}

	.full-summary :global(code) {
		font-family: var(--font-mono);
		font-size: 0.85em;
		background: var(--surface-secondary);
		padding: 0.15rem 0.35rem;
		border-radius: var(--radius-xs);
	}

	.full-summary :global(pre) {
		margin: 0.75rem 0;
		padding: 1rem;
		background: var(--surface-secondary);
		border-radius: var(--radius-sm);
		overflow-x: auto;
	}

	.full-summary :global(pre code) {
		background: none;
		padding: 0;
	}

	.no-summary {
		padding: 1rem;
		text-align: center;
		color: var(--civic-gray);
		font-style: italic;
		background: var(--surface-secondary);
		border-radius: var(--radius-xs);
		margin-bottom: 0.75rem;
	}

	.attachments-section {
		margin-bottom: 0.75rem;
	}

	.attachment-list {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-top: 0.375rem;
	}

	.attachment-link {
		display: inline-block;
		padding: 6px 12px;
		background: var(--surface-primary);
		color: var(--civic-blue);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-xs);
		text-decoration: none;
		font-size: 0.8rem;
		font-weight: 500;
		transition: all var(--transition-fast);
	}

	.attachment-link:hover {
		background: var(--surface-secondary);
		border-color: var(--civic-blue);
	}

	.result-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-top: 0.75rem;
	}

	.action-link {
		display: inline-block;
		padding: 8px 16px;
		text-decoration: none;
		font-weight: 500;
		font-size: 0.85rem;
		border-radius: var(--radius-sm);
		transition: all var(--transition-fast);
	}

	.action-link.primary {
		background: var(--civic-blue);
		color: white;
	}

	.action-link.primary:hover {
		background: var(--civic-accent);
	}

	.action-link.secondary {
		background: var(--surface-secondary);
		color: var(--civic-blue);
		border: 1px solid var(--border-primary);
	}

	.action-link.secondary:hover {
		background: var(--surface-hover);
		border-color: var(--civic-blue);
	}

	@media (max-width: 640px) {
		.result-card-header {
			padding: 0.875rem;
		}

		.result-expanded {
			padding: 0.875rem;
		}

		.result-context {
			font-size: 0.9rem;
		}

		.result-meta {
			font-size: 0.75rem;
		}
	}
</style>
