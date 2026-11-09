"""保单 CRUD 业务逻辑。

- 支持按 ``insuredId`` / ``policyholderId`` / ``insuranceType`` / ``status`` /
  ``paymentFrequency`` 组合筛选，``keyword`` 对 ``company`` / ``productName``
  做不区分大小写的 LIKE；
- 默认排序 ``effectiveDate desc``；
- 响应装配全部派生字段（调用 ``calc.calc_schedule`` 实时计算），
  其中已缴期数支持手动覆盖（``paidPeriodsOverride``，``None`` = 按生效日自动推算）；
- ``pay_next_period`` 提供「标记最早未缴期次已缴」；
- 投保人 / 被保人不存在 → 422 ``VALIDATION_ERROR``。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import func, or_, select, text, update as sa_update
from sqlalchemy.orm import Session

from ..calc import calc_schedule
from ..enums import InsuranceType, PaymentFrequency, PolicyStatus
from ..errors import AppError
from ..models import Member, Policy, PolicyInsured
from ..schemas import (
    BATCH_UPDATE_FIELD_LIMITS,
    FieldSuggestionItem,
    FieldSuggestionsOut,
    PolicyBatchUpdateIn,
    PolicyBatchUpdateOut,
    PolicyCreate,
    PolicyOut,
    PolicyUpdate,
)
from . import attachment_service, insured_service

# 支持的排序字段（API 名 → 列）
SORT_FIELDS = {
    "effectiveDate": Policy.effective_date,
    "premiumPerPeriod": Policy.premium_per_period,
    "createdAt": Policy.created_at,
}

#: 批量编辑白名单字段（camelCase）→ ORM 列
_BATCH_FIELD_TO_COLUMN = {
    "advisorName": Policy.advisor_name,
    "advisorPhone": Policy.advisor_phone,
    "advisorWechat": Policy.advisor_wechat,
    "advisorEmail": Policy.advisor_email,
    "companyPhone": Policy.company_phone,
    "channelName": Policy.channel_name,
    "channelPhone": Policy.channel_phone,
}

#: 常用值聚合字段：camelCase 名称 → ORM 列（``companyPhone`` 刻意不参与）
_SUGGESTION_FIELD_TO_COLUMN = {
    "advisorName": Policy.advisor_name,
    "advisorPhone": Policy.advisor_phone,
    "advisorWechat": Policy.advisor_wechat,
    "advisorEmail": Policy.advisor_email,
    "channelName": Policy.channel_name,
    "channelPhone": Policy.channel_phone,
}


def _load_member(db: Session, member_id: str, label: str) -> Member:
    """按 id 加载成员，不存在则抛 422。"""
    member = db.get(Member, member_id)
    if member is None:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            f"{label}不存在：{member_id}",
            {"memberId": member_id, "role": label},
        )
    return member


def _load_insured_members(db: Session, insured_ids: list[str]) -> list[Member]:
    """逐个校验被保人存在性（不存在 → 422），返回成员列表。"""
    return [_load_member(db, mid, "被保人") for mid in insured_ids]


def _policy_insureds(db: Session, policy: Policy) -> list[Member]:
    """读取某保单的全部被保人（**规范序**，来自关联表）。

    关联表为唯一真源；为鲁棒起见，若关联行为空（如极端未回填场景），
    降级为「仅主被保人」。
    """
    insureds = insured_service.load_insured_map(db, [policy.id]).get(policy.id)
    if insureds:
        return insureds
    primary = db.get(Member, policy.insured_id)
    return [primary] if primary is not None else []


def _to_out(
    policy: Policy,
    insureds: list[Member],
    holder: Member,
    today: date,
    attachment_count: int = 0,
) -> PolicyOut:
    """ORM 保单 + 派生字段 → ``PolicyOut``。

    Args:
        policy: 保单 ORM。
        insureds: 该保单的**全部被保人**（**规范序**，含主被保人）。
        holder: 投保人。
        today: 计算基准日。
        attachment_count: 已挂载附件数。

    主被保人 = ``policy.insured_id``（真源）；``calc_schedule`` 以主被保人为基准；
    兼容字段 ``insuredId`` = ``primaryInsuredId``、``insuredName`` = 主被保人姓名。
    """
    primary = next((m for m in insureds if m.id == policy.insured_id), None)
    if primary is None:
        primary = insureds[0] if insureds else holder
    sched = calc_schedule(policy, primary, today)
    return PolicyOut(
        id=policy.id,
        company=policy.company,
        insurance_type=policy.insurance_type,
        product_name=policy.product_name,
        policyholder_id=policy.policyholder_id,
        insured_id=primary.id,
        primary_insured_id=primary.id,
        insured_ids=[m.id for m in insureds],
        insured_names=[m.name for m in insureds],
        effective_date=policy.effective_date,
        coverage_term_type=policy.coverage_term_type,
        coverage_term_value=policy.coverage_term_value,
        payment_term_type=policy.payment_term_type,
        payment_term_value=policy.payment_term_value,
        payment_frequency=policy.payment_frequency,
        premium_per_period=policy.premium_per_period,
        insured_amount=policy.insured_amount,
        status=policy.status,
        remark=policy.remark,
        paid_periods_override=policy.paid_periods_override,
        advisor_name=policy.advisor_name,
        advisor_phone=policy.advisor_phone,
        advisor_wechat=policy.advisor_wechat,
        advisor_email=policy.advisor_email,
        company_phone=policy.company_phone,
        channel_name=policy.channel_name,
        channel_phone=policy.channel_phone,
        attachment_count=attachment_count,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        annualized_premium=sched.annualized_premium,
        # v1.5：该保单投保人是否计入统计（派生展示标识；annualizedPremium 本身不过滤）
        counted_in_stats=holder.count_in_stats,
        total_periods=sched.total_periods,
        paid_periods=sched.paid_periods,
        cumulative_paid=sched.cumulative_paid,
        remaining_periods=sched.remaining_periods,
        is_paid_off=sched.is_paid_off,
        progress_percent=sched.progress_percent,
        in_coverage=sched.in_coverage,
        coverage_end_date=sched.coverage_end_date,
        next_due_date=sched.next_due_date,
        expected_periods_to_date=sched.expected_periods_to_date,
        overdue_periods=sched.overdue_periods,
        insured_name=primary.name,
        policyholder_name=holder.name,
    )


def _apply_sort(stmt, sort: Optional[str]):  # noqa: ANN001, ANN201
    """应用排序；默认 ``effectiveDate desc``。"""
    if not sort:
        return stmt.order_by(Policy.effective_date.desc(), Policy.id.asc())
    parts = [part.strip() for part in sort.split(",") if part.strip()]
    column = SORT_FIELDS.get(parts[0], Policy.effective_date)
    direction = parts[1].lower() if len(parts) > 1 else "desc"
    order = column.desc() if direction == "desc" else column.asc()
    return stmt.order_by(order, Policy.id.asc())


def _assemble(db: Session, policies: list[Policy], today: date) -> list[PolicyOut]:
    """批量装配 ``PolicyOut``（一次性加载被保人集合 + 投保人 + 附件计数，避免 N+1）。"""
    insured_map = insured_service.load_insured_map(db, [p.id for p in policies])
    member_ids = {p.policyholder_id for p in policies} | {p.insured_id for p in policies}
    for members in insured_map.values():
        member_ids.update(m.id for m in members)
    members = (
        {m.id: m for m in db.query(Member).filter(Member.id.in_(member_ids)).all()}
        if member_ids
        else {}
    )
    # 一次 GROUP BY 拿到各保单附件数，避免逐张保单查询（N+1）
    attachment_counts = attachment_service.counts_by_policy_ids(db, [p.id for p in policies])
    result: list[PolicyOut] = []
    for policy in policies:
        holder = members.get(policy.policyholder_id)
        insureds = insured_map.get(policy.id)
        if not insureds:
            # 鲁棒降级：关联表未命中 → 仅主被保人
            primary = members.get(policy.insured_id)
            insureds = [primary] if primary is not None else []
        if holder is None or not insureds:
            continue
        result.append(
            _to_out(
                policy,
                insureds,
                holder,
                today,
                attachment_count=attachment_counts.get(policy.id, 0),
            )
        )
    return result


def list_policies(
    db: Session,
    *,
    insured_id: Optional[str] = None,
    policyholder_id: Optional[str] = None,
    insurance_type: Optional[InsuranceType] = None,
    status: Optional[PolicyStatus] = None,
    payment_frequency: Optional[PaymentFrequency] = None,
    keyword: Optional[str] = None,
    sort: Optional[str] = None,
) -> list[PolicyOut]:
    """按条件查询保单列表。"""
    stmt = select(Policy)
    if insured_id:
        # v1.3：语义由「等于」→「包含（any-of）」，命中「被保人集合含该成员」的全部保单
        insured_subquery = select(PolicyInsured.policy_id).where(
            PolicyInsured.member_id == insured_id
        )
        stmt = stmt.where(Policy.id.in_(insured_subquery))
    if policyholder_id:
        stmt = stmt.where(Policy.policyholder_id == policyholder_id)
    if insurance_type is not None:
        stmt = stmt.where(Policy.insurance_type == insurance_type)
    if status is not None:
        stmt = stmt.where(Policy.status == status)
    if payment_frequency is not None:
        stmt = stmt.where(Policy.payment_frequency == payment_frequency)
    if keyword:
        like = f"%{keyword.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Policy.company).like(like),
                func.lower(Policy.product_name).like(like),
            )
        )
    stmt = _apply_sort(stmt, sort)
    policies = list(db.scalars(stmt).all())
    return _assemble(db, policies, date.today())


def get_policy(db: Session, policy_id: str) -> PolicyOut:
    """按 id 获取保单，不存在则抛 404。"""
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise AppError(404, "NOT_FOUND", f"保单不存在：{policy_id}", {"policyId": policy_id})
    insureds = _policy_insureds(db, policy)
    holder = _load_member(db, policy.policyholder_id, "投保人")
    attachment_count = attachment_service.count_by_policy(db, policy_id)
    return _to_out(policy, insureds, holder, date.today(), attachment_count=attachment_count)


def create_policy(db: Session, data: PolicyCreate) -> PolicyOut:
    """新增保单（支持多被保人 + 主被保人）。"""
    holder = _load_member(db, data.policyholder_id, "投保人")
    _load_insured_members(db, data.insured_ids or [])
    policy = Policy(
        company=data.company,
        insurance_type=data.insurance_type,
        product_name=data.product_name,
        policyholder_id=data.policyholder_id,
        insured_id=data.primary_insured_id,  # 主被保人真源
        effective_date=data.effective_date,
        coverage_term_type=data.coverage_term_type,
        coverage_term_value=data.coverage_term_value,
        payment_term_type=data.payment_term_type,
        payment_term_value=data.payment_term_value,
        payment_frequency=data.payment_frequency,
        premium_per_period=data.premium_per_period,
        insured_amount=data.insured_amount,
        status=data.status,
        remark=data.remark,
        paid_periods_override=data.paid_periods_override,
        advisor_name=data.advisor_name,
        advisor_phone=data.advisor_phone,
        advisor_wechat=data.advisor_wechat,
        advisor_email=data.advisor_email,
        company_phone=data.company_phone,
        channel_name=data.channel_name,
        channel_phone=data.channel_phone,
    )
    db.add(policy)
    db.flush()  # 拿到 policy.id（供写关联表）
    insured_service.set_policy_insureds(
        db, policy.id, data.insured_ids or [], data.primary_insured_id or ""
    )
    db.commit()
    db.refresh(policy)
    return _to_out(policy, _policy_insureds(db, policy), holder, date.today())


def update_policy(db: Session, policy_id: str, data: PolicyUpdate) -> PolicyOut:
    """编辑保单（支持多被保人 + 主被保人）。"""
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise AppError(404, "NOT_FOUND", f"保单不存在：{policy_id}", {"policyId": policy_id})
    holder = _load_member(db, data.policyholder_id, "投保人")
    _load_insured_members(db, data.insured_ids or [])

    policy.company = data.company
    policy.insurance_type = data.insurance_type
    policy.product_name = data.product_name
    policy.policyholder_id = data.policyholder_id
    policy.insured_id = data.primary_insured_id  # 主被保人真源
    policy.effective_date = data.effective_date
    policy.coverage_term_type = data.coverage_term_type
    policy.coverage_term_value = data.coverage_term_value
    policy.payment_term_type = data.payment_term_type
    policy.payment_term_value = data.payment_term_value
    policy.payment_frequency = data.payment_frequency
    policy.premium_per_period = data.premium_per_period
    policy.insured_amount = data.insured_amount
    policy.status = data.status
    policy.remark = data.remark
    policy.paid_periods_override = data.paid_periods_override
    policy.advisor_name = data.advisor_name
    policy.advisor_phone = data.advisor_phone
    policy.advisor_wechat = data.advisor_wechat
    policy.advisor_email = data.advisor_email
    policy.company_phone = data.company_phone
    policy.channel_name = data.channel_name
    policy.channel_phone = data.channel_phone
    db.flush()
    insured_service.set_policy_insureds(
        db, policy.id, data.insured_ids or [], data.primary_insured_id or ""
    )
    db.commit()
    db.refresh(policy)
    return _to_out(policy, _policy_insureds(db, policy), holder, date.today())


def delete_policy(db: Session, policy_id: str) -> dict[str, Any]:
    """删除保单，并连带清理其附件（DB 记录 + 磁盘文件）。

    顺序：先取附件 ``stored_name`` → 删附件记录 → 删保单 → ``commit``（唯一真源）
    → **best-effort** 删磁盘文件（失败只记 WARNING，不回滚、不影响返回）。
    """
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise AppError(404, "NOT_FOUND", f"保单不存在：{policy_id}", {"policyId": policy_id})
    stored_names = attachment_service.delete_attachments_for_policy(db, policy_id)
    db.delete(policy)
    db.commit()
    # DB 已提交，再 best-effort 删磁盘文件（孤儿文件无害，悬挂记录有害）
    attachment_service.remove_stored_files(stored_names)
    return {"deleted": True, "id": policy_id, "deletedAttachments": len(stored_names)}


def batch_update_policies(db: Session, data: PolicyBatchUpdateIn) -> PolicyBatchUpdateOut:
    """批量更新售后字段（三态语义，契约）。

    ``values`` 三态：

    - 键**不存在** → 不改该字段；
    - 键存在且为**字符串** → 设值（``trim`` 后为空串按「不改」处理）；
    - 键存在且为 **``null``** → **显式清空**（写 ``NULL``）。

    ``ids`` 去重后为空 → 422 ``POLICY_IDS_EMPTY``；``values`` 空对象 →
    422 ``FIELD_VALUES_EMPTY``；越界键 → 422 ``UNKNOWN_UPDATE_FIELD``。
    不存在的 id 忽略并计入 ``missingIds``，其余照常更新。
    """
    # 1) ids 去重后非空
    unique_ids = list(dict.fromkeys(data.ids))
    if not unique_ids:
        raise AppError(422, "POLICY_IDS_EMPTY", "ids 不能为空数组", None)

    # 2) values 非空
    if not data.values:
        raise AppError(422, "FIELD_VALUES_EMPTY", "values 不能为空对象", None)

    # 3) 白名单 + 长度校验
    for key, value in data.values.items():
        if key not in _BATCH_FIELD_TO_COLUMN:
            raise AppError(
                422, "UNKNOWN_UPDATE_FIELD", f"不支持的更新字段：{key}", {"field": key}
            )
        if value is not None:
            limit = BATCH_UPDATE_FIELD_LIMITS[key]
            if len(value) > limit:
                raise AppError(
                    422,
                    "VALIDATION_ERROR",
                    f"字段 {key} 超长（≤ {limit} 字符）",
                    {"field": key, "limit": limit},
                )

    # 4) 存在的 id（其余计入 missingIds）
    existing = list(db.scalars(select(Policy.id).where(Policy.id.in_(unique_ids))).all())
    existing_set = set(existing)
    missing = [pid for pid in unique_ids if pid not in existing_set]

    # 5) 逐键 UPDATE（≤ 7 条），不逐行加载 ORM
    if existing:
        for key, value in data.values.items():
            column = _BATCH_FIELD_TO_COLUMN[key]
            if value is None:
                # 显式清空
                db.execute(
                    sa_update(Policy).where(Policy.id.in_(existing)).values({column: None})
                )
            else:
                trimmed = value.strip()
                if trimmed == "":
                    continue  # trim 后为空串 → 按「不改」处理（严禁留空即清空）
                db.execute(
                    sa_update(Policy)
                    .where(Policy.id.in_(existing))
                    .values({column: trimmed})
                )
        db.commit()

    return PolicyBatchUpdateOut(
        requested=len(unique_ids),
        updated=len(existing),
        updated_ids=existing,
        missing_ids=missing,
    )


#: 常用值聚合：单次 ``UNION ALL``（``companyPhone`` 刻意不参与，避免张冠李戴）
_SUGGESTION_SQL = text(
    """
    SELECT 'advisorName'  AS field, advisor_name  AS value, COUNT(*) AS cnt
      FROM policies WHERE advisor_name  IS NOT NULL AND TRIM(advisor_name)  <> '' GROUP BY advisor_name
    UNION ALL
    SELECT 'advisorPhone', advisor_phone, COUNT(*) FROM policies
     WHERE advisor_phone IS NOT NULL AND TRIM(advisor_phone) <> '' GROUP BY advisor_phone
    UNION ALL
    SELECT 'advisorWechat', advisor_wechat, COUNT(*) FROM policies
     WHERE advisor_wechat IS NOT NULL AND TRIM(advisor_wechat) <> '' GROUP BY advisor_wechat
    UNION ALL
    SELECT 'advisorEmail', advisor_email, COUNT(*) FROM policies
     WHERE advisor_email IS NOT NULL AND TRIM(advisor_email) <> '' GROUP BY advisor_email
    UNION ALL
    SELECT 'channelName', channel_name, COUNT(*) FROM policies
     WHERE channel_name IS NOT NULL AND TRIM(channel_name) <> '' GROUP BY channel_name
    UNION ALL
    SELECT 'channelPhone', channel_phone, COUNT(*) FROM policies
     WHERE channel_phone IS NOT NULL AND TRIM(channel_phone) <> '' GROUP BY channel_phone
    ORDER BY field, cnt DESC, value ASC
    """
)


def get_field_suggestions(db: Session, limit: int = 20) -> FieldSuggestionsOut:
    """返回 6 个售后字段的「常用值」聚合（按出现次数降序、同次数按值升序）。

    Args:
        limit: **每个字段**返回的最大条数。

    Returns:
        ``FieldSuggestionsOut``；无历史值的字段为空数组。
    """
    buckets: dict[str, list[FieldSuggestionItem]] = {
        "advisorName": [],
        "advisorPhone": [],
        "advisorWechat": [],
        "advisorEmail": [],
        "channelName": [],
        "channelPhone": [],
    }
    rows = db.execute(_SUGGESTION_SQL).all()
    for field, value, cnt in rows:
        bucket = buckets.get(field)
        if bucket is None or len(bucket) >= limit:
            continue
        bucket.append(FieldSuggestionItem(value=value, count=int(cnt)))
    return FieldSuggestionsOut(
        advisor_name=buckets["advisorName"],
        advisor_phone=buckets["advisorPhone"],
        advisor_wechat=buckets["advisorWechat"],
        advisor_email=buckets["advisorEmail"],
        channel_name=buckets["channelName"],
        channel_phone=buckets["channelPhone"],
    )


def pay_next_period(db: Session, policy_id: str) -> PolicyOut:
    """把「最早未缴期次」标记为已缴。

    语义：写入 ``paid_periods_override = paid_periods + 1``，
    即「已缴至第 N 期」。若保单存在逾期期次，该操作优先补缴最早的一期。

    Raises:
        AppError: 404 保单不存在；422 已缴清 / 无缴费期次。
    """
    policy = db.get(Policy, policy_id)
    if policy is None:
        raise AppError(404, "NOT_FOUND", f"保单不存在：{policy_id}", {"policyId": policy_id})
    insureds = _policy_insureds(db, policy)
    primary = next((m for m in insureds if m.id == policy.insured_id), None)
    if primary is None:
        primary = _load_member(db, policy.insured_id, "被保人")
        insureds = [primary]
    holder = _load_member(db, policy.policyholder_id, "投保人")

    today = date.today()
    sched = calc_schedule(policy, primary, today)
    if sched.is_paid_off or sched.paid_periods >= sched.total_periods:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "该保单已缴清，无可标记的期次",
            {"policyId": policy_id, "totalPeriods": sched.total_periods},
        )

    policy.paid_periods_override = sched.paid_periods + 1
    db.commit()
    db.refresh(policy)
    return _to_out(policy, insureds, holder, today)


__all__ = [
    "list_policies",
    "get_policy",
    "create_policy",
    "update_policy",
    "delete_policy",
    "pay_next_period",
    "batch_update_policies",
    "get_field_suggestions",
]
