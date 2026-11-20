"""保单托管报告路由（``/api/report``，v1.10）。

三个端点都不写报告快照：

- ``GET /api/report``：**现算**。前端每次进入报告页都重新拉，新增 / 到期 / 退保
  立刻反映在结果里；
- ``PUT /api/report/params``：保存测算参数（年收入 / 负债 / 年支出），
  只写这三个键；
- ``GET /api/report/export.docx``：按同一份现算结果导出 Word，
  与页面看到的数一模一样（不读缓存，避免「页面已变、文档还是旧的」）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..schemas import ReportOut, ReportParamsIn, ReportParamsOut
from ..services import report_service

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("", response_model=ReportOut)
def get_report(db: Session = Depends(get_db)) -> ReportOut:
    """生成一份完整的家庭保障检视报告（实时聚合，无缓存）。"""
    return ReportOut(**report_service.build_report(db))


@router.put("/params", response_model=ReportParamsOut)
def save_params(payload: ReportParamsIn, db: Session = Depends(get_db)) -> ReportParamsOut:
    """保存缺口测算参数。

    只提交改过的字段即可：未传的键保持原值，传 ``null`` 表示清空。
    """
    changes = {
        name: value
        for name, value in payload.model_dump(exclude_unset=True).items()
        if name in report_service.DEFAULTS
    }
    params = report_service.save_params(db, changes)
    return ReportParamsOut(**params.as_dict())


@router.get("/export.docx")
def export_docx(db: Session = Depends(get_db)) -> StreamingResponse:
    """导出 Word 版报告（与页面同源数据，导出即刻生成）。"""
    from ..services import report_docx

    data = report_service.build_report(db)
    buffer = report_docx.build_document(data)
    filename = f"{data['meta']['familyName']}-保障检视报告.docx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                "attachment; filename=report.docx; "
                f"filename*=UTF-8''{_quote(filename)}"
            )
        },
    )


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


__all__ = ["router"]
