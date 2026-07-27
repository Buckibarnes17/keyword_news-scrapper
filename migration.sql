-- Migration Script: Create news_media.scraped_news table and indexes
-- Target Schema: news_media
-- Target Table: scraped_news

-- 1. Ensure target schema exists
CREATE SCHEMA IF NOT EXISTS news_media;

-- 2. Create target scraped_news table only if it doesn't already exist
CREATE TABLE IF NOT EXISTS news_media.scraped_news (
    id SERIAL PRIMARY KEY,
    keyword TEXT NOT NULL,
    title TEXT,
    url TEXT UNIQUE,
    source TEXT,
    author TEXT,
    published_date TIMESTAMP,
    scraped_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    language VARCHAR(20),
    country VARCHAR(50),
    summary TEXT,
    content TEXT,
    sentiment VARCHAR(20),
    category VARCHAR(100),
    image_url TEXT,
    crawl_id UUID,
    status VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Create indexes only if they do not already exist
CREATE INDEX IF NOT EXISTS idx_scraped_news_keyword ON news_media.scraped_news (keyword);
CREATE INDEX IF NOT EXISTS idx_scraped_news_published_date ON news_media.scraped_news (published_date);
CREATE INDEX IF NOT EXISTS idx_scraped_news_source ON news_media.scraped_news (source);
CREATE INDEX IF NOT EXISTS idx_scraped_news_status ON news_media.scraped_news (status);
CREATE INDEX IF NOT EXISTS idx_scraped_news_crawl_id ON news_media.scraped_news (crawl_id);
