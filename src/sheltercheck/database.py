from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .models import AlarmSession, Member


class DatabaseError(RuntimeError):
    """Persistent state could not be read or updated safely."""


class StateDatabase:
    """Small synchronous SQLite store used only from the asyncio event-loop thread."""

    def __init__(self, path: str | Path) -> None:
        path_string = str(path)
        if path_string != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path_string)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if path_string != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    trigger_author_aci TEXT NOT NULL,
                    trigger_timestamp_ms INTEGER NOT NULL,
                    deadline_timestamp_ms INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'reported', 'completed', 'stale', 'error')
                    ),
                    initial_report_state INTEGER NOT NULL DEFAULT 0 CHECK (
                        initial_report_state IN (-1, 0, 1)
                    ),
                    initial_missing_snapshotted INTEGER NOT NULL DEFAULT 0 CHECK (
                        initial_missing_snapshotted IN (0, 1)
                    ),
                    report_message_timestamp_ms INTEGER,
                    last_report_text TEXT,
                    edit_due_timestamp_ms INTEGER,
                    error_detail TEXT,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE (group_id, trigger_author_aci, trigger_timestamp_ms)
                );

                CREATE TABLE IF NOT EXISTS released_members (
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    signal_aci TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    PRIMARY KEY (alarm_id, signal_aci),
                    UNIQUE (alarm_id, position)
                );

                CREATE TABLE IF NOT EXISTS reaction_state (
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                    reactor_aci TEXT NOT NULL,
                    responded INTEGER NOT NULL CHECK (responded IN (0, 1)),
                    last_event_timestamp_ms INTEGER NOT NULL,
                    last_emoji TEXT NOT NULL,
                    last_removed INTEGER NOT NULL CHECK (last_removed IN (0, 1)),
                    PRIMARY KEY (alarm_id, reactor_aci)
                );

                CREATE TABLE IF NOT EXISTS initial_missing (
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                    signal_aci TEXT NOT NULL,
                    PRIMARY KEY (alarm_id, signal_aci),
                    FOREIGN KEY (alarm_id, signal_aci)
                        REFERENCES released_members(alarm_id, signal_aci)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS alarms_due_initial
                    ON alarms(status, initial_report_state, deadline_timestamp_ms);
                CREATE INDEX IF NOT EXISTS alarms_reaction_target
                    ON alarms(group_id, trigger_timestamp_ms, status);
                CREATE INDEX IF NOT EXISTS alarms_due_edit
                    ON alarms(status, edit_due_timestamp_ms);
                """
            )
        self._migrate_initial_missing()

    def _migrate_initial_missing(self) -> None:
        """Add and best-effort backfill the immutable snapshot for older databases."""

        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(alarms)").fetchall()
        }
        with self._connection:
            if "initial_missing_snapshotted" not in columns:
                self._connection.execute(
                    """
                    ALTER TABLE alarms
                    ADD COLUMN initial_missing_snapshotted INTEGER NOT NULL DEFAULT 0
                        CHECK (initial_missing_snapshotted IN (0, 1))
                    """
                )

            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS initial_missing (
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                    signal_aci TEXT NOT NULL,
                    PRIMARY KEY (alarm_id, signal_aci),
                    FOREIGN KEY (alarm_id, signal_aci)
                        REFERENCES released_members(alarm_id, signal_aci)
                        ON DELETE CASCADE
                )
                """
            )

            # Older schema versions did not retain the deadline snapshot. The last
            # reaction timestamp recovers the normal cases: people currently absent,
            # and people whose latest positive reaction arrived after the deadline.
            self._connection.execute(
                """
                INSERT OR IGNORE INTO initial_missing (alarm_id, signal_aci)
                SELECT released.alarm_id, released.signal_aci
                FROM released_members AS released
                JOIN alarms AS alarm ON alarm.id = released.alarm_id
                LEFT JOIN reaction_state AS reaction
                    ON reaction.alarm_id = released.alarm_id
                    AND reaction.reactor_aci = released.signal_aci
                WHERE alarm.initial_report_state != 0
                  AND alarm.initial_missing_snapshotted = 0
                  AND NOT (
                      COALESCE(reaction.responded, 0) = 1
                      AND reaction.last_event_timestamp_ms <= alarm.deadline_timestamp_ms
                  )
                """
            )
            self._connection.execute(
                """
                UPDATE alarms
                SET initial_missing_snapshotted = 1
                WHERE initial_report_state != 0
                  AND initial_missing_snapshotted = 0
                """
            )

    def create_alarm(
        self,
        *,
        group_id: str,
        trigger_author_aci: str,
        trigger_timestamp_ms: int,
        deadline_timestamp_ms: int,
        released_members: Iterable[Member],
        status: str = "pending",
        error_detail: str | None = None,
        created_at_ms: int | None = None,
    ) -> AlarmSession | None:
        members = tuple(released_members)
        created_at_ms = created_at_ms if created_at_ms is not None else time.time_ns() // 1_000_000
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO alarms (
                    group_id, trigger_author_aci, trigger_timestamp_ms,
                    deadline_timestamp_ms, status, error_detail, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    trigger_author_aci,
                    trigger_timestamp_ms,
                    deadline_timestamp_ms,
                    status,
                    error_detail,
                    created_at_ms,
                ),
            )
            if cursor.rowcount == 0:
                return None
            alarm_id = int(cursor.lastrowid)
            self._connection.executemany(
                """
                INSERT INTO released_members (
                    alarm_id, position, signal_aci, display_name, phone
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (alarm_id, position, member.signal_aci, member.display_name, member.phone)
                    for position, member in enumerate(members)
                ],
            )
        return self.get_alarm(alarm_id)

    def get_alarm(self, alarm_id: int) -> AlarmSession:
        row = self._connection.execute(
            "SELECT * FROM alarms WHERE id = ?", (alarm_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError(f"alarm {alarm_id} does not exist")
        return self._hydrate(row)

    def get_alarm_by_identity(
        self, group_id: str, trigger_author_aci: str, trigger_timestamp_ms: int
    ) -> AlarmSession | None:
        row = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE group_id = ? AND trigger_author_aci = ? AND trigger_timestamp_ms = ?
            """,
            (group_id, trigger_author_aci, trigger_timestamp_ms),
        ).fetchone()
        return self._hydrate(row) if row is not None else None

    def list_alarms(self) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            "SELECT * FROM alarms ORDER BY trigger_timestamp_ms, id"
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def count_active_alarms(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count FROM alarms
            WHERE status IN ('pending', 'reported', 'completed')
            """
        ).fetchone()
        return int(row["count"])

    def last_check_timestamp_ms(self) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(trigger_timestamp_ms) AS timestamp_ms FROM alarms"
        ).fetchone()
        value = row["timestamp_ms"]
        return int(value) if value is not None else None

    def find_active_reaction_targets(
        self,
        *,
        group_id: str,
        trigger_timestamp_ms: int,
        target_author_aci: str | None,
    ) -> tuple[AlarmSession, ...]:
        sql = """
            SELECT * FROM alarms
            WHERE group_id = ? AND trigger_timestamp_ms = ?
              AND status IN ('pending', 'reported', 'completed')
        """
        params: list[object] = [group_id, trigger_timestamp_ms]
        if target_author_aci is not None:
            sql += " AND trigger_author_aci = ?"
            params.append(target_author_aci)
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def snapshot_initial_missing(self, alarm_id: int) -> AlarmSession:
        """Persist the deadline membership once and return the refreshed alarm."""

        with self._connection:
            alarm = self._connection.execute(
                """
                SELECT status, initial_report_state, initial_missing_snapshotted
                FROM alarms WHERE id = ?
                """,
                (alarm_id,),
            ).fetchone()
            if alarm is None:
                raise DatabaseError(f"alarm {alarm_id} does not exist")
            if (
                alarm["status"] == "pending"
                and int(alarm["initial_report_state"]) == 0
                and int(alarm["initial_missing_snapshotted"]) == 0
            ):
                self._connection.execute(
                    """
                    INSERT INTO initial_missing (alarm_id, signal_aci)
                    SELECT released.alarm_id, released.signal_aci
                    FROM released_members AS released
                    LEFT JOIN reaction_state AS reaction
                        ON reaction.alarm_id = released.alarm_id
                        AND reaction.reactor_aci = released.signal_aci
                    WHERE released.alarm_id = ?
                      AND COALESCE(reaction.responded, 0) = 0
                    """,
                    (alarm_id,),
                )
                self._connection.execute(
                    """
                    UPDATE alarms SET initial_missing_snapshotted = 1
                    WHERE id = ? AND initial_missing_snapshotted = 0
                    """,
                    (alarm_id,),
                )
        return self.get_alarm(alarm_id)

    def apply_reaction(
        self,
        *,
        alarm_id: int,
        reactor_aci: str,
        responded: bool,
        event_timestamp_ms: int,
        emoji: str,
        removed: bool,
        now_ms: int,
        debounce_ms: int,
    ) -> bool:
        """Apply the newest accepted reaction and return whether response state changed."""

        with self._connection:
            alarm = self._connection.execute(
                "SELECT status, initial_report_state FROM alarms WHERE id = ?", (alarm_id,)
            ).fetchone()
            if alarm is None or alarm["status"] not in {
                "pending",
                "reported",
                "completed",
            }:
                return False
            is_released = self._connection.execute(
                """
                SELECT 1 FROM released_members
                WHERE alarm_id = ? AND signal_aci = ?
                """,
                (alarm_id, reactor_aci),
            ).fetchone()
            if is_released is None:
                return False

            previous = self._connection.execute(
                """
                SELECT responded, last_event_timestamp_ms, last_emoji, last_removed
                FROM reaction_state WHERE alarm_id = ? AND reactor_aci = ?
                """,
                (alarm_id, reactor_aci),
            ).fetchone()
            if previous is not None:
                previous_timestamp = int(previous["last_event_timestamp_ms"])
                if event_timestamp_ms < previous_timestamp:
                    return False
                if (
                    event_timestamp_ms == previous_timestamp
                    and emoji == previous["last_emoji"]
                    and int(removed) == int(previous["last_removed"])
                ):
                    return False

            old_responded = bool(previous["responded"]) if previous is not None else False
            changed = old_responded != responded
            self._connection.execute(
                """
                INSERT INTO reaction_state (
                    alarm_id, reactor_aci, responded, last_event_timestamp_ms,
                    last_emoji, last_removed
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(alarm_id, reactor_aci) DO UPDATE SET
                    responded = excluded.responded,
                    last_event_timestamp_ms = excluded.last_event_timestamp_ms,
                    last_emoji = excluded.last_emoji,
                    last_removed = excluded.last_removed
                """,
                (
                    alarm_id,
                    reactor_aci,
                    int(responded),
                    event_timestamp_ms,
                    emoji,
                    int(removed),
                ),
            )
            if (
                changed
                and alarm["status"] in {"reported", "completed"}
                and int(alarm["initial_report_state"]) == 1
            ):
                self._connection.execute(
                    "UPDATE alarms SET edit_due_timestamp_ms = ? WHERE id = ?",
                    (now_ms + debounce_ms, alarm_id),
                )
            return changed

    def list_due_initial_reports(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE status = 'pending' AND initial_report_state = 0
              AND deadline_timestamp_ms <= ?
            ORDER BY deadline_timestamp_ms, id
            """,
            (now_ms,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def begin_initial_report(self, alarm_id: int, report_text: str) -> bool:
        """Durably claim a send before the non-transactional Signal RPC call."""

        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE alarms
                SET initial_report_state = -1, last_report_text = ?
                WHERE id = ? AND status = 'pending' AND initial_report_state = 0
                  AND initial_missing_snapshotted = 1
                """,
                (report_text, alarm_id),
            )
        return cursor.rowcount == 1

    def release_initial_report(self, alarm_id: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms SET initial_report_state = 0
                WHERE id = ? AND status = 'pending' AND initial_report_state = -1
                """,
                (alarm_id,),
            )

    def complete_initial_report(
        self,
        alarm_id: int,
        *,
        report_timestamp_ms: int,
        report_text: str,
        missing_count: int,
    ) -> None:
        status = "completed" if missing_count == 0 else "reported"
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE alarms
                SET initial_report_state = 1,
                    report_message_timestamp_ms = ?,
                    last_report_text = ?,
                    edit_due_timestamp_ms = NULL,
                    status = ?
                WHERE id = ? AND initial_report_state = -1
                """,
                (report_timestamp_ms, report_text, status, alarm_id),
            )
        if cursor.rowcount != 1:
            raise DatabaseError(f"alarm {alarm_id} initial report claim was lost")

    def list_due_edits(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE status IN ('reported', 'completed') AND initial_report_state = 1
              AND edit_due_timestamp_ms IS NOT NULL
              AND edit_due_timestamp_ms <= ?
            ORDER BY edit_due_timestamp_ms, id
            """,
            (now_ms,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def complete_edit(self, alarm_id: int, report_text: str, missing_count: int) -> None:
        status = "completed" if missing_count == 0 else "reported"
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms
                SET last_report_text = ?, edit_due_timestamp_ms = NULL, status = ?
                WHERE id = ? AND initial_report_state = 1
                """,
                (report_text, status, alarm_id),
            )

    def defer_edit(self, alarm_id: int, due_timestamp_ms: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms SET edit_due_timestamp_ms = ?
                WHERE id = ? AND status IN ('reported', 'completed')
                """,
                (due_timestamp_ms, alarm_id),
            )

    def list_uncertain_initial_reports(self) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            "SELECT * FROM alarms WHERE initial_report_state = -1 ORDER BY id"
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def _hydrate(self, row: sqlite3.Row) -> AlarmSession:
        alarm_id = int(row["id"])
        member_rows = self._connection.execute(
            """
            SELECT signal_aci, display_name, phone FROM released_members
            WHERE alarm_id = ? ORDER BY position
            """,
            (alarm_id,),
        ).fetchall()
        response_rows = self._connection.execute(
            """
            SELECT reactor_aci FROM reaction_state
            WHERE alarm_id = ? AND responded = 1
            """,
            (alarm_id,),
        ).fetchall()
        initial_missing_rows = self._connection.execute(
            """
            SELECT released.signal_aci, released.display_name, released.phone
            FROM initial_missing AS missing
            JOIN released_members AS released
              ON released.alarm_id = missing.alarm_id
             AND released.signal_aci = missing.signal_aci
            WHERE missing.alarm_id = ?
            ORDER BY released.position
            """,
            (alarm_id,),
        ).fetchall()
        members = tuple(
            Member(member["signal_aci"], member["display_name"], member["phone"])
            for member in member_rows
        )
        initial_missing = tuple(
            Member(member["signal_aci"], member["display_name"], member["phone"])
            for member in initial_missing_rows
        )
        responded = frozenset(response["reactor_aci"] for response in response_rows)
        return AlarmSession(
            id=alarm_id,
            group_id=row["group_id"],
            trigger_author_aci=row["trigger_author_aci"],
            trigger_timestamp_ms=int(row["trigger_timestamp_ms"]),
            deadline_timestamp_ms=int(row["deadline_timestamp_ms"]),
            status=row["status"],
            initial_report_state=int(row["initial_report_state"]),
            report_message_timestamp_ms=(
                int(row["report_message_timestamp_ms"])
                if row["report_message_timestamp_ms"] is not None
                else None
            ),
            last_report_text=row["last_report_text"],
            edit_due_timestamp_ms=(
                int(row["edit_due_timestamp_ms"])
                if row["edit_due_timestamp_ms"] is not None
                else None
            ),
            released_members=members,
            initial_missing=initial_missing,
            responded_acis=responded,
        )
