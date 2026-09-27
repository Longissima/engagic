"""Async PulseRepository: per-jurisdiction change-signal state.

See migration 054 for the model. The watcher owns probing; this repository
only records what a probe saw and which jurisdictions still owe a sync.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from database.repositories_async.base import BaseRepository
from config import get_logger

logger = get_logger(__name__).bind(component="pulse_repository")


class PulseRepository(BaseRepository):
    """Durable memory for the pulse watcher."""

    async def load_states(self, bananas: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        rows = await self._fetch(
            """
            SELECT banana, signal, state, usable, checked_at, dirty_since,
                   consecutive_failures, quiet_streak, next_probe_at
            FROM jurisdiction_pulse
            WHERE banana = ANY($1::text[])
            """,
            list(bananas),
        )
        return {row["banana"]: dict(row) for row in rows}

    async def record_success(
        self,
        banana: str,
        signal: str,
        state: Dict[str, Any],
        *,
        changed: bool,
        quiet_streak: int,
        next_in_seconds: float,
        hint_dates: Optional[List[date]] = None,
    ) -> None:
        """Store a successful probe. On a change, hint_dates narrows the
        pending sync to those meeting dates; None means the change could not
        be localized and the whole window must sync. Hints accumulate across
        probes until a sync clears them."""
        await self._execute(
            """
            INSERT INTO jurisdiction_pulse
                (banana, signal, state, usable, checked_at, changed_at, dirty_since,
                 dirty_touched_at, dirty_full, dirty_dates, quiet_streak, next_probe_at)
            VALUES ($1, $2, $3, TRUE, NOW(),
                    CASE WHEN $4 THEN NOW() END, CASE WHEN $4 THEN NOW() END,
                    CASE WHEN $4 THEN NOW() END, $4 AND $7::date[] IS NULL,
                    CASE WHEN $4 THEN COALESCE($7::date[], '{}') ELSE '{}' END,
                    $5, NOW() + make_interval(secs => $6))
            ON CONFLICT (banana) DO UPDATE SET
                signal = EXCLUDED.signal,
                state = EXCLUDED.state,
                usable = TRUE,
                checked_at = NOW(),
                changed_at = CASE WHEN $4 THEN NOW() ELSE jurisdiction_pulse.changed_at END,
                dirty_since = CASE
                    WHEN $4 THEN COALESCE(jurisdiction_pulse.dirty_since, NOW())
                    ELSE jurisdiction_pulse.dirty_since
                END,
                dirty_touched_at = CASE WHEN $4 THEN NOW() ELSE jurisdiction_pulse.dirty_touched_at END,
                dirty_full = CASE
                    WHEN NOT $4 THEN jurisdiction_pulse.dirty_full
                    WHEN jurisdiction_pulse.dirty_since IS NULL THEN $7::date[] IS NULL
                    ELSE jurisdiction_pulse.dirty_full OR $7::date[] IS NULL
                END,
                dirty_dates = CASE
                    WHEN NOT $4 THEN jurisdiction_pulse.dirty_dates
                    WHEN jurisdiction_pulse.dirty_since IS NULL THEN COALESCE($7::date[], '{}')
                    ELSE jurisdiction_pulse.dirty_dates || COALESCE($7::date[], '{}')
                END,
                consecutive_failures = 0,
                last_error = NULL,
                quiet_streak = EXCLUDED.quiet_streak,
                next_probe_at = EXCLUDED.next_probe_at
            """,
            banana,
            signal,
            state,
            changed,
            quiet_streak,
            next_in_seconds,
            hint_dates,
        )

    async def record_failure(
        self,
        banana: str,
        signal: str,
        error: str,
        *,
        unusable: bool,
        next_in_seconds: float,
    ) -> None:
        """Count a failed probe. unusable=True means the signal itself is
        absent for this jurisdiction (disabled feed, empty feed, ignored
        filter), not a transient outage."""
        await self._execute(
            """
            INSERT INTO jurisdiction_pulse
                (banana, signal, usable, checked_at, consecutive_failures, last_error,
                 next_probe_at)
            VALUES ($1, $2, NOT $4, NOW(), 1, $3, NOW() + make_interval(secs => $5))
            ON CONFLICT (banana) DO UPDATE SET
                signal = EXCLUDED.signal,
                usable = NOT $4 AND jurisdiction_pulse.usable,
                checked_at = NOW(),
                consecutive_failures = jurisdiction_pulse.consecutive_failures + 1,
                last_error = EXCLUDED.last_error,
                next_probe_at = EXCLUDED.next_probe_at
            """,
            banana,
            signal,
            error[:1000],
            unusable,
            next_in_seconds,
        )

    async def list_dirty(self) -> List[Dict[str, Any]]:
        rows = await self._fetch(
            """
            SELECT banana, dirty_full, dirty_dates
            FROM jurisdiction_pulse
            WHERE dirty_since IS NOT NULL
            ORDER BY dirty_since
            """
        )
        return [dict(row) for row in rows]

    async def clear_dirty(self, bananas: Sequence[str], observed_before: datetime) -> int:
        """Clear only rows with no change observed after the sync started; a
        change seen while the sync ran stays dirty and re-drives another."""
        result = await self._execute(
            """
            UPDATE jurisdiction_pulse
            SET dirty_since = NULL, dirty_touched_at = NULL, dirty_full = FALSE, dirty_dates = '{}'
            WHERE banana = ANY($1::text[])
              AND COALESCE(dirty_touched_at, dirty_since) <= $2
            """,
            list(bananas),
            observed_before,
        )
        return self._parse_row_count(result)

    async def get_database_now(self) -> datetime:
        row: Optional[Any] = await self._fetchrow("SELECT NOW()::timestamp AS now")
        assert row is not None
        return row["now"]
