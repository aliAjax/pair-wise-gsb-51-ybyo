"""担保代偿额度、代偿批次与追偿回款的领域规则。"""
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from .domain import ValidationError, number, text


BATCH_PENDING = "pending"        # 已提交、预占额度，待复核
BATCH_CONFIRMED = "confirmed"    # 复核确认，占用正式生效
BATCH_REJECTED = "rejected"      # 复核驳回，释放预占
BATCH_VOIDED = "voided"          # 方案失效等原因作废，释放预占
BATCH_ACTIVE_STATUSES = (BATCH_PENDING, BATCH_CONFIRMED)
BATCH_STATUS_LABELS = {
    BATCH_PENDING: "待复核",
    BATCH_CONFIRMED: "已确认",
    BATCH_REJECTED: "已驳回",
    BATCH_VOIDED: "已作废",
}

MONEY_TOLERANCE = 0.005


class GuaranteeRules:
    """代偿与回款的纯规则计算，不访问数据库。"""

    def default_year(self) -> int:
        return datetime.now(timezone.utc).year

    def validate_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        batch_no = text(payload, "batch_no")
        guarantor_code = text(payload, "guarantor_code")
        amount = round(number(payload, "amount", 0.01), 2)
        year = payload.get("year", self.default_year())
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ValidationError("year必须是2000-2100之间的整数")
        return {"batch_no": batch_no, "guarantor_code": guarantor_code, "amount": amount, "year": year}

    def validate_recovery(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        serial_no = text(payload, "serial_no")
        amount = round(number(payload, "amount", 0.01), 2)
        return {"serial_no": serial_no, "amount": amount}

    @staticmethod
    def allocate(outstanding: float, received: float) -> Tuple[float, float]:
        """回款冲减：返回(实际冲减金额, 超额退回金额)，不足时退回为0并保留差额。"""
        outstanding = round(float(outstanding), 2)
        received = round(float(received), 2)
        if received + MONEY_TOLERANCE >= outstanding:
            return outstanding, round(max(0.0, received - outstanding), 2)
        return received, 0.0
