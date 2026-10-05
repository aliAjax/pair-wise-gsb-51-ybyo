"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .guarantees import BATCH_CONFIRMED, BATCH_PENDING, BATCH_REJECTED, BATCH_VOIDED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

                CREATE TABLE IF NOT EXISTS guarantors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_pools (
                    year INTEGER PRIMARY KEY,
                    total_quota REAL NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compensation_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    guarantor_code TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    recovered_amount REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_status_year ON compensation_batches(status, year);
                CREATE INDEX IF NOT EXISTS idx_batches_guarantor ON compensation_batches(guarantor_code, year);
                CREATE INDEX IF NOT EXISTS idx_batches_record ON compensation_batches(record_id);
                CREATE TABLE IF NOT EXISTS recoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    serial_no TEXT NOT NULL UNIQUE,
                    batch_id INTEGER NOT NULL REFERENCES compensation_batches(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    applied_amount REAL NOT NULL,
                    refunded_amount REAL NOT NULL DEFAULT 0,
                    remaining_after REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recoveries_batch ON recoveries(batch_id, id);
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

    # ----- 担保机构与共用年度额度池 -----

    def create_guarantor(self, code: str, name: str, actor_id: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO guarantors(code,name,created_by,created_at) VALUES(?,?,?,?)",
                    (code, name, actor_id, _now()),
                )
                row = connection.execute("SELECT * FROM guarantors WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("担保机构编码已存在") from exc
        return dict(row)

    def list_guarantors(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM guarantors ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_guarantor(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM guarantors WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("担保机构不存在")
        return dict(row)

    def upsert_quota(self, year: int, total_quota: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO quota_pools(year,total_quota,updated_by,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(year) DO UPDATE SET total_quota=excluded.total_quota,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                (year, round(float(total_quota), 2), actor_id, now),
            )
            row = connection.execute("SELECT * FROM quota_pools WHERE year=?", (year,)).fetchone()
        return dict(row)

    def get_quota(self, year: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM quota_pools WHERE year=?", (year,)).fetchone()
        return dict(row) if row is not None else None

    def quota_overview(self, year: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT q.year, q.total_quota,"
            " COALESCE(SUM(CASE WHEN b.status='pending' THEN b.amount END),0) AS pending_amount,"
            " COALESCE(SUM(CASE WHEN b.status='confirmed' THEN b.amount END),0) AS confirmed_amount,"
            " COUNT(CASE WHEN b.status='pending' THEN 1 END) AS pending_batches,"
            " COUNT(CASE WHEN b.status='confirmed' THEN 1 END) AS confirmed_batches,"
            " COALESCE(SUM(CASE WHEN b.status IN ('pending','confirmed') THEN b.amount END),0) AS occupied_amount"
            " FROM quota_pools q LEFT JOIN compensation_batches b ON b.year=q.year"
        )
        params: tuple = ()
        if year is not None:
            sql += " WHERE q.year=?"
            params = (year,)
        sql += " GROUP BY q.year ORDER BY q.year DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["available_amount"] = round(float(item["total_quota"]) - float(item["occupied_amount"]), 2)
            result.append(item)
        return result

    # ----- 代偿批次 -----

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        from .guarantees import BATCH_STATUS_LABELS

        item = dict(row)
        item["amount"] = round(float(item["amount"]), 2)
        item["recovered_amount"] = round(float(item.pop("recovered_total")), 2)
        item["outstanding_amount"] = round(item["amount"] - item["recovered_amount"], 2)
        item["status_label"] = BATCH_STATUS_LABELS.get(item["status"], item["status"])
        return item

    _BATCH_SELECT = (
        "SELECT b.*, COALESCE((SELECT SUM(r.applied_amount) FROM recoveries r WHERE r.batch_id=b.id),0) AS recovered_total"
        " FROM compensation_batches b"
    )

    def get_batch(self, batch_id: int, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        own = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                self._BATCH_SELECT + " WHERE b.id=?", (batch_id,)
            ).fetchone()
        finally:
            if own:
                connection.close()
        if row is None:
            raise NotFound("代偿批次不存在")
        return self._batch_row(row)

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(self._BATCH_SELECT + " WHERE b.batch_no=?", (batch_no,)).fetchone()
        if row is None:
            return None
        return self._batch_row(row)

    def list_batches(self, status: str = None, guarantor_code: str = None, year: int = None, record_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if status:
            clauses.append("b.status=?")
            params.append(status)
        if guarantor_code:
            clauses.append("b.guarantor_code=?")
            params.append(guarantor_code)
        if year is not None:
            clauses.append("b.year=?")
            params.append(year)
        if record_id is not None:
            clauses.append("b.record_id=?")
            params.append(record_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                self._BATCH_SELECT + where + " ORDER BY b.id DESC LIMIT ?", tuple(params) + (limit,)
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def submit_compensation(self, batch_no: str, guarantor_code: str, year: int, record_id: Optional[int], amount: float, actor_id: str, note: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                pool = connection.execute("SELECT * FROM quota_pools WHERE year=?", (year,)).fetchone()
                if pool is None:
                    connection.rollback()
                    raise NotFound("%s年度额度池尚未配置" % year)
                guarantor = connection.execute("SELECT code FROM guarantors WHERE code=?", (guarantor_code,)).fetchone()
                if guarantor is None:
                    connection.rollback()
                    raise NotFound("担保机构不存在")
                occupied = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM compensation_batches WHERE year=? AND status IN (?,?)",
                    (year, BATCH_PENDING, BATCH_CONFIRMED),
                ).fetchone()["total"]
                if float(occupied) + amount > float(pool["total_quota"]) + 0.005:
                    connection.rollback()
                    raise Conflict(
                        "年度共享额度不足：总额度%s，已占用%s，剩余%s，本次申请%s"
                        % (float(pool["total_quota"]), round(float(occupied), 2),
                           round(float(pool["total_quota"]) - float(occupied), 2), amount)
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO compensation_batches(batch_no,guarantor_code,year,record_id,amount,status,
                        recovered_amount,created_by,reviewed_by,note,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,0,?,NULL,?,?,?)
                    """,
                    (batch_no, guarantor_code, year, record_id, amount, BATCH_PENDING, actor_id, note, now, now),
                )
                batch_id = int(cursor.lastrowid)
                if record_id is not None:
                    version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record_id, "compensation_submitted", actor_id, version,
                         json.dumps({"batch_no": batch_no, "amount": amount, "status": BATCH_PENDING}, ensure_ascii=False, sort_keys=True), now),
                    )
                row = connection.execute(self._BATCH_SELECT + " WHERE b.id=?", (batch_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("代偿批次号已存在") from exc
        return self._batch_row(row)

    def decide_batch(self, batch_id: int, approve: bool, actor_id: str, note: str) -> Dict[str, Any]:
        from .guarantees import BATCH_STATUS_LABELS

        new_status = BATCH_CONFIRMED if approve else BATCH_REJECTED
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM compensation_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("代偿批次不存在")
            if row["status"] != BATCH_PENDING:
                connection.rollback()
                raise Conflict("仅待复核批次可以复核，当前状态：%s" % BATCH_STATUS_LABELS.get(row["status"], row["status"]))
            connection.execute(
                "UPDATE compensation_batches SET status=?,reviewed_by=?,note=?,updated_at=? WHERE id=?",
                (new_status, actor_id, note, now, batch_id),
            )
            record_id = row["record_id"]
            if record_id is not None:
                version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if version_row is not None:
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record_id, "compensation_confirmed" if approve else "compensation_rejected", actor_id,
                         int(version_row["version"]),
                         json.dumps({"batch_no": row["batch_no"], "status": new_status, "note": note}, ensure_ascii=False, sort_keys=True), now),
                    )
            result = connection.execute(self._BATCH_SELECT + " WHERE b.id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    # ----- 追偿回款 -----

    @staticmethod
    def _recovery_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("amount", "applied_amount", "refunded_amount", "remaining_after"):
            item[key] = round(float(item[key]), 2)
        return item

    def get_recovery_by_serial(self, serial_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recoveries WHERE serial_no=?", (serial_no,)).fetchone()
        return self._recovery_row(row) if row is not None else None

    def list_recoveries(self, batch_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if batch_id is not None:
                rows = connection.execute(
                    "SELECT * FROM recoveries WHERE batch_id=? ORDER BY id DESC LIMIT ?", (batch_id, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM recoveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._recovery_row(row) for row in rows]

    def register_recovery(self, serial_no: str, batch_id: int, amount: float, actor_id: str) -> Dict[str, Any]:
        from .guarantees import GuaranteeRules

        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM compensation_batches WHERE id=?", (batch_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("代偿批次不存在")
                if row["status"] != BATCH_CONFIRMED:
                    connection.rollback()
                    raise Conflict("仅已确认批次可以登记追偿回款")
                recovered = float(connection.execute(
                    "SELECT COALESCE(SUM(applied_amount),0) AS total FROM recoveries WHERE batch_id=?", (batch_id,)
                ).fetchone()["total"])
                outstanding = round(float(row["amount"]) - recovered, 2)
                applied, refunded = GuaranteeRules.allocate(outstanding, amount)
                new_recovered = round(recovered + applied, 2)
                remaining = round(float(row["amount"]) - new_recovered, 2)
                cursor = connection.execute(
                    """
                    INSERT INTO recoveries(serial_no,batch_id,amount,applied_amount,refunded_amount,remaining_after,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (serial_no, batch_id, amount, applied, refunded, remaining, actor_id, now),
                )
                connection.execute(
                    "UPDATE compensation_batches SET recovered_amount=?,updated_at=? WHERE id=?",
                    (new_recovered, now, batch_id),
                )
                record_id = row["record_id"]
                if record_id is not None:
                    version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                    if version_row is not None:
                        connection.execute(
                            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                            (record_id, "recovery_registered", actor_id, int(version_row["version"]),
                             json.dumps({"serial_no": serial_no, "amount": amount, "applied_amount": applied,
                                         "refunded_amount": refunded, "remaining_after": remaining}, ensure_ascii=False, sort_keys=True), now),
                        )
                result = connection.execute("SELECT * FROM recoveries WHERE id=?", (int(cursor.lastrowid),)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("回款流水号已存在，不能重复冲减") from exc
        return self._recovery_row(result)

    # ----- 方案失效联动 -----

    def void_pending_batches(self, record_id: int, actor_id: str, reason: str) -> List[Dict[str, Any]]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM compensation_batches WHERE record_id=? AND status=?",
                (record_id, BATCH_PENDING),
            ).fetchall()
            version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            version = int(version_row["version"]) if version_row is not None else 1
            for row in rows:
                connection.execute(
                    "UPDATE compensation_batches SET status=?,reviewed_by=?,note=?,updated_at=? WHERE id=?",
                    (BATCH_VOIDED, actor_id, reason, now, int(row["id"])),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "compensation_voided", actor_id, version,
                     json.dumps({"batch_no": row["batch_no"], "amount": float(row["amount"]), "reason": reason},
                                ensure_ascii=False, sort_keys=True), now),
                )
            result = connection.execute(
                self._BATCH_SELECT + " WHERE b.record_id=? ORDER BY b.id", (record_id,)
            ).fetchall()
            connection.commit()
        return [self._batch_row(row) for row in result]

    # ----- 担保统计 -----

    def guarantee_stats(self, year: Optional[int] = None) -> Dict[str, Any]:
        clauses, params = [], []
        if year is not None:
            clauses.append("b.year=?")
            params.append(year)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            pool_rows = connection.execute(
                "SELECT year, total_quota FROM quota_pools" + (" WHERE year=?" if year is not None else ""),
                (year,) if year is not None else (),
            ).fetchall()
            batch_rows = connection.execute(
                "SELECT status, COUNT(*) AS total, COALESCE(SUM(amount),0) AS amount"
                " FROM compensation_batches b" + where + " GROUP BY status",
                tuple(params),
            ).fetchall()
            guarantor_rows = connection.execute(
                "SELECT b.guarantor_code, COUNT(*) AS batches,"
                " COALESCE(SUM(CASE WHEN b.status IN ('pending','confirmed') THEN b.amount END),0) AS occupied,"
                " COALESCE((SELECT SUM(r.applied_amount) FROM recoveries r"
                " JOIN compensation_batches b2 ON b2.id=r.batch_id WHERE b2.guarantor_code=b.guarantor_code"
                + (" AND b2.year=?" if year is not None else "") + "),0) AS recovered"
                " FROM compensation_batches b"
                + (" WHERE b.year=?" if year is not None else "")
                + " GROUP BY b.guarantor_code ORDER BY b.guarantor_code",
                (year, year) if year is not None else (),
            ).fetchall()
            recovery_total = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(r.applied_amount),0) AS applied,"
                " COALESCE(SUM(r.refunded_amount),0) AS refunded FROM recoveries r"
                " JOIN compensation_batches cb ON cb.id=r.batch_id"
                + (" WHERE cb.year=?" if year is not None else ""),
                (year,) if year is not None else (),
            ).fetchone()
        batches: Dict[str, Dict[str, float]] = {}
        for row in batch_rows:
            batches[str(row["status"])] = {"count": int(row["total"]), "amount": round(float(row["amount"]), 2)}
        total_quota = round(sum(float(row["total_quota"]) for row in pool_rows), 2)
        occupied = round(sum(
            item["amount"] for status, item in batches.items() if status in (BATCH_PENDING, BATCH_CONFIRMED)
        ), 2)
        return {
            "year": year,
            "total_quota": total_quota,
            "occupied_amount": occupied,
            "available_amount": round(total_quota - occupied, 2),
            "batches": batches,
            "recoveries": {"count": int(recovery_total["total"]),
                           "applied_amount": round(float(recovery_total["applied"]), 2),
                           "refunded_amount": round(float(recovery_total["refunded"]), 2)},
            "by_guarantor": [
                {"guarantor_code": row["guarantor_code"], "batches": int(row["batches"]),
                 "occupied_amount": round(float(row["occupied"]), 2),
                 "recovered_amount": round(float(row["recovered"]), 2),
                 "outstanding_amount": round(float(row["occupied"]) - float(row["recovered"]), 2)}
                for row in guarantor_rows
            ],
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
