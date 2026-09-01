from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import AlarmSession, Member, ObservedMessage, OutgoingOperation, ReactionEvent


ACTIVE_STATUSES = ("pending", "reported")
OUTGOING_STATES = (
    "not_due",
    "due_not_attempted",
    "attempted_success",
    "attempted_failed",
    "attempted_uncertain",
    "skipped",
)
OBSERVED_RETENTION_MS = 24 * 60 * 60 * 1000
_DEFAULT_TTL_MS = 21_600_000


class DatabaseError(RuntimeError):
    """Persistent state could not be read or updated safely."""


class StateDatabase:
    """Small synchronous SQLite store used from the serialized event-loop paths."""

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
        self._migrate_alarm_table()
        with self._connection:
            self._connection.executescript(
                """
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

                CREATE TABLE IF NOT EXISTS outgoing_operations (
                    operation_key TEXT PRIMARY KEY,
                    alarm_id INTEGER REFERENCES alarms(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'not_due', 'due_not_attempted', 'attempted_success',
                        'attempted_failed', 'attempted_uncertain', 'skipped'
                    )),
                    message TEXT,
                    target_timestamp_ms INTEGER,
                    result_timestamp_ms INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_detail TEXT,
                    created_at_ms INTEGER NOT NULL,
                    attempted_at_ms INTEGER,
                    updated_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observed_messages (
                    group_id TEXT NOT NULL,
                    sender_aci TEXT NOT NULL,
                    sent_timestamp_ms INTEGER NOT NULL,
                    original_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    PRIMARY KEY (group_id, sender_aci, sent_timestamp_ms)
                );

                CREATE TABLE IF NOT EXISTS observed_reactions (
                    id INTEGER PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    target_author_aci TEXT NOT NULL,
                    target_timestamp_ms INTEGER NOT NULL,
                    reactor_aci TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    removed INTEGER NOT NULL CHECK (removed IN (0, 1)),
                    event_timestamp_ms INTEGER NOT NULL,
                    FOREIGN KEY (group_id, target_author_aci, target_timestamp_ms)
                        REFERENCES observed_messages(group_id, sender_aci, sent_timestamp_ms)
                        ON DELETE CASCADE,
                    UNIQUE (
                        group_id, target_author_aci, target_timestamp_ms,
                        reactor_aci, emoji, removed, event_timestamp_ms
                    )
                );

                CREATE INDEX IF NOT EXISTS alarms_due_intermediate
                    ON alarms(status, intermediate_deadline_timestamp_ms);
                CREATE INDEX IF NOT EXISTS alarms_due_final
                    ON alarms(status, deadline_timestamp_ms);
                CREATE INDEX IF NOT EXISTS alarms_due_expiry
                    ON alarms(status, expires_timestamp_ms);
                CREATE INDEX IF NOT EXISTS alarms_reaction_target
                    ON alarms(group_id, trigger_timestamp_ms, status);
                CREATE INDEX IF NOT EXISTS alarms_due_edit
                    ON alarms(status, edit_due_timestamp_ms);
                CREATE INDEX IF NOT EXISTS alarms_latest_active
                    ON alarms(status, tracking_started_at_ms DESC);
                CREATE INDEX IF NOT EXISTS outgoing_alarm_kind
                    ON outgoing_operations(alarm_id, kind, state);
                CREATE INDEX IF NOT EXISTS observed_message_lookup
                    ON observed_messages(group_id, normalized_text, sent_timestamp_ms DESC);
                CREATE INDEX IF NOT EXISTS observed_reaction_replay
                    ON observed_reactions(
                        group_id, target_author_aci, target_timestamp_ms,
                        event_timestamp_ms, id
                    );
                """
            )
        self._migrate_initial_missing()
        self._migrate_legacy_outgoing_operations()

    @staticmethod
    def _alarm_table_sql(table_name: str) -> str:
        return f"""
            CREATE TABLE {table_name} (
                id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                trigger_author_aci TEXT NOT NULL,
                trigger_timestamp_ms INTEGER NOT NULL,
                tracking_started_at_ms INTEGER NOT NULL,
                intermediate_deadline_timestamp_ms INTEGER NOT NULL,
                deadline_timestamp_ms INTEGER NOT NULL,
                expires_timestamp_ms INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'standard'
                    CHECK (source IN ('standard', 'custom')),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'reported', 'completed', 'expired', 'stale', 'error'
                    )
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
                reaction_revision INTEGER NOT NULL DEFAULT 0,
                error_detail TEXT,
                created_at_ms INTEGER NOT NULL,
                UNIQUE (group_id, trigger_author_aci, trigger_timestamp_ms)
            )
        """

    def _migrate_alarm_table(self) -> None:
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'alarms'"
        ).fetchone()
        if row is None:
            with self._connection:
                self._connection.execute(self._alarm_table_sql("alarms"))
            return

        columns = {
            item["name"]
            for item in self._connection.execute("PRAGMA table_info(alarms)").fetchall()
        }
        required = {
            "tracking_started_at_ms",
            "intermediate_deadline_timestamp_ms",
            "expires_timestamp_ms",
            "source",
            "initial_missing_snapshotted",
            "reaction_revision",
        }
        table_sql = row["sql"] or ""
        if required <= columns and "'expired'" in table_sql:
            return

        def value(column: str, fallback: str) -> str:
            return column if column in columns else fallback

        source_fallback = "'standard'"
        select_values = (
            "id, group_id, trigger_author_aci, trigger_timestamp_ms, "
            f"{value('tracking_started_at_ms', 'trigger_timestamp_ms')}, "
            f"{value('intermediate_deadline_timestamp_ms', 'deadline_timestamp_ms')}, "
            "deadline_timestamp_ms, "
            f"{value('expires_timestamp_ms', f'trigger_timestamp_ms + {_DEFAULT_TTL_MS}')}, "
            f"{value('source', source_fallback)}, status, initial_report_state, "
            f"{value('initial_missing_snapshotted', '0')}, "
            "report_message_timestamp_ms, last_report_text, edit_due_timestamp_ms, "
            f"{value('reaction_revision', '0')}, error_detail, created_at_ms"
        )
        self._connection.commit()
        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self._connection:
                self._connection.execute("DROP TABLE IF EXISTS alarms_new")
                self._connection.execute(self._alarm_table_sql("alarms_new"))
                self._connection.execute(
                    """
                    INSERT INTO alarms_new (
                        id, group_id, trigger_author_aci, trigger_timestamp_ms,
                        tracking_started_at_ms, intermediate_deadline_timestamp_ms,
                        deadline_timestamp_ms, expires_timestamp_ms, source, status,
                        initial_report_state, initial_missing_snapshotted,
                        report_message_timestamp_ms, last_report_text,
                        edit_due_timestamp_ms, reaction_revision, error_detail, created_at_ms
                    )
                    SELECT """
                    + select_values
                    + " FROM alarms"
                )
                self._connection.execute("DROP TABLE alarms")
                self._connection.execute("ALTER TABLE alarms_new RENAME TO alarms")
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_initial_missing(self) -> None:
        with self._connection:
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

    def _migrate_legacy_outgoing_operations(self) -> None:
        """Represent old report claims without making any old write retryable."""

        now_ms = time.time_ns() // 1_000_000
        rows = self._connection.execute(
            """
            SELECT id, initial_report_state, report_message_timestamp_ms
            FROM alarms
            """
        ).fetchall()
        with self._connection:
            for row in rows:
                alarm_id = int(row["id"])
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO outgoing_operations (
                        operation_key, alarm_id, kind, state,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'intermediate', 'skipped', ?, ?)
                    """,
                    (
                        self.alarm_operation_key(alarm_id, "intermediate"),
                        alarm_id,
                        now_ms,
                        now_ms,
                    ),
                )
                legacy_state = int(row["initial_report_state"])
                if legacy_state == 0:
                    state = "not_due"
                elif legacy_state == 1 and row["report_message_timestamp_ms"] is not None:
                    state = "attempted_success"
                else:
                    state = "attempted_uncertain"
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO outgoing_operations (
                        operation_key, alarm_id, kind, state,
                        result_timestamp_ms, attempt_count, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'final', ?, ?, ?, ?, ?)
                    """,
                    (
                        self.alarm_operation_key(alarm_id, "final"),
                        alarm_id,
                        state,
                        row["report_message_timestamp_ms"],
                        int(legacy_state != 0),
                        now_ms,
                        now_ms,
                    ),
                )

    @staticmethod
    def alarm_operation_key(alarm_id: int, kind: str) -> str:
        return f"alarm:{alarm_id}:{kind}"

    def create_alarm(
        self,
        *,
        group_id: str,
        trigger_author_aci: str,
        trigger_timestamp_ms: int,
        deadline_timestamp_ms: int,
        released_members: Iterable[Member],
        tracking_started_at_ms: int | None = None,
        intermediate_deadline_timestamp_ms: int | None = None,
        expires_timestamp_ms: int | None = None,
        source: str = "standard",
        status: str = "pending",
        error_detail: str | None = None,
        created_at_ms: int | None = None,
    ) -> AlarmSession | None:
        members = tuple(released_members)
        tracking_started_at_ms = (
            trigger_timestamp_ms
            if tracking_started_at_ms is None
            else tracking_started_at_ms
        )
        intermediate_deadline_timestamp_ms = (
            deadline_timestamp_ms
            if intermediate_deadline_timestamp_ms is None
            else intermediate_deadline_timestamp_ms
        )
        expires_timestamp_ms = (
            tracking_started_at_ms + _DEFAULT_TTL_MS
            if expires_timestamp_ms is None
            else expires_timestamp_ms
        )
        created_at_ms = (
            created_at_ms
            if created_at_ms is not None
            else time.time_ns() // 1_000_000
        )
        initial_operation_state = "not_due" if status in ACTIVE_STATUSES else "skipped"
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO alarms (
                    group_id, trigger_author_aci, trigger_timestamp_ms,
                    tracking_started_at_ms, intermediate_deadline_timestamp_ms,
                    deadline_timestamp_ms, expires_timestamp_ms, source,
                    status, error_detail, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    trigger_author_aci,
                    trigger_timestamp_ms,
                    tracking_started_at_ms,
                    intermediate_deadline_timestamp_ms,
                    deadline_timestamp_ms,
                    expires_timestamp_ms,
                    source,
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
            for kind in ("intermediate", "final"):
                key = self.alarm_operation_key(alarm_id, kind)
                self._connection.execute(
                    """
                    INSERT INTO outgoing_operations (
                        operation_key, alarm_id, kind, state,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, alarm_id, kind, initial_operation_state, created_at_ms, created_at_ms),
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
            "SELECT COUNT(*) AS count FROM alarms WHERE status IN ('pending', 'reported')"
        ).fetchone()
        return int(row["count"])

    def latest_active_alarm(self) -> AlarmSession | None:
        row = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE status IN ('pending', 'reported')
            ORDER BY tracking_started_at_ms DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        return self._hydrate(row) if row is not None else None

    def latest_standard_alarm(self) -> AlarmSession | None:
        row = self._connection.execute(
            """
            SELECT * FROM alarms WHERE source = 'standard'
            ORDER BY trigger_timestamp_ms DESC, id DESC LIMIT 1
            """
        ).fetchone()
        return self._hydrate(row) if row is not None else None

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
              AND status IN ('pending', 'reported')
        """
        params: list[object] = [group_id, trigger_timestamp_ms]
        if target_author_aci is not None:
            sql += " AND trigger_author_aci = ?"
            params.append(target_author_aci)
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def snapshot_final_missing(self, alarm_id: int) -> AlarmSession:
        with self._connection:
            alarm = self._connection.execute(
                """
                SELECT status, initial_missing_snapshotted
                FROM alarms WHERE id = ?
                """,
                (alarm_id,),
            ).fetchone()
            if alarm is None:
                raise DatabaseError(f"alarm {alarm_id} does not exist")
            if (
                alarm["status"] == "pending"
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

    snapshot_initial_missing = snapshot_final_missing

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
        """Apply only the newest accepted event to an active alarm."""

        with self._connection:
            alarm = self._connection.execute(
                """
                SELECT status, reaction_revision, report_message_timestamp_ms
                FROM alarms WHERE id = ?
                """,
                (alarm_id,),
            ).fetchone()
            if alarm is None or alarm["status"] not in ACTIVE_STATUSES:
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
            if changed:
                revision = int(alarm["reaction_revision"]) + 1
                edit_due = (
                    now_ms + debounce_ms
                    if alarm["status"] == "reported"
                    and alarm["report_message_timestamp_ms"] is not None
                    else None
                )
                self._connection.execute(
                    """
                    UPDATE alarms
                    SET reaction_revision = ?,
                        edit_due_timestamp_ms = COALESCE(?, edit_due_timestamp_ms)
                    WHERE id = ? AND status IN ('pending', 'reported')
                    """,
                    (revision, edit_due, alarm_id),
                )
            return changed

    def list_due_intermediate(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT alarm.* FROM alarms AS alarm
            JOIN outgoing_operations AS operation
              ON operation.operation_key = 'alarm:' || alarm.id || ':intermediate'
            WHERE alarm.status = 'pending'
              AND alarm.intermediate_deadline_timestamp_ms <= ?
              AND operation.state = 'not_due'
            ORDER BY alarm.intermediate_deadline_timestamp_ms, alarm.id
            """,
            (now_ms,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def list_due_final(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT alarm.* FROM alarms AS alarm
            JOIN outgoing_operations AS operation
              ON operation.operation_key = 'alarm:' || alarm.id || ':final'
            WHERE alarm.status = 'pending'
              AND alarm.deadline_timestamp_ms <= ?
              AND operation.state = 'not_due'
            ORDER BY alarm.deadline_timestamp_ms, alarm.id
            """,
            (now_ms,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def list_due_edits(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE status = 'reported'
              AND report_message_timestamp_ms IS NOT NULL
              AND edit_due_timestamp_ms IS NOT NULL
              AND edit_due_timestamp_ms <= ?
            ORDER BY edit_due_timestamp_ms, id
            """,
            (now_ms,),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def expire_due_alarms(self, now_ms: int) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM alarms
            WHERE status IN ('pending', 'reported') AND expires_timestamp_ms <= ?
            ORDER BY expires_timestamp_ms, id
            """,
            (now_ms,),
        ).fetchall()
        alarms = tuple(self._hydrate(row) for row in rows)
        if not alarms:
            return ()
        ids = [alarm.id for alarm in alarms]
        placeholders = ",".join("?" for _ in ids)
        with self._connection:
            self._connection.execute(
                f"""
                UPDATE alarms SET status = 'expired', edit_due_timestamp_ms = NULL
                WHERE id IN ({placeholders}) AND status IN ('pending', 'reported')
                """,
                ids,
            )
            self._connection.execute(
                f"""
                UPDATE outgoing_operations SET state = 'skipped', updated_at_ms = ?
                WHERE alarm_id IN ({placeholders})
                  AND state IN ('not_due', 'due_not_attempted')
                """,
                (now_ms, *ids),
            )
        return alarms

    def mark_reported(
        self,
        alarm_id: int,
        *,
        operation_state: str,
        report_timestamp_ms: int | None,
        report_text: str,
    ) -> None:
        legacy_state = -1 if operation_state == "attempted_uncertain" else 1
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms
                SET status = 'reported', initial_report_state = ?,
                    report_message_timestamp_ms = ?, last_report_text = ?,
                    edit_due_timestamp_ms = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (legacy_state, report_timestamp_ms, report_text, alarm_id),
            )

    def mark_completed(self, alarm_id: int, *, now_ms: int | None = None) -> None:
        now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms
                SET status = 'completed', edit_due_timestamp_ms = NULL
                WHERE id = ? AND status IN ('pending', 'reported')
                """,
                (alarm_id,),
            )
            self._connection.execute(
                """
                UPDATE outgoing_operations SET state = 'skipped', updated_at_ms = ?
                WHERE alarm_id = ? AND kind IN ('intermediate', 'final')
                  AND state IN ('not_due', 'due_not_attempted')
                """,
                (now_ms, alarm_id),
            )

    def clear_edit_due(self, alarm_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE alarms SET edit_due_timestamp_ms = NULL WHERE id = ?",
                (alarm_id,),
            )

    def record_successful_edit(self, alarm_id: int, report_text: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE alarms SET last_report_text = ?, edit_due_timestamp_ms = NULL
                WHERE id = ? AND status = 'reported'
                """,
                (report_text, alarm_id),
            )

    def prepare_outgoing(
        self,
        *,
        operation_key: str,
        kind: str,
        now_ms: int,
        alarm_id: int | None = None,
        state: str = "due_not_attempted",
        message: str | None = None,
        target_timestamp_ms: int | None = None,
    ) -> OutgoingOperation:
        if state not in OUTGOING_STATES:
            raise ValueError(f"invalid outgoing state: {state}")
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO outgoing_operations (
                    operation_key, alarm_id, kind, state, message,
                    target_timestamp_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_key,
                    alarm_id,
                    kind,
                    state,
                    message,
                    target_timestamp_ms,
                    now_ms,
                    now_ms,
                ),
            )
            if message is not None:
                self._connection.execute(
                    """
                    UPDATE outgoing_operations
                    SET message = ?, target_timestamp_ms = ?, updated_at_ms = ?
                    WHERE operation_key = ? AND state IN ('not_due', 'due_not_attempted')
                    """,
                    (message, target_timestamp_ms, now_ms, operation_key),
                )
        operation = self.get_outgoing(operation_key)
        if operation is None:
            raise DatabaseError(f"outgoing operation {operation_key!r} was not created")
        return operation

    def mark_outgoing_due(
        self,
        operation_key: str,
        *,
        message: str,
        now_ms: int,
        target_timestamp_ms: int | None = None,
    ) -> OutgoingOperation:
        with self._connection:
            self._connection.execute(
                """
                UPDATE outgoing_operations
                SET state = 'due_not_attempted', message = ?,
                    target_timestamp_ms = ?, updated_at_ms = ?
                WHERE operation_key = ? AND state = 'not_due'
                """,
                (message, target_timestamp_ms, now_ms, operation_key),
            )
        operation = self.get_outgoing(operation_key)
        if operation is None:
            raise DatabaseError(f"outgoing operation {operation_key!r} does not exist")
        return operation

    def claim_outgoing(self, operation_key: str, *, now_ms: int) -> bool:
        """Claim before RPC; a crash leaves the operation durably uncertain."""

        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE outgoing_operations
                SET state = 'attempted_uncertain', attempt_count = attempt_count + 1,
                    attempted_at_ms = ?, updated_at_ms = ?, error_detail = NULL
                WHERE operation_key = ? AND state = 'due_not_attempted'
                """,
                (now_ms, now_ms, operation_key),
            )
        return cursor.rowcount == 1

    def finish_outgoing(
        self,
        operation_key: str,
        *,
        state: str,
        now_ms: int,
        result_timestamp_ms: int | None = None,
        error_detail: str | None = None,
    ) -> None:
        if state not in {
            "attempted_success",
            "attempted_failed",
            "attempted_uncertain",
            "skipped",
        }:
            raise ValueError(f"invalid terminal outgoing state: {state}")
        with self._connection:
            self._connection.execute(
                """
                UPDATE outgoing_operations
                SET state = ?, result_timestamp_ms = ?, error_detail = ?, updated_at_ms = ?
                WHERE operation_key = ? AND state = 'attempted_uncertain'
                """,
                (state, result_timestamp_ms, error_detail, now_ms, operation_key),
            )

    def skip_outgoing(self, operation_key: str, *, now_ms: int, reason: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE outgoing_operations
                SET state = 'skipped', error_detail = ?, updated_at_ms = ?
                WHERE operation_key = ? AND state IN ('not_due', 'due_not_attempted')
                """,
                (reason, now_ms, operation_key),
            )

    def get_outgoing(self, operation_key: str) -> OutgoingOperation | None:
        row = self._connection.execute(
            "SELECT * FROM outgoing_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:
            return None
        return OutgoingOperation(
            operation_key=row["operation_key"],
            alarm_id=int(row["alarm_id"]) if row["alarm_id"] is not None else None,
            kind=row["kind"],
            state=row["state"],
            message=row["message"],
            target_timestamp_ms=(
                int(row["target_timestamp_ms"])
                if row["target_timestamp_ms"] is not None
                else None
            ),
            result_timestamp_ms=(
                int(row["result_timestamp_ms"])
                if row["result_timestamp_ms"] is not None
                else None
            ),
            attempt_count=int(row["attempt_count"]),
            error_detail=row["error_detail"],
        )

    def list_uncertain_initial_reports(self) -> tuple[AlarmSession, ...]:
        rows = self._connection.execute(
            """
            SELECT alarm.* FROM alarms AS alarm
            JOIN outgoing_operations AS operation ON operation.alarm_id = alarm.id
            WHERE operation.kind = 'final' AND operation.state = 'attempted_uncertain'
            ORDER BY alarm.id
            """
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def list_uncertain_outgoing_operations(self) -> tuple[OutgoingOperation, ...]:
        rows = self._connection.execute(
            """
            SELECT operation_key FROM outgoing_operations
            WHERE state = 'attempted_uncertain'
            ORDER BY created_at_ms, operation_key
            """
        ).fetchall()
        operations = tuple(
            self.get_outgoing(row["operation_key"]) for row in rows
        )
        return tuple(operation for operation in operations if operation is not None)

    def observe_message(
        self,
        *,
        group_id: str,
        sender_aci: str,
        sent_timestamp_ms: int,
        original_text: str,
        normalized_text: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO observed_messages (
                    group_id, sender_aci, sent_timestamp_ms,
                    original_text, normalized_text
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, sender_aci, sent_timestamp_ms, original_text, normalized_text),
            )

    def find_latest_observed_message(
        self,
        *,
        group_id: str,
        normalized_text: str,
        allowed_authors: Sequence[str],
    ) -> ObservedMessage | None:
        sql = """
            SELECT * FROM observed_messages
            WHERE group_id = ? AND normalized_text = ?
        """
        params: list[object] = [group_id, normalized_text]
        if allowed_authors:
            placeholders = ",".join("?" for _ in allowed_authors)
            sql += f" AND sender_aci IN ({placeholders})"
            params.extend(allowed_authors)
        sql += " ORDER BY sent_timestamp_ms DESC, sender_aci DESC LIMIT 1"
        row = self._connection.execute(sql, params).fetchone()
        if row is None:
            return None
        return ObservedMessage(
            group_id=row["group_id"],
            sender_aci=row["sender_aci"],
            sent_timestamp_ms=int(row["sent_timestamp_ms"]),
            original_text=row["original_text"],
            normalized_text=row["normalized_text"],
        )

    def resolve_observed_reaction_target(
        self,
        *,
        group_id: str,
        target_timestamp_ms: int,
        target_author_aci: str | None,
    ) -> ObservedMessage | None:
        sql = """
            SELECT * FROM observed_messages
            WHERE group_id = ? AND sent_timestamp_ms = ?
        """
        params: list[object] = [group_id, target_timestamp_ms]
        if target_author_aci is not None:
            sql += " AND sender_aci = ?"
            params.append(target_author_aci)
        rows = self._connection.execute(sql, params).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        return ObservedMessage(
            group_id=row["group_id"],
            sender_aci=row["sender_aci"],
            sent_timestamp_ms=int(row["sent_timestamp_ms"]),
            original_text=row["original_text"],
            normalized_text=row["normalized_text"],
        )

    def observe_reaction(
        self,
        *,
        target: ObservedMessage,
        event: ReactionEvent,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO observed_reactions (
                    group_id, target_author_aci, target_timestamp_ms,
                    reactor_aci, emoji, removed, event_timestamp_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target.group_id,
                    target.sender_aci,
                    target.sent_timestamp_ms,
                    event.reactor_aci,
                    event.emoji,
                    int(event.removed),
                    event.sent_timestamp_ms,
                ),
            )

    def list_observed_reactions(self, message: ObservedMessage) -> tuple[ReactionEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM observed_reactions
            WHERE group_id = ? AND target_author_aci = ? AND target_timestamp_ms = ?
            ORDER BY event_timestamp_ms, id
            """,
            (message.group_id, message.sender_aci, message.sent_timestamp_ms),
        ).fetchall()
        return tuple(
            ReactionEvent(
                group_id=row["group_id"],
                reactor_aci=row["reactor_aci"],
                target_author_aci=row["target_author_aci"],
                target_timestamp_ms=int(row["target_timestamp_ms"]),
                emoji=row["emoji"],
                removed=bool(row["removed"]),
                sent_timestamp_ms=int(row["event_timestamp_ms"]),
            )
            for row in rows
        )

    def cleanup_observed_history(
        self, now_ms: int, *, retention_ms: int = OBSERVED_RETENTION_MS
    ) -> int:
        cutoff = now_ms - retention_ms
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM observed_messages
                WHERE sent_timestamp_ms < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM alarms
                      WHERE alarms.group_id = observed_messages.group_id
                        AND alarms.trigger_author_aci = observed_messages.sender_aci
                        AND alarms.trigger_timestamp_ms = observed_messages.sent_timestamp_ms
                        AND alarms.status IN ('pending', 'reported')
                  )
                """,
                (cutoff,),
            )
        return cursor.rowcount

    def observed_message_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM observed_messages"
        ).fetchone()
        return int(row["count"])

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
            tracking_started_at_ms=int(row["tracking_started_at_ms"]),
            intermediate_deadline_timestamp_ms=int(
                row["intermediate_deadline_timestamp_ms"]
            ),
            deadline_timestamp_ms=int(row["deadline_timestamp_ms"]),
            expires_timestamp_ms=int(row["expires_timestamp_ms"]),
            source=row["source"],
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
            reaction_revision=int(row["reaction_revision"]),
            released_members=members,
            initial_missing=initial_missing,
            responded_acis=responded,
        )
