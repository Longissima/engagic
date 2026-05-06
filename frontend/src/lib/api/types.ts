// Discriminated unions for better type safety

export interface CityOption {
	city_name: string;
	state: string;
	banana: string;
	vendor: string;
	display_name: string;
	total_meetings: number;
	meetings_with_packet: number;
	summarized_meetings: number;
}

// City-level participation config for centralized testimony processes (e.g., NYC)
// When set, replaces meeting-level participation for testimony/contact info
export interface CityParticipation {
	testimony_url?: string;    // Portal to submit testimony (council.nyc.gov/testify/)
	testimony_email?: string;  // Official testimony email (testimony@council.nyc.gov)
	process_url?: string;      // Link to "how to testify" instructions
}

export interface AgendaItem {
	id: string;
	meeting_id: string;
	title: string;
	sequence: number;
	attachments: Array<{
		name: string;
		url: string;
		type: string;
		portal_url?: string;  // Stable viewer URL (CivicClerk portal)
		history_id?: string;  // PrimeGov-specific
	}>;
	summary?: string;
	topics?: string[];
	created_at?: string;
	// Matter tracking (Nov 2025)
	matter_id?: string;  // Composite hash (FK to city_matters)
	matter_file?: string;  // Official public identifier (BL2025-1005, 25-1209, etc.)
	matter_type?: string;  // Ordinance, Resolution, CD 12, etc.
	agenda_number?: string;  // Position on this agenda (1, K. 87, etc.)
	sponsors?: string[];  // Sponsor names
	matter?: Matter;  // Eagerly loaded Matter object (when load_matters=True in API)
}

export interface AgendaSource {
	type: 'agenda' | 'continuation' | 'special';
	url: string;
	label: string;
}

export interface Meeting {
	id: string;
	banana: string;
	title: string;
	date: string | null; // ISO format datetime or null when missing
	agenda_url?: string; // HTML/PDF agenda with extracted items (item-based, primary)
	agenda_sources?: AgendaSource[]; // Multi-agenda provenance [{type, url, label}]
	packet_url?: string | string[]; // Monolithic PDF fallback (no items extracted)
	summary?: string;
	meeting_status?: 'cancelled' | 'postponed' | 'revised' | 'rescheduled';
	participation?: {
		email?: string;
		emails?: Array<{
			address: string;
			purpose: string;
		}>;
		phone?: string;
		virtual_url?: string;
		meeting_id?: string;
		streaming_urls?: Array<{
			url?: string;
			platform: string;
			channel?: string;
		}>;
		is_hybrid?: boolean;
		is_virtual_only?: boolean;
		physical_location?: string;
	};
	topics?: string[];
	processing_status?: 'pending' | 'processing' | 'completed' | 'failed';
	has_items?: boolean; // True for item-based meetings, false for monolithic
	items?: AgendaItem[]; // Present for item-based meetings (58% of cities)
	committee_id?: string; // FK to committees (a meeting is an occurrence of a committee)
}

export interface RandomMeetingResponse {
	status: string;
	meeting: {
		id: string;
		banana: string;
		title: string;
		date: string;
		packet_url: string;
		summary: string;
		quality_score: number;
	};
}

export interface RandomMeetingWithItemsResponse {
	success: boolean;
	meeting: {
		id: string;
		banana: string;
		title: string;
		date: string;
		packet_url: string;
		item_count: number;
		avg_summary_length: number;
	};
}

// Discriminated union for search results
export type SearchResult = 
	| SearchSuccess
	| SearchAmbiguous
	| SearchError;

interface SearchSuccess {
	success: true;
	city_name: string;
	state: string;
	banana: string;
	vendor: string;
	vendor_display_name: string;
	source_url: string | null;
	source_urls?: Array<{ url: string; body: string }>;  // Multi-body sources (e.g. Granicus with multiple view_ids)
	participation?: CityParticipation;  // City-level testimony config (replaces meeting-level for NYC, etc.)
	meetings: Meeting[];
	cached: boolean;
	query: string;
	type: 'city' | 'zipcode' | 'state';
}

interface SearchAmbiguous {
	success: boolean;  // Can be true (state search found cities) or false (city not found)
	ambiguous: true;
	message: string;
	city_options: CityOption[];
	query: string;
	type?: 'city' | 'state';
	meetings?: Meeting[];
}

interface SearchError {
	success: false;
	ambiguous?: false;
	message: string;
	query?: string;
}

// Topic search types
export interface TopicSearchRequest {
	topic: string;
	banana?: string;
	limit?: number;
}

export interface TopicSearchResult {
	success: boolean;
	query: string;
	normalized_topic: string;
	display_name?: string;
	results: Array<{
		meeting: Meeting;
		matching_items?: AgendaItem[];
	}>;
	count: number;
	banana?: string;
	city_name?: string;
	state?: string;
}

export interface AnalyticsData {
	success: boolean;
	timestamp: string;
	real_metrics: {
		cities_covered: number;
		active_cities: number;
		by_type: {
			total: {
				city: number;
				county: number;
				school_district: number;
			};
			active: {
				city: number;
				county: number;
				school_district: number;
			};
			// "live" = jurisdictions with at least one summary at meeting/item/matter level.
			// Prefer this over `total` for user-facing coverage claims.
			live: {
				city: number;
				county: number;
				school_district: number;
			};
		};
		live_jurisdictions: number;
		frequently_updated_cities: number;
		frequently_updated_population: number;
		meetings_tracked: number;
		// meetings with a summary at meeting/item/matter level — the "live" meeting count
		meetings_with_summary: number;
		meetings_with_items: number;
		meetings_with_packet: number;
		agendas_summarized: number;
		agenda_items_processed: number;
		matters_tracked: number;
		unique_item_summaries: number;
		population_total: number;
		population_with_data: number;
		population_with_summaries: number;
	};
}

// Platform-wide metrics for impact page
export interface PlatformMetrics {
	status: string;
	content: {
		total_cities: number;
		active_cities: number;
		meetings: number;
		agenda_items: number;
		matters: number;
		matter_appearances: number;
	};
	civic_infrastructure: {
		committees: number;
		council_members: number;
		committee_assignments: number;
	};
	accountability: {
		votes: number;
		sponsorships: number;
		cities_with_votes: number;
		officials_with_votes: number;
		votes_by_city: Array<{
			city: string;
			votes: number;
			voters: number;
		}>;
	};
	processing: {
		summarized_meetings: number;
		summarized_items: number;
		filtered_items: number;
		items_analyzed: number;
		meeting_summary_rate: number;
		item_summary_rate: number;
	};
	growth: {
		meetings_30d: number;
		items_30d: number;
		matters_30d: number;
		votes_30d: number;
		// Summarized meetings whose meeting date fell in the last 30 days.
		meeting_summaries_30d: number;
	};
	trends: {
		meetings: number[];
		items: number[];
		matters: number[];
		votes: number[];
		summaries: number[];
	};
}

// Flyer generation types (mirrors backend Pydantic models)
export type FlyerPosition = 'support' | 'oppose' | 'more_info';

export interface FlyerRequest {
	meeting_id: string;
	item_id: string | null;
	position: FlyerPosition;
	custom_message: string | null;
	user_name: string | null;
}

export interface FlyerConstraints {
	readonly MAX_MESSAGE_LENGTH: 500;
	readonly MAX_NAME_LENGTH: 100;
	readonly POSITIONS: readonly FlyerPosition[];
}

export const FLYER_CONSTRAINTS: FlyerConstraints = {
	MAX_MESSAGE_LENGTH: 500,
	MAX_NAME_LENGTH: 100,
	POSITIONS: ['support', 'oppose', 'more_info'] as const,
} as const;

// Error types
export class ApiError extends Error {
	constructor(
		message: string,
		public statusCode: number,
		public isRetryable: boolean = false
	) {
		super(message);
		this.name = 'ApiError';
	}
}

export class NetworkError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'NetworkError';
	}
}

// Type guards
export function isSearchSuccess(result: SearchResult): result is SearchSuccess {
	return result.success === true;
}

export function isSearchAmbiguous(result: SearchResult): result is SearchAmbiguous {
	return result.success === false && result.ambiguous === true;
}

export function isSearchError(result: SearchResult): result is SearchError {
	return result.success === false && !result.ambiguous;
}

// Matter status values
export type MatterStatus = 'active' | 'passed' | 'failed' | 'tabled' | 'withdrawn' | 'referred' | 'amended' | 'vetoed' | 'enacted';

// Matter types
export interface Matter {
	id: string;
	banana: string;  // City identifier (FK to cities)
	matter_id?: string;  // Raw vendor ID (UUID, numeric, etc.)
	matter_file?: string;  // Official public identifier (25-1234, BL2025-1098)
	matter_type?: string;
	title?: string;  // May be null for legacy data
	canonical_summary?: string;
	canonical_topics?: string[];
	sponsors?: string[];
	attachments?: Array<{ name: string; url: string; type: string; history_id?: string }>;
	first_seen: string;
	last_seen: string;
	appearance_count?: number;  // Number of times this matter appeared across meetings
	status?: MatterStatus;  // Legislative disposition
	final_vote_date?: string;  // Date when matter reached terminal disposition
	quality_score?: number;  // Denormalized from ratings (1-5 scale)
	rating_count?: number;  // Count of ratings received
}

export interface MatterTimelineAppearance {
	item_id: string;
	meeting_id: string;
	meeting_title: string;
	meeting_date: string;
	city_name: string;
	state: string;
	banana: string;
	agenda_number?: string;
	summary?: string;
	topics?: string[];
	committee?: string;
	committee_id?: string;
	vote_outcome?: VoteOutcome;
	vote_tally?: VoteTally;
}

export interface MatterTimelineResponse {
	success: boolean;
	matter: Matter;
	timeline: MatterTimelineAppearance[];
	appearance_count: number;
}

export interface GetMeetingResponse {
	success: boolean;
	meeting: Meeting;
	city_name: string | null;
	state: string | null;
	banana: string;
	participation?: CityParticipation;  // City-level testimony config
}

export interface MatterSummary {
	id: string;
	matter_file?: string;
	matter_type?: string;
	title: string;
	canonical_summary?: string;
	canonical_topics?: string;
	appearance_count: number;
	first_seen: string;
	last_seen: string;
}

export interface GetCityMattersResponse {
	success: boolean;
	city_name: string;
	state: string;
	banana: string;
	matters: MatterSummary[];
	total_count: number;
	limit: number;
	offset: number;
}

export interface StateMatterSummary {
	id: string;
	matter_file?: string;
	matter_type?: string;
	title: string;
	canonical_summary?: string;
	canonical_topics?: string;
	appearance_count: number;
	first_seen: string;
	last_seen: string;
	city_name: string;
	banana: string;
	state: string;
}

export interface TopCityStats {
	name: string;
	banana: string;
	matter_count: number;
	meeting_count: number;
	summarized_items: number;
}

export interface GetStateMattersResponse {
	success: boolean;
	state: string;
	cities_count: number;
	cities: Array<{
		banana: string;
		name: string;
		vendor: string;
	}>;
	matters: StateMatterSummary[];
	total_matters: number;
	topic_distribution: Record<string, number>;
	filtered_by_topic?: string;
	top_cities: TopCityStats[];
	meeting_stats: {
		total_meetings: number;
		with_agendas: number;
		with_summaries: number;
	};
}

// City-scoped search response types with rich results
export interface CitySearchItemResult {
	type: 'item';
	item_id: string;
	item_title?: string;
	item_sequence?: number;
	agenda_number?: string;
	summary?: string;
	topics?: string[];
	matter_id?: string;
	matter_file?: string;
	matter_type?: string;
	attachments?: Array<{ name: string; url: string; type: string }>;
	meeting_id: string;
	meeting_title?: string;
	meeting_date?: string;
	agenda_url?: string;
	agenda_sources?: AgendaSource[];
	context: string; // Snippet centered on keyword
}

export interface CitySearchMatterResult {
	type: 'matter';
	id: string;
	banana: string;
	matter_id?: string;
	matter_file?: string;
	matter_type?: string;
	title?: string;
	canonical_summary?: string;
	canonical_topics?: string[];
	sponsors?: string[];
	attachments?: Array<{ name: string; url: string; type: string }>;
	first_seen: string;
	last_seen: string;
	appearance_count?: number;
	context: string; // Snippet centered on keyword
}

export type CitySearchResult = CitySearchItemResult | CitySearchMatterResult;

export interface SearchCityMeetingsResponse {
	success: boolean;
	query: string;
	banana: string;
	results: CitySearchItemResult[];
	count: number;
}

export interface SearchCityMattersResponse {
	success: boolean;
	query: string;
	banana: string;
	results: CitySearchMatterResult[];
	count: number;
}

// Vote types
export type VoteValue = 'yes' | 'no' | 'abstain' | 'absent' | 'present' | 'recused' | 'not_voting';
export type VoteOutcome = 'passed' | 'failed' | 'tabled' | 'withdrawn' | 'referred' | 'amended' | 'unknown' | 'no_vote';

export interface VoteTally {
	yes: number;
	no: number;
	abstain: number;
	absent: number;
	present?: number;
}

export interface Vote {
	id: number;
	council_member_id: string;
	matter_id: string;
	meeting_id: string;
	vote: VoteValue;
	vote_date: string;
	sequence?: number;
	metadata?: Record<string, unknown>;
	created_at?: string;
}

export interface MeetingVoteGroup {
	meeting_id: string;
	meeting_title?: string;
	meeting_date?: string;
	committee?: string;
	committee_id?: string;
	vote_outcome?: VoteOutcome;
	vote_tally?: VoteTally;
	computed_tally?: VoteTally;
	votes: Vote[];
}

export interface MatterVoteOutcome {
	meeting_id: string;
	meeting_title: string;
	date?: string;
	outcome: VoteOutcome;
	tally: VoteTally;
}

export interface MatterVotesResponse {
	success: boolean;
	matter_id: string;
	matter_title: string;
	votes: Vote[];
	votes_by_meeting?: MeetingVoteGroup[];
	tally: VoteTally;
	outcomes: MatterVoteOutcome[];
}

export interface MatterSponsorsResponse {
	success: boolean;
	matter_id: string;
	sponsors: CouncilMember[];
	total: number;
}

export interface MeetingVoteMatter {
	matter_id: string;
	matter_title: string;
	matter_file?: string;
	votes: Vote[];
	tally: VoteTally;
	outcome: VoteOutcome;
}

export interface MeetingVotesResponse {
	success: boolean;
	meeting_id: string;
	meeting_title: string;
	meeting_date?: string;
	matters_with_votes: MeetingVoteMatter[];
	total: number;
}

// Council Member types
export type CouncilMemberStatus = 'active' | 'former' | 'unknown';

export interface CouncilMember {
	id: string;
	banana: string;
	name: string;
	normalized_name?: string;
	title?: string;
	district?: string;
	status: CouncilMemberStatus;
	first_seen: string;
	last_seen: string;
	sponsorship_count: number;
	vote_count: number;
	metadata?: Record<string, unknown>;
	created_at?: string;
	updated_at?: string;
}

export interface CouncilRosterResponse {
	success: boolean;
	city_name: string;
	state: string;
	banana: string;
	council_members: CouncilMember[];
	total: number;
}

export interface VoteRecord {
	id: number;
	matter_id: string;
	meeting_id: string;
	vote: VoteValue;
	vote_date: string;
	sequence?: number;
	matter_file?: string;
	title: string;
	matter_type?: string;
}

export interface VotingRecordResponse {
	success: boolean;
	member: CouncilMember;
	voting_record: VoteRecord[];
	total: number;
	statistics: VoteTally;
}

// Committee types
export type CommitteeStatus = 'active' | 'inactive' | 'unknown';

export interface Committee {
	id: string;
	name: string;
	description?: string;
	status: CommitteeStatus;
	banana: string;
	member_count?: number;
	created_at?: string;
}

export interface CommitteeMember {
	id: number;
	committee_id: string;
	council_member_id: string;
	role?: string;
	joined_at?: string;
	left_at?: string;
	member_name: string;
	title?: string;
	district?: string;
}

export interface CommitteeAssignment {
	id: number;
	committee_id: string;
	committee_name: string;
	committee_status: CommitteeStatus;
	role?: string;
	joined_at?: string;
	left_at?: string;
}

export interface CommitteeVoteRecord {
	matter_id: string;
	meeting_id: string;
	item_id: string;
	appeared_at?: string;
	vote_outcome?: VoteOutcome;
	vote_tally?: VoteTally;
	matter_file?: string;
	matter_title: string;
}

export interface CityCommitteesResponse {
	success: boolean;
	city_name: string;
	state: string;
	banana: string;
	committees: Committee[];
	total: number;
}

export interface CommitteeDetailResponse {
	success: boolean;
	committee: Committee;
	city_name?: string;
	state?: string;
	members: CommitteeMember[];
	member_count: number;
}

export interface CommitteeMembersResponse {
	success: boolean;
	committee_id: string;
	committee_name: string;
	as_of?: string;
	members: CommitteeMember[];
	total: number;
}

export interface CommitteeVotesResponse {
	success: boolean;
	committee_id: string;
	committee_name: string;
	votes: CommitteeVoteRecord[];
	total: number;
}

export interface MemberCommitteesResponse {
	success: boolean;
	member_id: string;
	member_name: string;
	committees: CommitteeAssignment[];
	total: number;
}

// Rating types
export interface RatingStats {
	success: boolean;
	entity_type: string;
	entity_id: string;
	avg_rating: number;
	rating_count: number;
	distribution: Record<string, number>;
	user_rating?: number;  // Only if authenticated
}

export interface RatingSubmitResponse {
	success: boolean;
	status: string;
}

// Issue reporting types
export type IssueType = 'inaccurate' | 'incomplete' | 'misleading' | 'offensive' | 'other';
export type IssueStatus = 'open' | 'resolved' | 'dismissed';

export interface Issue {
	id: number;
	issue_type: IssueType;
	description: string;
	status: IssueStatus;
	created_at: string;
	resolved_at?: string;
}

export interface IssuesResponse {
	success: boolean;
	entity_type: string;
	entity_id: string;
	open_issue_count: number;
	issues: Issue[];
}

export interface ReportIssueResponse {
	success: boolean;
	issue_id: number;
}

// Engagement types
export interface EngagementStats {
	success: boolean;
	matter_id?: string;
	meeting_id?: string;
	watch_count: number;
	is_watching: boolean;
}

// Watch types
export interface Watch {
	id: number;
	entity_type: string;
	entity_id: string;
	created_at: string;
}

export interface WatchListResponse {
	success: boolean;
	watches: Watch[];
	total: number;
}

// Happening This Week types (Claude-analyzed important items)
export interface HappeningItem {
	item_id: string;
	meeting_id: string;
	meeting_date: string | null;
	meeting_title: string | null;
	rank: number;
	reason: string;
	item_title: string | null;
	item_summary: string | null;
	matter_file: string | null;
	agenda_number: string | null;
	participation: Meeting['participation'] | null;
	expires_at: string | null;
}

export interface HappeningResponse {
	success: boolean;
	banana: string;
	count: number;
	items: HappeningItem[];
}

// Global happening items (all cities)
export interface GlobalHappeningItem {
	banana: string;
	item_id: string;
	meeting_id: string;
	meeting_date: string | null;
	meeting_title: string | null;
	rank: number;
	reason: string;
	item_title: string | null;
	created_at: string | null;
}

export interface GlobalHappeningResponse {
	success: boolean;
	cities_count: number;
	cities: string[];
	items_count: number;
	items: GlobalHappeningItem[];
}

// Jurisdiction coverage types for transparency page
export type CoverageType = 'matter' | 'item' | 'monolithic' | 'synced' | 'pending';
export type JurisdictionType = 'city' | 'county' | 'school_district';

export interface CityWithCoverage {
	name: string;
	state: string;
	type: JurisdictionType;
	population: number;
	coverage_type: CoverageType;
	summary_count: number;
}

export interface CityCoverageSummary {
	matter: number;
	item: number;
	monolithic: number;
	synced: number;
	total: number;
	by_type?: {
		city: number;
		county: number;
		school_district: number;
	};
}

export interface CityCoverageResponse {
	success: boolean;
	timestamp: string;
	summary: CityCoverageSummary;
	cities: CityWithCoverage[];
}

// State meetings types
export interface StateMeeting extends Meeting {
	city_name: string;
	city_banana: string;
}

export interface GetStateMeetingsResponse {
	success: boolean;
	state: string;
	meetings: StateMeeting[];
	total: number;
}

// Civic infrastructure types for council-members and committees pages
export interface CivicInfrastructureCity {
	banana: string;
	city_name: string;
	state: string;
	population: number;
	council_member_count: number;
	vote_count: number;
	committee_count: number;
	assignment_count: number;
}

export interface CivicInfrastructureTotals {
	cities_with_council_members: number;
	cities_with_committees: number;
	total_council_members: number;
	total_votes: number;
	total_committees: number;
	total_assignments: number;
}

export interface CivicInfrastructureResponse {
	success: boolean;
	timestamp: string;
	cities: CivicInfrastructureCity[];
	totals: CivicInfrastructureTotals;
}