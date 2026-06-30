-- Postgres init script: seed admin user + default trading pairs + demo trader
--
-- IMPORTANT: This script runs BEFORE Alembic migrations (which create the
-- tables). All INSERTs are wrapped in DO $$ ... IF EXISTS ... END $$ blocks
-- so they silently skip if the table doesn't exist yet. The backend's
-- startup migrations (alembic upgrade head) will create the tables, and
-- the seed data will be applied on the NEXT restart via a separate
-- seed script, OR you can run this script manually after migrations:
--
--   docker compose exec postgres psql -U exchange -d exchange -f /docker-entrypoint-initdb.d/init.sql
--
-- For automatic seeding after migrations, the backend's lifespan startup
-- also calls a seed function (see app/services/admin_service.py).

-- ─── Helper: conditional INSERT that only runs if the table exists ──────────

-- Default admin user
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
        INSERT INTO users (email, password_hash, is_active, is_admin)
        VALUES (
            'admin@exchange.local',
            -- bcrypt hash of "admin123" (rounds=12)
            '$2b$12$YoOvlVHqBqmjojR56Om.WOjhr6ILQj.0mmZXs8bwBfDidn.2t4bla',
            true,
            true
        )
        ON CONFLICT (email) DO NOTHING;
        RAISE NOTICE 'Seeded admin user: admin@exchange.local';
    ELSE
        RAISE NOTICE 'Table "users" does not exist yet — skipping admin seed (will be seeded by backend startup)';
    END IF;
END $$;

-- Default trading pairs
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'trading_pairs') THEN
        INSERT INTO trading_pairs (
            symbol, base_asset, quote_asset,
            price_precision, quantity_precision,
            min_lot_size, max_lot_size, tick_size,
            maker_fee_bps, taker_fee_bps, is_active
        ) VALUES
            ('BTC/USDT', 'BTC', 'USDT', 2, 8, 0.0001, 1000, 0.01, 0, 0, true),
            ('ETH/USDT', 'ETH', 'USDT', 2, 8, 0.0001, 1000, 0.01, 0, 0, true),
            ('SOL/USDT', 'SOL', 'USDT', 2, 8, 0.0001, 1000, 0.01, 0, 0, true),
            ('BNB/USDT', 'BNB', 'USDT', 2, 8, 0.0001, 1000, 0.01, 0, 0, true)
        ON CONFLICT (symbol) DO NOTHING;
        RAISE NOTICE 'Seeded 4 trading pairs';
    ELSE
        RAISE NOTICE 'Table "trading_pairs" does not exist yet — skipping (will be seeded by backend startup)';
    END IF;
END $$;

-- Demo trader user + balances
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
        INSERT INTO users (email, password_hash, is_active, is_admin)
        VALUES (
            'trader@exchange.local',
            '$2b$12$YoOvlVHqBqmjojR56Om.WOjhr6ILQj.0mmZXs8bwBfDidn.2t4bla',
            true,
            false
        )
        ON CONFLICT (email) DO NOTHING;
        RAISE NOTICE 'Seeded demo trader: trader@exchange.local';
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'balances') THEN
        -- Give the demo trader starting balances (100k USDT + 10 BTC)
        INSERT INTO balances (user_id, asset, total_balance, locked_balance, available_balance)
        SELECT u.id, 'USDT', 100000, 0, 100000
        FROM users u WHERE u.email = 'trader@exchange.local'
        ON CONFLICT (user_id, asset) DO NOTHING;

        INSERT INTO balances (user_id, asset, total_balance, locked_balance, available_balance)
        SELECT u.id, 'BTC', 10, 0, 10
        FROM users u WHERE u.email = 'trader@exchange.local'
        ON CONFLICT (user_id, asset) DO NOTHING;
        RAISE NOTICE 'Seeded demo trader balances';
    END IF;
END $$;
