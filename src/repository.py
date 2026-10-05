"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


BATCH_SELECT = (
    "SELECT b.*, a.code AS agency_code, a.name AS agency_name, "
    "COALESCE(SUM(r.applied), 0) AS recovered, COALESCE(SUM(r.refunded), 0) AS refunded "
    "FROM compensation_batches b "
    "JOIN agencies a ON a.id = b.agency_id "
    "LEFT JOIN recovery_payments r ON r.batch_id = b.id "
)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS quota_pools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL UNIQUE,
                    total_amount REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compensation_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    agency_id INTEGER NOT NULL REFERENCES agencies(id),
                    year INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pre_occupied',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_no TEXT NOT NULL UNIQUE,
                    batch_id INTEGER NOT NULL REFERENCES compensation_batches(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    applied REAL NOT NULL,
                    refunded REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_record ON compensation_batches(record_id);
                CREATE INDEX IF NOT EXISTS idx_batches_year ON compensation_batches(year, status);
                CREATE INDEX IF NOT EXISTS idx_recoveries_batch ON recovery_payments(batch_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def _quota_usage(self, connection: sqlite3.Connection, year: int) -> Dict[str, float]:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='pre_occupied' THEN amount END),0) AS pre_occupied, "
            "COALESCE(SUM(CASE WHEN status='confirmed' THEN amount END),0) AS confirmed "
            "FROM compensation_batches WHERE year=?",
            (int(year),),
        ).fetchone()
        recovered = connection.execute(
            "SELECT COALESCE(SUM(r.applied),0) AS total FROM recovery_payments r "
            "JOIN compensation_batches b ON b.id=r.batch_id WHERE b.year=?",
            (int(year),),
        ).fetchone()
        return {
            "pre_occupied": round(float(row["pre_occupied"]), 2),
            "confirmed": round(float(row["confirmed"]), 2),
            "recovered": round(float(recovered["total"]), 2),
        }

    @staticmethod
    def _pool_view(pool: sqlite3.Row, usage: Dict[str, float]) -> Dict[str, Any]:
        available = float(pool["total_amount"]) - usage["pre_occupied"] - usage["confirmed"] + usage["recovered"]
        return {
            "id": pool["id"],
            "year": pool["year"],
            "total_amount": pool["total_amount"],
            "pre_occupied": usage["pre_occupied"],
            "confirmed": usage["confirmed"],
            "recovered": usage["recovered"],
            "available": round(available, 2),
            "created_by": pool["created_by"],
            "created_at": pool["created_at"],
            "updated_at": pool["updated_at"],
        }

    def create_quota_pool(self, year: int, total_amount: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO quota_pools(year,total_amount,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (int(year), float(total_amount), actor_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该年度代偿额度池已存在") from exc
        return self.get_quota_pool(year)

    def get_quota_pool(self, year: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM quota_pools WHERE year=?", (int(year),)).fetchone()
            if row is None:
                raise NotFound("该年度代偿额度池不存在")
            usage = self._quota_usage(connection, int(year))
        return self._pool_view(row, usage)

    def list_quota_pools(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM quota_pools ORDER BY year").fetchall()
            return [self._pool_view(row, self._quota_usage(connection, row["year"])) for row in rows]

    def create_agency(self, code: str, name: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("INSERT INTO agencies(code,name,created_at) VALUES(?,?,?)", (code, name, _now()))
        except sqlite3.IntegrityError as exc:
            raise Conflict("担保机构代码已存在") from exc
        return self.get_agency_by_code(code)

    def get_agency_by_code(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agencies WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("担保机构不存在")
        return dict(row)

    def list_agencies(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM agencies ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _batch_view(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["recovered"] = round(float(item["recovered"]), 2)
        item["refunded"] = round(float(item["refunded"]), 2)
        item["outstanding"] = round(float(item["amount"]) - item["recovered"], 2)
        return item

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(BATCH_SELECT + " WHERE b.id=? GROUP BY b.id", (int(batch_id),)).fetchone()
        if row is None:
            raise NotFound("代偿批次不存在")
        return self._batch_view(row)

    def list_batches(self, record_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if record_id is not None:
                rows = connection.execute(BATCH_SELECT + " WHERE b.record_id=? GROUP BY b.id ORDER BY b.id DESC LIMIT ?", (int(record_id), limit)).fetchall()
            else:
                rows = connection.execute(BATCH_SELECT + " GROUP BY b.id ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_view(row) for row in rows]

    def submit_batch(self, record_id: int, agency: Dict[str, Any], batch_no: str, year: int, amount: float, actor_id: str) -> Any:
        """预占额度并登记批次；同一batch_no重试按原批次返回，不重复占用。返回(批次, 是否新建)。"""
        now = _now()
        created = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM records WHERE id=?", (int(record_id),)).fetchone() is None:
                connection.rollback()
                raise NotFound("记录不存在")
            existing = connection.execute("SELECT * FROM compensation_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if existing is not None:
                same = (
                    int(existing["record_id"]) == int(record_id)
                    and int(existing["agency_id"]) == int(agency["id"])
                    and int(existing["year"]) == int(year)
                    and abs(float(existing["amount"]) - float(amount)) < 1e-9
                )
                if not same:
                    connection.rollback()
                    raise Conflict("批次号已存在且要素不一致")
                batch_id = int(existing["id"])
            else:
                pool = connection.execute("SELECT * FROM quota_pools WHERE year=?", (int(year),)).fetchone()
                if pool is None:
                    connection.rollback()
                    raise NotFound("该年度代偿额度池不存在")
                usage = self._quota_usage(connection, int(year))
                available = float(pool["total_amount"]) - usage["pre_occupied"] - usage["confirmed"] + usage["recovered"]
                if float(amount) > available + 1e-9:
                    connection.rollback()
                    raise Conflict("年度代偿额度不足，先到先占")
                cursor = connection.execute(
                    "INSERT INTO compensation_batches(batch_no,record_id,agency_id,year,amount,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch_no, int(record_id), int(agency["id"]), int(year), float(amount), "pre_occupied", actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                created = True
            connection.commit()
        return self.get_batch(batch_id), created

    def confirm_batch(self, batch_id: int, actor_id: str) -> Any:
        """复核确认：预占转为有效占用；重复确认幂等返回。返回(批次, 是否变更)。"""
        now = _now()
        changed = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM compensation_batches WHERE id=?", (int(batch_id),)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("代偿批次不存在")
            if row["status"] == "voided":
                connection.rollback()
                raise Conflict("批次已作废，不能复核确认")
            if row["status"] == "pre_occupied":
                connection.execute("UPDATE compensation_batches SET status='confirmed', updated_at=? WHERE id=?", (now, int(batch_id)))
                changed = True
            connection.commit()
        return self.get_batch(batch_id), changed

    def _recovery_view(self, connection: sqlite3.Connection, recovery_id: int) -> Dict[str, Any]:
        row = connection.execute(
            "SELECT r.*, b.batch_no AS batch_no, b.record_id AS record_id, b.amount AS batch_amount "
            "FROM recovery_payments r JOIN compensation_batches b ON b.id=r.batch_id WHERE r.id=?",
            (int(recovery_id),),
        ).fetchone()
        applied_total = connection.execute(
            "SELECT COALESCE(SUM(applied),0) AS total FROM recovery_payments WHERE batch_id=?",
            (int(row["batch_id"]),),
        ).fetchone()
        item = dict(row)
        item["remaining"] = round(float(item["batch_amount"]) - float(applied_total["total"]), 2)
        del item["batch_amount"]
        return item

    def post_recovery(self, batch_id: int, flow_no: str, amount: float, actor_id: str) -> Any:
        """登记追偿回款：同一流水只匹配一笔；不足留差额，超额退回。返回(回款, 是否新建)。"""
        now = _now()
        created = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute("SELECT * FROM compensation_batches WHERE id=?", (int(batch_id),)).fetchone()
            if batch is None:
                connection.rollback()
                raise NotFound("代偿批次不存在")
            if batch["status"] != "confirmed":
                connection.rollback()
                raise Conflict("批次未复核确认，不能登记回款")
            existing = connection.execute("SELECT * FROM recovery_payments WHERE flow_no=?", (flow_no,)).fetchone()
            if existing is not None:
                if int(existing["batch_id"]) != int(batch_id) or abs(float(existing["amount"]) - float(amount)) > 1e-9:
                    connection.rollback()
                    raise Conflict("流水号已用于其他回款")
                recovery_id = int(existing["id"])
            else:
                applied_sum = connection.execute(
                    "SELECT COALESCE(SUM(applied),0) AS total FROM recovery_payments WHERE batch_id=?",
                    (int(batch_id),),
                ).fetchone()
                outstanding = round(float(batch["amount"]) - float(applied_sum["total"]), 2)
                applied = round(min(float(amount), max(outstanding, 0.0)), 2)
                refunded = round(float(amount) - applied, 2)
                cursor = connection.execute(
                    "INSERT INTO recovery_payments(flow_no,batch_id,amount,applied,refunded,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (flow_no, int(batch_id), float(amount), applied, refunded, actor_id, now),
                )
                recovery_id = int(cursor.lastrowid)
                created = True
            view = self._recovery_view(connection, recovery_id)
            connection.commit()
        return view, created

    def list_recoveries(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            batch = connection.execute("SELECT amount FROM compensation_batches WHERE id=?", (int(batch_id),)).fetchone()
            if batch is None:
                raise NotFound("代偿批次不存在")
            rows = connection.execute("SELECT * FROM recovery_payments WHERE batch_id=? ORDER BY id", (int(batch_id),)).fetchall()
        remaining = float(batch["amount"])
        result = []
        for row in rows:
            item = dict(row)
            remaining = round(remaining - float(item["applied"]), 2)
            item["remaining"] = remaining
            result.append(item)
        return result

    def mutate_and_void_pending_batches(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """记录状态迁移与作废未确认批次在同一事务内完成，作废后额度按批次重算。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            cursor = connection.execute(
                "UPDATE compensation_batches SET status='voided', updated_at=? WHERE record_id=? AND status='pre_occupied'",
                (now, record_id),
            )
            details = dict(details)
            details["voided_batches"] = int(cursor.rowcount)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get(record_id)

    def guarantee_stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            batch_rows = connection.execute("SELECT status, COUNT(*) AS total FROM compensation_batches GROUP BY status").fetchall()
            recovery = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(applied),0) AS applied, COALESCE(SUM(refunded),0) AS refunded FROM recovery_payments"
            ).fetchone()
            confirmed = connection.execute("SELECT COALESCE(SUM(amount),0) AS total FROM compensation_batches WHERE status='confirmed'").fetchone()
        return {
            "batches": {str(row["status"]): int(row["total"]) for row in batch_rows},
            "recoveries": {
                "count": int(recovery["total"]),
                "applied": round(float(recovery["applied"]), 2),
                "refunded": round(float(recovery["refunded"]), 2),
                "outstanding": round(float(confirmed["total"]) - float(recovery["applied"]), 2),
            },
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
