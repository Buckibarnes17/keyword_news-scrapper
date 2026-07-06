/**
 * Typescript definitions mapping to Pydantic schemas in backend/schemas.py
 * and models in backend/models.py.
 */

export type MatchType = 'phrase' | 'boolean';
export type CrawlerEngine = 'fast' | 'dynamic' | 'lightpanda';
export type SourceType = 'search' | 'direct' | 'sitemap' | 'feed';
export type SearchStatus = 'pending' | 'processing' | 'completed' | 'failed' | 'aborted';
export type CrawlStatus = 'pending' | 'crawling' | 'matched' | 'skipped' | 'failed';
export type ScheduleFrequency = 'daily' | 'weekly' | 'monthly';

export interface SearchQueryCreate {
    keyword?: string;
    match_type?: MatchType;
    case_sensitive?: boolean;
    exact_match?: boolean;
    domains_filter?: {
        include?: string[];
        exclude?: string[];
    } | null;
    languages_filter?: string[] | null;
    date_range_start?: string | null; // ISO Date String
    date_range_end?: string | null;   // ISO Date String
    engine?: CrawlerEngine;
    source_type?: SourceType;
    direct_urls?: string | null;       // Multiline string
    ignore_robots?: boolean;
    proxy_url?: string | null;         // e.g., "socks5h://host:1080"
    schedule_time_hour?: number | null;
    schedule_time_minute?: number | null;
    schedule_time_weekday?: number | null;
    schedule_time_day?: number | null;
}

export interface SearchQueryResponse {
    id: number;
    keyword: string;
    match_type: MatchType;
    case_sensitive: boolean;
    exact_match: boolean;
    domains_filter?: string | null;     // JSON string representation
    languages_filter?: string | null;   // JSON string representation
    date_range_start?: string | null;  // ISO Date String
    date_range_end?: string | null;    // ISO Date String
    engine: CrawlerEngine;
    source_type: SourceType;
    direct_urls?: string | null;
    ignore_robots: boolean;
    status: SearchStatus;
    total_urls_found: number;
    total_urls_crawled: number;
    total_urls_matched: number;
    error_message?: string | null;
    created_at: string;                // ISO Date String
    updated_at: string;                // ISO Date String
}

export interface CrawledURLResponse {
    id: number;
    search_id: number;
    url: string;
    domain: string;
    title?: string | null;
    snippet?: string | null;
    occurrences: number;
    found_in_title: boolean;
    found_in_description: boolean;
    found_in_body: boolean;
    found_in_url: boolean;
    language?: string | null;
    status: CrawlStatus;
    error_message?: string | null;
    relevance_score: number;
    is_duplicate: boolean;
    description?: string | null;
    full_content?: string | null;
    raw_html?: string | null;
    author?: string | null;
    image_url?: string | null;
    image_links?: string | null;       // JSON string representation
    video_links?: string | null;       // JSON string representation
    discovered_at: string;             // ISO Date String
    matched_keywords?: string | null;  // JSON string representation or list
}

export interface PaginatedCrawledURLResponse {
    total: number;
    page: number;
    limit: number;
    items: CrawledURLResponse[];
}

export interface SearchScheduleCreate {
    keyword?: string;
    frequency: ScheduleFrequency;
    engine?: CrawlerEngine;
    config: SearchQueryCreate;
    next_run?: string | null;          // ISO Date String
}

export interface SearchScheduleResponse {
    id: number;
    keyword: string;
    frequency: ScheduleFrequency;
    active: boolean;
    engine: CrawlerEngine;
    config_json: string;               // JSON string of SearchQueryCreate
    last_run?: string | null;          // ISO Date String
    next_run: string;                  // ISO Date String
    created_at: string;                // ISO Date String
}

// ── Firecrawl Response Schemas ───────────────────────────────────────────────

export interface FirecrawlMetadata {
    title: string;
    description: string;
    language: string;
    sourceURL: string;
    statusCode: number;
}

export interface FirecrawlImageData {
    src: string;
    alt: string;
    caption: string;
    width: number;
    height: number;
}

export interface FirecrawlVideoData {
    src: string;
    title: string;
    thumbnail: string;
    type: string;
}

export interface FirecrawlLinkData {
    text: string;
    url: string;
    title: string;
}

export interface FirecrawlContent {
    headings: string[];
    paragraphs: string[];
    lists: string[][];
    tables: string[][][];
    codeBlocks: string[];
    quotes: string[];
}

export interface FirecrawlData {
    markdown: string;
    html: string;
    metadata: FirecrawlMetadata;
    links: FirecrawlLinkData[];
    images: FirecrawlImageData[];
    videos: FirecrawlVideoData[];
    content: FirecrawlContent;
}

export interface FirecrawlResponse {
    success: boolean;
    data: FirecrawlData;
}

export interface ScrapeRequest {
    url: string;
    engine?: CrawlerEngine;
    ignore_robots?: boolean;
    proxy_url?: string | null;
}
