<script lang="ts">
	import { goto } from '$app/navigation';
	import { Protocol } from 'pmtiles';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { config } from '$lib/api/config';

	interface Props {
		tilesUrl?: string;
	}

	let { tilesUrl = '/tiles/cities.pmtiles' }: Props = $props();

	type CoverageType = 'matter' | 'item' | 'monolithic' | 'synced' | 'pending';

	let mapContainer: HTMLDivElement;
	let map: maplibregl.Map | null = $state(null);
	let hoveredCity: { name: string; state: string; coverage_type: CoverageType; summary_count: number } | null = $state(null);
	let cityStats: Record<string, { t: CoverageType; c: number }> = {};

	const coverageLabels: Record<CoverageType, string> = {
		matter: 'Matter-level summaries',
		item: 'Item-level summaries',
		monolithic: 'Meeting-level summaries',
		synced: 'Meetings synced',
		pending: 'No data yet'
	};

	// Theme color palettes - MapLibre requires actual hex values, not CSS vars.
	// Five-tier coverage palette: deep green (richest) -> gray (pending).
	const themes = {
		light: {
			background: '#faf9f5',
			stateBorder: '#d5d0c6',
			countryFill: '#f3f1ea',
			hover: '#c87a3f',
			outline: '#8a897e',
			tiers: {
				matter: '#3d8b55',
				item: '#7ab368',
				monolithic: '#b5642a',
				synced: '#d4a574',
				pending: '#e4e2da'
			} as Record<CoverageType, string>
		},
		dark: {
			background: '#1a1918',
			stateBorder: '#3d3a36',
			countryFill: '#141312',
			hover: '#e09258',
			outline: '#6b6860',
			tiers: {
				matter: '#6aad5e',
				item: '#8fc47e',
				monolithic: '#d4874d',
				synced: '#b38c6a',
				pending: '#302e2b'
			} as Record<CoverageType, string>
		}
	};

	// Paint expression: coverage_type -> fill color, feature-state takes precedence
	// over tile-baked value so post-regen tier changes show up live.
	function fillColorExpr(tiers: Record<CoverageType, string>): maplibregl.ExpressionSpecification {
		return [
			'match',
			['coalesce', ['feature-state', 'coverage_type'], ['get', 'coverage_type'], 'pending'],
			'matter', tiers.matter,
			'item', tiers.item,
			'monolithic', tiers.monolithic,
			'synced', tiers.synced,
			'pending', tiers.pending,
			tiers.pending
		] as maplibregl.ExpressionSpecification;
	}

	function getTheme(): 'light' | 'dark' {
		if (typeof document === 'undefined') return 'light';
		return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
	}

	function buildStyle(colors: typeof themes.light): maplibregl.StyleSpecification {
		return {
			version: 8,
			glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
			sources: {
				cities: {
					type: 'vector',
					url: `pmtiles://${tilesUrl}`,
					promoteId: 'banana'
				},
				// US state boundaries (bundled locally for performance)
				states: {
					type: 'geojson',
					data: '/us-states.json'
				}
			},
			layers: [
				{
					id: 'background',
					type: 'background',
					paint: {
						'background-color': colors.countryFill
					}
				},
				// US states fill (gives the country shape)
				{
					id: 'states-fill',
					type: 'fill',
					source: 'states',
					paint: {
						'fill-color': colors.background,
						'fill-opacity': 1
					}
				},
				// State boundaries
				{
					id: 'state-borders',
					type: 'line',
					source: 'states',
					paint: {
						'line-color': colors.stateBorder,
						'line-width': 1,
						'line-opacity': 0.4
					}
				},
				{
					id: 'jurisdiction-fill',
					type: 'fill',
					source: 'cities',
					'source-layer': 'cities',
					paint: {
						'fill-color': fillColorExpr(colors.tiers),
						// Opacity grows with the depth of coverage, with a small
						// boost from summary_count so heavily-covered cities pop.
						'fill-opacity': [
							'interpolate',
							['linear'],
							['coalesce', ['feature-state', 'summary_count'], ['get', 'summary_count'], 0],
							0, [
								'match',
								['coalesce', ['feature-state', 'coverage_type'], ['get', 'coverage_type'], 'pending'],
								'matter', 0.5,
								'item', 0.45,
								'monolithic', 0.4,
								'synced', 0.35,
								0.35
							],
							50, 0.7,
							200, 0.85
						]
					}
				},
				{
					id: 'jurisdiction-outline',
					type: 'line',
					source: 'cities',
					'source-layer': 'cities',
					paint: {
						'line-color': colors.outline,
						'line-width': [
							'interpolate',
							['linear'],
							['zoom'],
							3, 0.3,
							8, 1,
							12, 2
						],
						'line-opacity': 0.4
					}
				},
				{
					id: 'jurisdiction-hover',
					type: 'fill',
					source: 'cities',
					'source-layer': 'cities',
					filter: ['==', ['get', 'banana'], ''],
					paint: {
						'fill-color': colors.hover,
						'fill-opacity': 0.5
					}
				}
			]
		};
	}

	// Update layer paint properties when theme changes
	function applyTheme(mapInstance: maplibregl.Map, colors: typeof themes.light) {
		mapInstance.setPaintProperty('background', 'background-color', colors.countryFill);
		mapInstance.setPaintProperty('states-fill', 'fill-color', colors.background);
		mapInstance.setPaintProperty('state-borders', 'line-color', colors.stateBorder);
		mapInstance.setPaintProperty('jurisdiction-fill', 'fill-color', fillColorExpr(colors.tiers));
		mapInstance.setPaintProperty('jurisdiction-outline', 'line-color', colors.outline);
		mapInstance.setPaintProperty('jurisdiction-hover', 'fill-color', colors.hover);
	}

	// Map initialization effect
	$effect(() => {
		if (!mapContainer) return;

		// Register PMTiles protocol
		const protocol = new Protocol();
		maplibregl.addProtocol('pmtiles', protocol.tile);

		// Get initial theme and build style
		const initialTheme = getTheme();
		const initialColors = themes[initialTheme];

		// Create map with theme-aware style
		const mapInstance = new maplibregl.Map({
			container: mapContainer,
			style: buildStyle(initialColors),
			center: [-98.5, 39.8],
			zoom: 4,
			maxBounds: [[-130, 24], [-65, 50]],
			minZoom: 3,
			maxZoom: 12
		});

		// Navigation controls
		mapInstance.addControl(new maplibregl.NavigationControl(), 'top-right');

		// Fetch live stats and overlay via feature-state. Paint reads feature-state
		// preferentially, so a city whose tier advanced since the last tile regen
		// repaints immediately without waiting for a fresh PMTiles build.
		mapInstance.on('load', async () => {
			try {
				const res = await fetch(`${config.apiBaseUrl}/api/map-stats`);
				if (!res.ok) return;
				const stats: Record<string, { t: CoverageType; c: number }> = await res.json();
				cityStats = stats;
				for (const [banana, s] of Object.entries(stats)) {
					mapInstance.setFeatureState(
						{ source: 'cities', sourceLayer: 'cities', id: banana },
						{ coverage_type: s.t, summary_count: s.c }
					);
				}
			} catch {
				// Graceful degradation -- tile-baked data remains
			}
		});

		// Click to navigate to city page (pending cities are non-interactive)
		mapInstance.on('click', 'jurisdiction-fill', (e) => {
			const feat = e.features?.[0];
			if (!feat) return;
			const banana = feat.properties?.banana;
			const live = cityStats[banana];
			const tier = live?.t ?? feat.properties?.coverage_type ?? 'pending';
			if (banana && tier !== 'pending') {
				goto(`/${banana}`);
			}
		});

		function handleHover(e: maplibregl.MapMouseEvent & { features?: maplibregl.GeoJSONFeature[] }) {
			const feat = e.features?.[0];
			if (!feat) return;
			const props = feat.properties ?? {};
			const banana = props.banana || '';
			const live = cityStats[banana];
			const tier = (live?.t ?? props.coverage_type ?? 'pending') as CoverageType;
			mapInstance.getCanvas().style.cursor = tier === 'pending' ? '' : 'pointer';
			hoveredCity = {
				name: props.name || 'Unknown',
				state: props.state || '',
				coverage_type: tier,
				summary_count: live?.c ?? props.summary_count ?? 0
			};
			mapInstance.setFilter('jurisdiction-hover', ['==', ['get', 'banana'], banana]);
		}

		mapInstance.on('mousemove', 'jurisdiction-fill', handleHover);
		mapInstance.on('mouseleave', 'jurisdiction-fill', () => {
			mapInstance.getCanvas().style.cursor = '';
			hoveredCity = null;
			mapInstance.setFilter('jurisdiction-hover', ['==', ['get', 'banana'], '']);
		});

		// Watch for theme changes via MutationObserver on <html> element
		let currentTheme = initialTheme;
		const observer = new MutationObserver(() => {
			const newTheme = getTheme();
			if (newTheme !== currentTheme) {
				currentTheme = newTheme;
				applyTheme(mapInstance, themes[newTheme]);
			}
		});

		observer.observe(document.documentElement, {
			attributes: true,
			attributeFilter: ['class']
		});

		map = mapInstance;

		// Teardown
		return () => {
			observer.disconnect();
			mapInstance.remove();
			maplibregl.removeProtocol('pmtiles');
		};
	});
</script>

<div class="map-wrapper">
	<div bind:this={mapContainer} class="map-container"></div>

	{#if hoveredCity}
		<div class="tooltip">
			<div class="tooltip-city">{hoveredCity.name}, {hoveredCity.state}</div>
			<div class="tooltip-stats">
				{coverageLabels[hoveredCity.coverage_type]}{#if hoveredCity.summary_count > 0} &middot; {hoveredCity.summary_count.toLocaleString()}{/if}
			</div>
		</div>
	{/if}

	<div class="legend">
		<div class="legend-title">Coverage</div>
		<div class="legend-item">
			<span class="legend-swatch tier-matter"></span>
			<span>Matter-level</span>
		</div>
		<div class="legend-item">
			<span class="legend-swatch tier-item"></span>
			<span>Item-level</span>
		</div>
		<div class="legend-item">
			<span class="legend-swatch tier-monolithic"></span>
			<span>Meeting-level</span>
		</div>
		<div class="legend-item">
			<span class="legend-swatch tier-synced"></span>
			<span>Synced</span>
		</div>
		<div class="legend-item">
			<span class="legend-swatch tier-pending"></span>
			<span>Pending</span>
		</div>
	</div>
</div>

<style>
	.map-wrapper {
		position: relative;
		width: 100%;
		height: 100%;
	}

	.map-container {
		width: 100%;
		height: 100%;
		background: var(--surface-secondary);
		border-radius: var(--radius-lg);
		overflow: hidden;
	}

	.tooltip {
		position: absolute;
		top: var(--space-md);
		left: var(--space-md);
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		padding: var(--space-sm) var(--space-md);
		box-shadow: 0 2px 8px var(--shadow-md);
		pointer-events: none;
		z-index: 10;
	}

	.tooltip-city {
		font-family: var(--font-mono);
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.tooltip-stats {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--text-secondary);
		margin-top: 0.25rem;
	}

	.legend {
		position: absolute;
		bottom: var(--space-md);
		left: var(--space-md);
		background: var(--surface-primary);
		border: 1px solid var(--border-primary);
		border-radius: var(--radius-md);
		padding: var(--space-sm) var(--space-md);
		box-shadow: 0 2px 8px var(--shadow-md);
		z-index: 10;
	}

	.legend-title {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--text-primary);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: var(--space-xs);
	}

	.legend-item {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
		font-family: var(--font-body);
		font-size: 0.8rem;
		color: var(--text-secondary);
		margin-top: 0.25rem;
	}

	.legend-swatch {
		display: inline-block;
		width: 12px;
		height: 12px;
		border-radius: 2px;
	}

	.tier-matter { background: #3d8b55; opacity: 0.85; }
	.tier-item { background: #7ab368; opacity: 0.8; }
	.tier-monolithic { background: #b5642a; opacity: 0.75; }
	.tier-synced { background: #d4a574; opacity: 0.7; }
	.tier-pending { background: var(--border-primary); opacity: 0.5; }

	:global(.dark) .tier-matter { background: #6aad5e; }
	:global(.dark) .tier-item { background: #8fc47e; }
	:global(.dark) .tier-monolithic { background: #d4874d; }
	:global(.dark) .tier-synced { background: #b38c6a; }

	/* MapLibre overrides for dark mode */
	:global(.maplibregl-ctrl-group) {
		background: var(--surface-primary) !important;
		border: 1px solid var(--border-primary) !important;
	}

	:global(.maplibregl-ctrl-group button) {
		background-color: var(--surface-primary) !important;
	}

	:global(.maplibregl-ctrl-group button + button) {
		border-top: 1px solid var(--border-primary) !important;
	}

	:global(.maplibregl-ctrl button:not(:disabled):hover) {
		background-color: var(--surface-hover) !important;
	}
</style>
