import { createServerApiClient } from '$lib/api/server';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ setHeaders, locals }) => {
	const apiClient = createServerApiClient(locals.clientIp, locals.ssrAuthSecret);
	const [analytics, platformMetrics] = await Promise.all([
		apiClient.getAnalytics().catch((error) => {
			console.error('Failed to load analytics:', error);
			return null;
		}),
		apiClient.getPlatformMetrics().catch((error) => {
			console.error('Failed to load platform metrics:', error);
			return null;
		})
	]);

	// Coverage is only needed after the visitor selects that tab. Avoid making
	// every overview render pay for another whole-table aggregate.
	setHeaders({
		'cache-control': analytics || platformMetrics
			? 'public, max-age=300, stale-while-revalidate=300'
			: 'no-store'
	});

	return { analytics, platformMetrics, cityCoverage: null };
};
