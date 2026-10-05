"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, number, optional_text, text
from .guarantees import GuaranteeRules
from .repository import Repository
from .rules import DomainRules


# 担保相关角色与动作权限
GUARANTEE_ROLES = {"guarantor_officer", "guarantor_reviewer", "guarantee_admin"}
GUARANTEE_ACTION_ROLES = {
    "create_guarantor": {"guarantee_admin"},
    "configure_quota": {"guarantee_admin"},
    "submit_compensation": {"guarantor_officer", "guarantee_admin"},
    "review_batch": {"guarantor_reviewer", "guarantee_admin"},
    "register_recovery": {"guarantor_officer", "guarantee_admin"},
}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 guarantee_rules: GuaranteeRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.guarantee_rules = guarantee_rules or GuaranteeRules()
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role) and actor.role not in GUARANTEE_ROLES:
            raise PermissionDenied("角色无权访问该服务")

    def _require_guarantee_action(self, actor: Actor, action: str) -> None:
        if actor.role != "admin" and actor.role not in GUARANTEE_ACTION_ROLES.get(action, set()):
            raise PermissionDenied("角色无权执行%s" % action)

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
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        # 方案失效：未确认（仅预占）的代偿批次一律作废，额度按状态重算
        if new_state == "defaulted":
            self.repository.void_pending_batches(
                record_id, actor.user_id,
                optional_text(data or {}, "default_reason", "纾困方案失效，未确认批次自动作废"),
            )
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ----- 担保机构与额度池 -----

    def create_guarantor(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_guarantee_action(actor, "create_guarantor")
        code = text(payload or {}, "code")
        name = text(payload or {}, "name")
        return self.repository.create_guarantor(code, name, actor.user_id)

    def list_guarantors(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_guarantors()

    def configure_quota(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_guarantee_action(actor, "configure_quota")
        payload = payload or {}
        year = payload.get("year", self.guarantee_rules.default_year())
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ValidationError("year必须是2000-2100之间的整数")
        total_quota = number(payload, "total_quota", 0)
        return self.repository.upsert_quota(year, total_quota, actor.user_id)

    def quota_overview(self, actor: Actor, year: int = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.quota_overview(year)

    # ----- 代偿批次 -----

    def submit_compensation(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """提交代偿：预占年度共享额度。批次号幂等，重试返回原批次，不重复占用。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_guarantee_action(actor, "submit_compensation")
        data = self.guarantee_rules.validate_batch(payload or {})
        record_id = (payload or {}).get("record_id")
        if record_id is not None:
            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
                raise ValidationError("record_id必须是正整数")
            record = self.repository.get(record_id)
            if record["state"] == "defaulted":
                raise Conflict("纾困方案已失效，不能提交代偿")
        note = optional_text(payload or {}, "note")
        existing = self.repository.get_batch_by_no(data["batch_no"])
        if existing is not None:
            # 写入失败后的重试：按原批次恢复，不重复占用额度
            if (existing["guarantor_code"] != data["guarantor_code"] or existing["year"] != data["year"]
                    or abs(existing["amount"] - data["amount"]) > 0.005
                    or (existing["record_id"] is not None and existing["record_id"] != record_id)):
                raise Conflict("批次号已存在，且与本次提交内容不一致")
            existing["idempotent_hit"] = True
            return existing
        return self.repository.submit_compensation(
            data["batch_no"], data["guarantor_code"], data["year"], record_id, data["amount"],
            actor.user_id, note,
        )

    def list_batches(self, actor: Actor, status: str = None, guarantor_code: str = None,
                     year: int = None, record_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(status=status, guarantor_code=guarantor_code, year=year,
                                            record_id=record_id, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch(batch_id)

    def review_batch(self, actor: Actor, batch_id: int, approve: bool, data: Dict[str, Any]) -> Dict[str, Any]:
        """复核确认后预占才转为有效占用；驳回则释放额度。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_guarantee_action(actor, "review_batch")
        note = optional_text(data or {}, "review_note", "复核%s" % ("通过" if approve else "驳回"))
        return self.repository.decide_batch(batch_id, bool(approve), actor.user_id, note)

    # ----- 追偿回款 -----

    def register_recovery(self, actor: Actor, batch_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """同一流水只匹配一笔回款：不足留差额，超额退回并显示剩余。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_guarantee_action(actor, "register_recovery")
        data = self.guarantee_rules.validate_recovery(payload or {})
        existing = self.repository.get_recovery_by_serial(data["serial_no"])
        if existing is not None:
            if existing["batch_id"] != int(batch_id) or abs(existing["amount"] - data["amount"]) > 0.005:
                raise Conflict("回款流水号已存在，且与本次提交内容不一致")
            existing["idempotent_hit"] = True
            return existing
        return self.repository.register_recovery(
            data["serial_no"], int(batch_id), data["amount"], actor.user_id,
        )

    def list_recoveries(self, actor: Actor, batch_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_recoveries(batch_id=batch_id, limit=limit)

    # ----- 统计 -----

    def guarantee_stats(self, actor: Actor, year: int = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.guarantee_stats(year)
