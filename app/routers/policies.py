"""保单路由（``/api/policies``）。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import get_db
from ..enums import InsuranceType, PaymentFrequency, PolicyStatus
from ..schemas import (
    CoverageIn,
    CoverageListOut,
    CoverageOut,
    CoverageReplaceIn,
    FieldSuggestionsOut,
    PolicyBatchUpdateIn,
    PolicyBatchUpdateOut,
    PolicyCreate,
    PolicyOut,
    PolicyUpdate,
)
from ..services import coverage_service, policy_service

router = APIRouter(prefix="/api/policies", tags=["policies"])


@router.get("", response_model=list[PolicyOut])
def list_policies(
    insured_id: Optional[str] = Query(None, alias="insuredId"),
    policyholder_id: Optional[str] = Query(None, alias="policyholderId"),
    insurance_type: Optional[InsuranceType] = Query(None, alias="insuranceType"),
    status: Optional[PolicyStatus] = Query(None, alias="status"),
    payment_frequency: Optional[PaymentFrequency] = Query(None, alias="paymentFrequency"),
    keyword: Optional[str] = Query(None, alias="keyword"),
    sort: Optional[str] = Query(None, alias="sort"),
    db: Session = Depends(get_db),
) -> list[PolicyOut]:
    """保单列表（支持筛选 / 搜索 / 排序，含全部派生字段）。

    说明（v1.3）：``insuredId`` 查询参数语义由「等于」改为「**包含（any-of）**」——
    返回「被保人集合含该成员」的全部保单。
    """
    return policy_service.list_policies(
        db,
        insured_id=insured_id,
        policyholder_id=policyholder_id,
        insurance_type=insurance_type,
        status=status,
        payment_frequency=payment_frequency,
        keyword=keyword,
        sort=sort,
    )


@router.post("", response_model=PolicyOut, status_code=201)
def create_policy(payload: PolicyCreate, db: Session = Depends(get_db)) -> PolicyOut:
    """新增保单。"""
    return policy_service.create_policy(db, payload)


@router.post("/batch-update", response_model=PolicyBatchUpdateOut)
def batch_update_policies(
    payload: PolicyBatchUpdateIn, db: Session = Depends(get_db)
) -> PolicyBatchUpdateOut:
    """批量编辑售后字段（只覆盖显式填写的字段；留空 = 不改）。"""
    return policy_service.batch_update_policies(db, payload)


# ⚠️ 声明顺序：``/suggestions`` 必须在 ``/{policy_id}`` 之前，
#    否则会被 ``{policy_id}`` 当作保单 id 吞掉（FastAPI 按声明顺序匹配）。
@router.get("/suggestions", response_model=FieldSuggestionsOut)
def get_field_suggestions(
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> FieldSuggestionsOut:
    """售后字段的常用值聚合（每个字段最多 ``limit`` 条）。"""
    return policy_service.get_field_suggestions(db, limit)


@router.get("/{policy_id}", response_model=PolicyOut)
def get_policy(policy_id: str, db: Session = Depends(get_db)) -> PolicyOut:
    """保单详情。"""
    return policy_service.get_policy(db, policy_id)


@router.put("/{policy_id}", response_model=PolicyOut)
def update_policy(
    policy_id: str, payload: PolicyUpdate, db: Session = Depends(get_db)
) -> PolicyOut:
    """编辑保单。"""
    return policy_service.update_policy(db, policy_id, payload)


@router.post("/{policy_id}/pay-next", response_model=PolicyOut)
def pay_next_period(policy_id: str, db: Session = Depends(get_db)) -> PolicyOut:
    """把「最早未缴期次」标记为已缴（写入缴费记录）。

    若保单存在逾期期次，优先补缴最早的一期；已缴清则返回 422。
    """
    return policy_service.pay_next_period(db, policy_id)


@router.delete("/{policy_id}")
def delete_policy(policy_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    """删除保单。"""
    return policy_service.delete_policy(db, policy_id)


__all__ = ["router"]


# ---------------------------------------------------------------------------
# v1.10 保障责任（条款级拆解）
# ---------------------------------------------------------------------------
@router.get("/{policy_id}/coverages", response_model=CoverageListOut)
def list_coverages(policy_id: str, db: Session = Depends(get_db)) -> CoverageListOut:
    """读取一张保单的责任明细，并附带**该险种**的预置模板。

    模板与明细一起返回，前端打开录入弹窗只需一次请求。
    """
    policy = coverage_service.require_policy(db, policy_id)
    return CoverageListOut(
        policy_id=policy_id,
        items=[
            CoverageOut.model_validate(item)
            for item in coverage_service.list_for_policy(db, policy_id)
        ],
        templates=[
            CoverageIn(**item)
            for item in coverage_service.template_for(policy.insurance_type.value)
        ],
        categories=coverage_service.categories(),
    )


@router.put("/{policy_id}/coverages", response_model=CoverageListOut)
def save_coverages(
    policy_id: str,
    payload: CoverageReplaceIn,
    db: Session = Depends(get_db),
) -> CoverageListOut:
    """**全量替换**一张保单的责任明细。

    不做逐条 diff：前端就是一张可增删的表格，整体覆盖不会出现
    「删了没删掉、改了一半」的中间态。传空数组 = 清空。
    """
    coverage_service.replace_all(
        db, policy_id, [item.model_dump() for item in payload.items]
    )
    return list_coverages(policy_id, db)
