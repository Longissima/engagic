<script lang="ts">
	import SeoHead from '$lib/components/SeoHead.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	const byType = $derived(data.analytics?.real_metrics.by_type?.total);
	const total = $derived(
		byType ? byType.city + byType.county + byType.school_district : null
	);
</script>

<SeoHead
	title="About Engagic - Help Shape Your City"
	description="Stay informed. Make your voice heard. AI summaries of local government meetings and civic participation information."
	url="https://engagic.org/about/general"
/>

<article class="about-content">
	<section class="section">
		<h1 class="page-title">Our Mission</h1>
		<p class="lead">Help you stay informed and make your voice heard in local government.</p>
		<p>We track public meetings of local government across the US — city councils, county boards, school districts — summarize what's being decided, and show you exactly how to weigh in: call, email, or Zoom.</p>
	</section>

	<section class="section">
		<div class="section-rule">What We Cover</div>
		<p>"Local government" isn't just city hall. The decisions that shape your block come from a patchwork of <strong>jurisdictions</strong>: cities and towns, counties, school districts, and special-purpose boards. Engagic treats them all as first-class.</p>
		{#if byType && total}
			<p>Right now we're tracking <strong>{total.toLocaleString()}</strong> jurisdictions: <strong>{byType.city.toLocaleString()}</strong> cities, <strong>{byType.county.toLocaleString()}</strong> counties, and <strong>{byType.school_district.toLocaleString()}</strong> school districts.</p>
		{/if}
	</section>

	<section class="section">
		<div class="section-rule">How It Works</div>
		<div class="feature-list">
			<div class="feature-item">
				<h3 class="feature-heading">Find What Matters</h3>
				{#if total}
					<p>We track {total.toLocaleString()}+ jurisdictions, pulling meeting agendas and documents so you don't have to hunt across government websites.</p>
				{:else}
					<p>We track jurisdictions across the US, pulling meeting agendas and documents so you don't have to hunt across government websites.</p>
				{/if}
			</div>
			<div class="feature-item">
				<h3 class="feature-heading">Make It Understandable</h3>
				<p>AI extracts the key decisions - budget changes, zoning proposals, public hearings - in plain language.</p>
			</div>
			<div class="feature-item">
				<h3 class="feature-heading">Show You How to Participate</h3>
				<p>Every meeting includes contact info: phone numbers, email addresses, Zoom links. Everything you need to have your say.</p>
			</div>
		</div>
		<p>Search your zip code and know what's happening - and how to speak up.</p>
	</section>

	<section class="section">
		<div class="section-rule">Our Principles</div>
		<div class="principles-list">
			<div class="principle">
				<h3 class="principle-heading">Open Source</h3>
				<p>All our code is publicly available and auditable (AGPL-3.0). Democracy should be transparent, and so should the tools that support it.</p>
			</div>

			<div class="principle">
				<h3 class="principle-heading">No Agenda</h3>
				<p>We don't editorialize or take political positions. We just make information accessible. Our AI summarizes meetings factually.</p>
			</div>

			<div class="principle">
				<h3 class="principle-heading">Privacy First</h3>
				<p>We don't track users or collect personal data. Civic engagement shouldn't require surveillance. Search freely, participate freely.</p>
			</div>

			<div class="principle">
				<h3 class="principle-heading">Public Data Stays Public</h3>
				<p>Government meeting information belongs to everyone. We built this infrastructure to make that access real, not theoretical.</p>
			</div>
		</div>
	</section>

	<section class="section">
		<div class="section-rule">The Philosophy</div>
		<p class="philosophy-lead">Civic data should be open. Infrastructure costs money. We're balancing both.</p>

		<div class="use-case-list">
			<p>Individual checking your city's meetings &mdash; <strong>use it freely</strong></p>
			<p>Nonprofit fighting for housing justice &mdash; <strong>let's partner</strong></p>
			<p>Journalist covering local government &mdash; <strong>we're here to help</strong></p>
			<p>Researcher studying civic engagement &mdash; <strong>the data is yours</strong></p>
		</div>

		<p class="contact-footer">
			<strong>Questions?</strong> <a href="mailto:hello@engagic.org">hello@engagic.org</a> - We're humans. Let's talk.
		</p>
	</section>
</article>

<style>
	.about-content {
		display: flex;
		flex-direction: column;
		gap: var(--space-2xl);
		padding-bottom: var(--space-xl);
		color: var(--text-primary);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}

	.page-title {
		font-family: var(--font-display);
		font-size: clamp(2rem, 5vw, 3rem);
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
		line-height: 1.1;
		letter-spacing: -0.02em;
	}

	.section-rule {
		font-family: var(--font-body);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--civic-gray);
		padding-bottom: 0.5rem;
		border-bottom: 2px solid var(--text-primary);
		margin: 0;
	}

	.lead {
		font-family: var(--font-display);
		font-size: 1.35rem;
		line-height: 1.4;
		color: var(--text-primary);
		margin: 0;
	}

	.about-content p {
		font-size: 1.05rem;
		line-height: 1.7;
		color: var(--text-secondary);
		margin: 0;
	}

	.feature-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-lg);
		padding-left: var(--space-md);
		border-left: 2px solid var(--border-primary);
	}

	.feature-item {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}

	.feature-heading {
		font-family: var(--font-display);
		font-size: 1.2rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
	}

	.feature-item p {
		font-size: 1.05rem;
		line-height: 1.7;
	}

	.principles-list {
		display: flex;
		flex-direction: column;
	}

	.principle {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		padding: var(--space-lg) 0;
		border-bottom: 1px solid var(--border-primary);
	}

	.principle:last-child {
		border-bottom: none;
	}

	.principle-heading {
		font-family: var(--font-display);
		font-size: 1.15rem;
		font-weight: 400;
		color: var(--text-primary);
		margin: 0;
	}

	.principle p {
		font-size: 1.05rem;
		line-height: 1.7;
		color: var(--text-secondary);
	}

	.philosophy-lead {
		font-family: var(--font-display);
		font-size: 1.3rem;
		font-weight: 400;
		color: var(--text-primary);
		line-height: 1.5;
		font-style: italic;
	}

	.use-case-list {
		display: flex;
		flex-direction: column;
	}

	.use-case-list p {
		font-size: 1.05rem;
		line-height: 1.7;
		padding: var(--space-sm) 0;
		border-bottom: 1px solid var(--border-primary);
		color: var(--text-secondary);
	}

	.use-case-list p:last-child {
		border-bottom: none;
	}

	.use-case-list strong {
		color: var(--civic-blue);
		font-weight: 600;
	}

	.contact-footer {
		font-size: 1.05rem;
		line-height: 1.7;
		padding-top: var(--space-lg);
		border-top: 1px solid var(--border-primary);
		color: var(--text-secondary);
	}

	.contact-footer a {
		color: var(--civic-blue);
		text-decoration: none;
		font-weight: 600;
		transition: color var(--transition-fast);
	}

	.contact-footer a:hover {
		color: var(--civic-accent);
		text-decoration: underline;
	}

	@media (max-width: 768px) {
		.page-title {
			font-size: 2rem;
		}

		.lead {
			font-size: 1.15rem;
		}

		.about-content p {
			font-size: 1rem;
		}
	}
</style>
