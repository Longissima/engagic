import { createServerApiClient } from '$lib/api/server';
import type { PlatformMetrics } from '$lib/api/types';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ setHeaders, locals }) => {
	setHeaders({
		'cache-control': 'public, max-age=300'
	});

	const apiClient = createServerApiClient(locals.clientIp, locals.ssrAuthSecret, locals.userAgent);

	let platformMetrics: PlatformMetrics | null = null;
	try {
		platformMetrics = await apiClient.getPlatformMetrics();
	} catch (error) {
		console.error('Failed to load about/general coverage:', error);
	}

	return { platformMetrics };
};
