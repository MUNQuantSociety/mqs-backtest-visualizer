-- public.market_data — the one table this application READS but never owns.
--
-- In production the live trading system (MQSMaster) creates and fills this
-- table; the backtest visualiser only ever SELECTs from it. A local container
-- has no MQSMaster to do that, so the DDL is reproduced here verbatim from
-- MQSMaster/src/common/database/schemaDefinitions.py. Keep the two in sync:
-- if a column is added there, add it here, or a backtest that works against
-- the real warehouse will fail locally for reasons that look like a bug.
--
-- This file runs ONLY when the data volume is empty (that is how the postgres
-- image's /docker-entrypoint-initdb.d works). To pick up an edit you must
-- destroy the volume: `docker compose down -v`.
--
-- Deliberately absent:
--   * the `app` schema and its tables — src/db/init.py creates those on every
--     boot, and duplicating them here would give you two sources of truth;
--   * positions_book, cash_equity_book, trade_execution_logs and the rest of
--     the trading tables — this application must never read or write them,
--     and a dev database that has them invites code that does.

CREATE TABLE IF NOT EXISTS public.market_data (
    id            SERIAL PRIMARY KEY,
    ticker        VARCHAR(10) NOT NULL,
    timestamp     TIMESTAMP WITH TIME ZONE NOT NULL,
    date          DATE NOT NULL,
    exchange      VARCHAR(50) NOT NULL,
    open_price    NUMERIC,
    high_price    NUMERIC,
    low_price     NUMERIC,
    close_price   NUMERIC,
    volume        BIGINT,
    avg_sentiment NUMERIC,
    created_at    TIMESTAMP DEFAULT NOW()
);

-- The engine's hot path is `WHERE ticker = ? ORDER BY timestamp`, and
-- repositories/market_data.py reads both ends of this index to bound a run
-- window. It is also what makes the seeder's ON CONFLICT upsert idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_data_ticker_timestamp
    ON public.market_data (ticker, timestamp);
