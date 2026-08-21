// Configuration with environment variable support

export const config = {
	apiBaseUrl: import.meta.env.VITE_API_BASE_URL || 'https://api.engagic.org',
	maxRetries: 2,
	retryDelay: 500,
	requestTimeout: 5000,
	aggregateRequestTimeout: 20000,
	debounceDelay: 300,
} as const;

export const errorMessages = {
	network: 'Connection error. Please check your internet and try again.',
	rateLimit: 'Too many requests. Please wait a moment and try again.',
	notFound: 'No meetings found for this location.',
	noAgenda: 'Agenda not yet available. Packets are typically posted within 48 hours of the meeting.',
	generic: 'Something went wrong. Please try again.',
	timeout: 'Request timed out. Please try again.',
} as const;
