import { createServerApiClient } from '$lib/api/server';
import type { AnalyticsData } from '$lib/api/types';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ setHeaders, locals }) => {
	setHeaders({
		'cache-control': 'public, max-age=300'
	});

	const apiClient = createServerApiClient(locals.clientIp, locals.ssrAuthSecret);

	let analytics: AnalyticsData | null = null;
	try {
		analytics = await apiClient.getAnalytics();
	} catch (error) {
		console.error('Failed to load about/general analytics:', error);
	}

	return { analytics };
};
