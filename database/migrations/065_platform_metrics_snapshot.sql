-- Public platform metrics (/about, /api/analytics, /api/platform-metrics) as a
-- persisted snapshot. Computing them is a full pass over items; serving it
-- from a 5-minute in-process cache put that pass (~100 s, ~1.5 GB of temp
-- spill) in front of whichever visitor came next, and with / at 3 GB free the
-- spill hit ENOSPC (2026-10-26). The API's background refresher is the one
-- writer; requests only read this row, and it survives restarts.

CREATE TABLE platform_metrics_snapshot (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    metrics JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    elapsed_ms INTEGER NOT NULL
);
