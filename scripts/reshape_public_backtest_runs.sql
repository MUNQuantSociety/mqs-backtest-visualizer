-- Reshape public.backtest_runs to: id, owner_id, created_at, results JSONB.
-- Identity stays as columns; strategy-specific numbers live in results.
-- Safe to re-run. Run this in pgAdmin against mqsdb, then retry GET /api/backtests.

ALTER TABLE public.backtest_runs
  ADD COLUMN IF NOT EXISTS results JSONB;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'backtest_runs'
      AND column_name = 'name'
  ) THEN
    UPDATE public.backtest_runs
    SET results = jsonb_strip_nulls(
      jsonb_build_object(
        'name', name,
        'strategy_key', strategy_key,
        'symbol', symbol,
        'status', status,
        'start_date', start_date,
        'end_date', end_date,
        'total_return', total_return,
        'sharpe', sharpe,
        'max_drawdown', max_drawdown
      )
    )
    WHERE results IS NULL OR results = '{}'::jsonb;
  END IF;
END $$;

UPDATE public.backtest_runs
SET results = '{}'::jsonb
WHERE results IS NULL;

ALTER TABLE public.backtest_runs
  ALTER COLUMN results SET DEFAULT '{}'::jsonb,
  ALTER COLUMN results SET NOT NULL;

ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS name;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS strategy_key;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS symbol;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS status;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS start_date;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS end_date;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS total_return;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS sharpe;
ALTER TABLE public.backtest_runs DROP COLUMN IF EXISTS max_drawdown;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'backtest_runs_owner_fk'
  ) THEN
    ALTER TABLE public.backtest_runs
      ADD CONSTRAINT backtest_runs_owner_fk
      FOREIGN KEY (owner_id) REFERENCES public.user_creds(id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_backtest_runs_owner_created
  ON public.backtest_runs (owner_id, created_at DESC);
