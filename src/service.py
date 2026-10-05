"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if self.rules.voids_pending_batches(action):
            return self.repository.mutate_and_void_pending_batches(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
            )
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {
            "records": self.repository.stats(),
            "guarantee": self.repository.guarantee_stats(),
            "quota_pools": self.repository.list_quota_pools(),
        }

    def create_quota_pool(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_administer(actor.role):
            raise PermissionDenied("仅管理员可维护年度代偿额度")
        data = self.rules.validate_pool(payload or {})
        return self.repository.create_quota_pool(int(data["year"]), float(data["total_amount"]), actor.user_id)

    def list_quota_pools(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_quota_pools()

    def create_agency(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_administer(actor.role):
            raise PermissionDenied("仅管理员可登记担保机构")
        data = self.rules.validate_agency(payload or {})
        return self.repository.create_agency(data["code"], data["name"])

    def list_agencies(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_agencies()

    def submit_compensation(self, actor: Actor, record_id: int, payload: Dict[str, Any]) -> Any:
        """提交代偿批次并预占额度；同一batch_no重试按原批次返回。返回(批次, 是否新建)。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_submit_batch(actor.role):
            raise PermissionDenied("角色无权提交代偿批次")
        record = self.repository.get(record_id)
        self.rules.check_record_accepts_compensation(record)
        data = self.rules.validate_batch(payload or {}, datetime.now(timezone.utc).year)
        agency = self.repository.get_agency_by_code(data["agency_code"])
        batch, created = self.repository.submit_batch(
            record_id=record["id"],
            agency=agency,
            batch_no=data["batch_no"],
            year=int(data["year"]),
            amount=float(data["amount"]),
            actor_id=actor.user_id,
        )
        if created:
            self.audit.note(record["id"], actor.user_id, "compensation_submitted", {
                "batch_no": batch["batch_no"],
                "agency_code": agency["code"],
                "year": batch["year"],
                "amount": batch["amount"],
            })
        return batch, created

    def confirm_compensation(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_confirm_batch(actor.role):
            raise PermissionDenied("角色无权复核确认代偿批次")
        batch, changed = self.repository.confirm_batch(batch_id, actor.user_id)
        if changed:
            self.audit.note(batch["record_id"], actor.user_id, "compensation_confirmed", {
                "batch_no": batch["batch_no"],
                "amount": batch["amount"],
            })
        return batch

    def post_recovery(self, actor: Actor, batch_id: int, payload: Dict[str, Any]) -> Any:
        """登记追偿回款；同一流水号重复提交按原回款返回。返回(回款, 是否新建)。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_post_recovery(actor.role):
            raise PermissionDenied("角色无权登记追偿回款")
        data = self.rules.validate_recovery(payload or {})
        recovery, created = self.repository.post_recovery(batch_id, data["flow_no"], float(data["amount"]), actor.user_id)
        if created:
            self.audit.note(recovery["record_id"], actor.user_id, "recovery_posted", {
                "batch_no": recovery["batch_no"],
                "flow_no": recovery["flow_no"],
                "amount": recovery["amount"],
                "applied": recovery["applied"],
                "refunded": recovery["refunded"],
                "remaining": recovery["remaining"],
            })
        return recovery, created

    def get_compensation(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        batch["recoveries"] = self.repository.list_recoveries(batch_id)
        return batch

    def list_compensations(self, actor: Actor, record_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(record_id=record_id, limit=limit)
