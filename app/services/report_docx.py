"""把托管报告导出为 Word（v1.10）。

数据来源就是 ``report_service.build_report()`` 的返回值——**页面看到的数、
导出的数、永远同一份**，不存在「页面上已经变了、文档还是旧的」。

排版取向：蓝绿配色、无花哨边框、以表格承载数据，A4 竖版。
一份完整导出通常 8–12 页，与经纪公司给的托管报告厚度相当。

实现约束：

- 只用 ``python-docx`` 的能力（含少量 OXML 直接操作：单元格底色、页脚页码）；
- 不依赖 Word 模板文件，空环境也能生成；
- 表头跨页重复（``tblHeader``），长表格翻页后仍能读懂。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Optional, Sequence

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# ---- 配色（蓝绿） ----
COLOR_TITLE = RGBColor(0x11, 0x3C, 0x6E)
COLOR_HEADING = RGBColor(0x0E, 0x5E, 0x6B)
COLOR_TEXT = RGBColor(0x1F, 0x2A, 0x37)
COLOR_MUTED = RGBColor(0x6B, 0x72, 0x80)
COLOR_DANGER = RGBColor(0xB4, 0x23, 0x18)

FILL_HEADER = "E6F1F2"
FILL_ALT = "F7FAFA"
FILL_WARN = "FFF8E6"

FONT_CN = "微软雅黑"
FONT_EN = "Segoe UI"

SUMMABLE = ("重大疾病", "身故全残", "意外")
PRIORITY_TEXT = {"high": "优先", "medium": "建议", "low": "待办"}


# ---------------------------------------------------------------------------
# 基础排版工具
# ---------------------------------------------------------------------------
def _set_run_font(run, size: float = 10.5, bold: bool = False, color: Optional[RGBColor] = None) -> None:
    """统一设置西文 / 中文字体（python-docx 不设 eastAsia 会回落到宋体）。"""
    run.font.name = FONT_EN
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CN)


def _paragraph(
    doc,
    text: str = "",
    *,
    size: float = 10.5,
    bold: bool = False,
    color: Optional[RGBColor] = None,
    align=None,
    space_before: float = 0,
    space_after: float = 4,
    indent: float = 0,
):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    if indent:
        paragraph.paragraph_format.left_indent = Cm(indent)
    if align is not None:
        paragraph.alignment = align
    if text:
        run = paragraph.add_run(text)
        _set_run_font(run, size=size, bold=bold, color=color)
    return paragraph


def _bottom_border(paragraph, color: str = "0E5E6B", size: int = 8) -> None:
    """给段落加一条下边框（章节标题的分隔线）。"""
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def _heading(doc, index: str, text: str) -> None:
    """章节标题：`一  保障总览`，带下划线分隔。"""
    paragraph = _paragraph(doc, space_before=12, space_after=8)
    run = paragraph.add_run(f"{index}  {text}")
    _set_run_font(run, size=14, bold=True, color=COLOR_HEADING)
    _bottom_border(paragraph)


def _subheading(doc, text: str) -> None:
    _paragraph(doc, text, size=11, bold=True, color=COLOR_TEXT, space_before=8, space_after=4)


def _shade(cell, hex_fill: str) -> None:
    """给单元格填底色。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _repeat_header(row) -> None:
    """表头跨页重复。"""
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _cell_text(cell, text: str, *, bold: bool = False, color: Optional[RGBColor] = None,
               size: float = 9.5, align=None) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_before = Pt(1)
    paragraph.paragraph_format.space_after = Pt(1)
    if align is not None:
        paragraph.alignment = align
    run = paragraph.add_run(text)
    _set_run_font(run, size=size, bold=bold, color=color)


def _table(doc, headers: Sequence[str], rows: Sequence[Sequence[str]], widths: Sequence[float],
           *, right_columns: Sequence[int] = (), font_size: float = 9.5) -> None:
    """建一张表：首行表头（浅青底、加粗），隔行浅底，右对齐列由 ``right_columns`` 指定。"""
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    header_row = table.rows[0]
    _repeat_header(header_row)
    for index, text in enumerate(headers):
        cell = header_row.cells[index]
        _shade(cell, FILL_HEADER)
        _cell_text(
            cell,
            text,
            bold=True,
            color=COLOR_HEADING,
            size=font_size,
            align=WD_ALIGN_PARAGRAPH.RIGHT if index in right_columns else WD_ALIGN_PARAGRAPH.LEFT,
        )
        cell.width = Cm(widths[index])

    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for index, value in enumerate(values):
            cell = cells[index]
            if row_index % 2 == 1:
                _shade(cell, FILL_ALT)
            _cell_text(
                cell,
                str(value),
                size=font_size,
                align=WD_ALIGN_PARAGRAPH.RIGHT if index in right_columns else WD_ALIGN_PARAGRAPH.LEFT,
            )
            cell.width = Cm(widths[index])


def _bullet(doc, text: str, *, color: Optional[RGBColor] = None, size: float = 10) -> None:
    paragraph = _paragraph(doc, f"· {text}", size=size, color=color, indent=0.4, space_after=2)
    return paragraph


def _add_page_number_footer(section) -> None:
    """页脚：`家庭保障检视报告 · 第 N 页`。"""
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("家庭保障检视报告 · 第 ")
    _set_run_font(run, size=9, color=COLOR_MUTED)

    field_run = paragraph.add_run()
    _set_run_font(field_run, size=9, color=COLOR_MUTED)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    field_run._r.append(begin)
    field_run._r.append(instr)
    field_run._r.append(end)

    tail = paragraph.add_run(" 页")
    _set_run_font(tail, size=9, color=COLOR_MUTED)


def _wan(value: Any) -> str:
    """金额按万展示（与页面一致）：``None`` → 「—」，``0`` → 「0」。"""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number == 0:
        return "0"
    if abs(number) < 10000:
        return f"{number:,.0f} 元"
    ten_thousand = number / 10000
    text = f"{ten_thousand:.0f}" if ten_thousand >= 100 else f"{ten_thousand:.1f}"
    return f"{text} 万元"


def _money(value: Any) -> str:
    try:
        return f"¥{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


def _protection_total(cells: Sequence[dict]) -> float:
    return sum(
        float(cell.get("amount") or 0)
        for cell in cells
        if cell.get("category") in SUMMABLE
    )


# ---------------------------------------------------------------------------
# 各章节
# ---------------------------------------------------------------------------
def _cover(doc, data: dict) -> None:
    meta = data["meta"]
    _paragraph(doc, data["meta"]["familyName"], size=13, bold=True, color=COLOR_MUTED,
               align=WD_ALIGN_PARAGRAPH.CENTER, space_before=18, space_after=2)
    _paragraph(
        doc,
        "家庭保障检视报告",
        size=26,
        bold=True,
        color=COLOR_TITLE,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=6,
    )
    _paragraph(
        doc,
        f"报告编号 {meta['reportNo']}　|　生成日期 {str(meta['generatedAt'])[:10]}　|　版本 v{meta['version']}",
        size=9,
        color=COLOR_MUTED,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_after=14,
    )
    _paragraph(
        doc,
        "本报告按当前录入数据实时汇总生成，不是存档快照。保单新增、到期、退保或补录责任后，"
        "重新导出即为最新结果。",
        size=9.5,
        color=COLOR_MUTED,
        space_after=10,
    )

    warnings = data.get("warnings") or []
    if warnings:
        for text in warnings:
            paragraph = _paragraph(doc, text, size=9, color=COLOR_DANGER, space_after=2, indent=0.2)


def _overview(doc, data: dict) -> None:
    overview = data["overview"]
    _heading(doc, "一", "保障总览")

    recorded_total = overview["coverageRecorded"] + overview["coverageMissing"]
    cards = [
        ("家庭成员", f"{overview['memberCount']} 人"),
        ("保单总数", f"{overview['policyCount']} 张（在保 {overview['inForceCount']}）"),
        ("年化保费", _money(overview["annualPremium"])),
        ("给付型保额", _wan(overview["totalSumInsured"])),
        ("责任已录", f"{overview['coverageRecorded']}/{recorded_total}"),
    ]
    _table(
        doc,
        ["指标", "数值", "指标", "数值"],
        [
            [cards[0][0], cards[0][1], cards[3][0], cards[3][1]],
            [cards[1][0], cards[1][1], cards[4][0], cards[4][1]],
            [cards[2][0], cards[2][1], "", ""],
        ],
        [3.6, 5.6, 3.6, 4.4],
    )

    _subheading(doc, "成员 × 保障类别矩阵")
    headers = ["成员"] + list(data["matrixCategories"]) + ["给付合计"]
    rows = []
    for member in data["members"]:
        row = [f"{member['name']}（{member['relation']}）"]
        for cell in member["cells"]:
            if not cell["has"]:
                row.append("—")
            elif not cell["amount"]:
                row.append("待补金额")
            else:
                row.append(_wan(cell["amount"]))
        row.append(_wan(_protection_total(member["cells"])))
        rows.append(row)

    if rows:
        _table(doc, headers, rows, [3.6, 2.3, 2.2, 2.2, 2.4, 2.2, 2.3],
               right_columns=range(1, len(headers)))
    else:
        _paragraph(doc, "还没有家庭成员。", color=COLOR_MUTED)

    _paragraph(
        doc,
        "「—」表示没有该类保障；「待补金额」表示有该责任但未填金额或比例。"
        "医疗为报销限额，单列不并入给付合计；多被保人保单保额记在主被保人名下。",
        size=8.5,
        color=COLOR_MUTED,
        space_before=4,
    )


def _gaps(doc, data: dict) -> None:
    _heading(doc, "二", "保障缺口分析")
    params = data["params"]

    _subheading(doc, "2.1 测算参数")
    _table(
        doc,
        ["家庭年收入", "负债余额", "年生活支出", "是否测算"],
        [[
            _money(params["annualIncome"]) if params["annualIncome"] else "未填",
            _money(params["totalDebt"]) if params["totalDebt"] else "未填",
            _money(params["annualExpense"]) if params["annualExpense"] else "未填",
            "已测算" if params["filled"] else "未填年收入，只做结构缺口",
        ]],
        [4.0, 4.0, 4.0, 5.2],
    )

    _subheading(doc, "2.2 额度缺口")
    amount_gaps = data["gaps"]["amount"]
    if amount_gaps:
        _table(
            doc,
            ["成员", "项目", "现有", "参考建议", "缺口", "说明"],
            [
                [
                    row["memberName"],
                    row["item"],
                    _wan(row["current"]),
                    _wan(row["suggested"]),
                    _wan(row["gap"]),
                    row.get("note") or "",
                ]
                for row in amount_gaps
            ],
            [2.4, 2.5, 2.5, 2.7, 2.5, 4.6],
            right_columns=(2, 3, 4),
        )
    else:
        _paragraph(
            doc,
            "各项保障已达参考值，或尚未填写家庭年收入（额度缺口无法测算）。",
            color=COLOR_MUTED,
        )

    _subheading(doc, "2.3 结构缺口")
    structure = data["gaps"]["structure"]
    if structure:
        for row in structure:
            _bullet(doc, f"{row['memberName']}：{row['message']}",
                    color=COLOR_DANGER if row["severity"] == "high" else None)
    else:
        _paragraph(doc, "结构上没有明显空白。", color=COLOR_MUTED)

    _paragraph(
        doc,
        "参考值口径：重疾 3 倍年收入（不低于 30 万）；寿险「负债余额 + 5 倍年支出」，仅测算本人与配偶；"
        "意外 5 倍年收入（不低于 50 万）；未成年人重疾按 50 万、意外按身故给付监管上限。"
        "以上为行业通行经验值，不是监管标准。",
        size=8.5,
        color=COLOR_MUTED,
        space_before=4,
    )


def _diagnosis(doc, data: dict) -> None:
    """保障诊断小结：服务端生成的自然语言结论，页面与 Word 同源。"""
    items = data.get("diagnosis") or []
    _heading(doc, "三", "保障诊断小结")
    if not items:
        _paragraph(doc, "暂无可分析的结论。", color=COLOR_MUTED)
        return

    for item in items:
        tone = item.get("tone", "info")
        color = COLOR_DANGER if tone == "warn" else (COLOR_HEADING if tone == "good" else COLOR_TEXT)
        _paragraph(doc, f"· {item['title']}", size=10.5, bold=True, color=color,
                   space_before=6, space_after=2, indent=0.2)
        _paragraph(doc, item["text"], size=10, color=COLOR_TEXT, indent=0.6, space_after=4)

    _paragraph(doc, "结论由当前数据自动生成，不涉及任何投资或投保建议。",
               size=8.5, color=COLOR_MUTED, space_before=4)


def _members(doc, data: dict) -> None:
    _heading(doc, "四", "成员保障分析")
    if not data["members"]:
        _paragraph(doc, "还没有家庭成员。", color=COLOR_MUTED)
        return

    for member in data["members"]:
        paragraph = _paragraph(doc, space_before=10, space_after=4)
        run = paragraph.add_run(
            f"{member['name']}（{member['relation']}"
            + (f"，{member['age']} 岁" if member["age"] is not None else "")
            + f"）　保单 {member['policyCount']} 张　年化保费 {_money(member['annualPremium'])}"
            + f"　给付合计 {_wan(_protection_total(member['cells']))}"
        )
        _set_run_font(run, size=11, bold=True, color=COLOR_TEXT)

        covering = [c for c in member["cells"] if c["has"]]
        if covering:
            text = "　".join(
                f"{c['category']} {_wan(c['amount']) if c['amount'] else '待补金额'}" for c in covering
            )
            _paragraph(doc, f"已有保障：{text}", size=9.5, indent=0.2, space_after=2)
        else:
            _paragraph(doc, "名下暂无可计入的在保保障。", size=9.5, color=COLOR_MUTED,
                       indent=0.2, space_after=2)

        for gap in member["structureGaps"]:
            _bullet(doc, gap, color=COLOR_DANGER, size=9.5)
        for note in member.get("sharedNotes") or []:
            _bullet(doc, note, color=COLOR_MUTED, size=9.5)
        for gap in member["amountGaps"]:
            _bullet(
                doc,
                f"{gap['item']}：现有 {_wan(gap['current'])}，参考 {_wan(gap['suggested'])}，"
                f"缺口约 {_wan(gap['gap'])}"
                + (f"（{gap['note']}）" if gap.get("note") else ""),
                color=COLOR_DANGER,
                size=9.5,
            )

        for policy in member["policies"]:
            _paragraph(
                doc,
                f"{policy['productName']}　{policy['company']}　{policy['insuranceType']}　{policy['status']}",
                size=9.5,
                bold=True,
                space_before=6,
                space_after=2,
                indent=0.2,
            )
            if policy["items"]:
                _table(
                    doc,
                    ["责任", "类别", "比例", "金额", "次数"],
                    [
                        [
                            item["name"],
                            item["category"],
                            f"{item['ratio']}%" if item["ratio"] else "—",
                            _wan(item["effectiveAmount"]),
                            f"{item['times']} 次" if item["times"] else "—",
                        ]
                        for item in policy["items"]
                    ],
                    [6.4, 3.0, 2.2, 3.2, 2.0],
                    right_columns=(2, 3, 4),
                    font_size=9,
                )
            else:
                _paragraph(
                    doc,
                    "未录入保障责任，本报告按险种与保额粗算。",
                    size=9,
                    color=COLOR_MUTED,
                    indent=0.4,
                )


def _premium(doc, data: dict) -> None:
    _heading(doc, "五", "保费分布")
    premium = data["premium"]
    ratio = data["overview"].get("premiumRatio")
    if ratio is None:
        _paragraph(doc, "未填写家庭年收入，暂不评估保费负担率（经验区间为年收入的 5%–15%）。",
                   size=9.5, color=COLOR_MUTED, space_after=6)
    else:
        level = "偏高" if ratio > 0.15 else ("偏低" if ratio < 0.05 else "在合理区间")
        _paragraph(
            doc,
            f"保费负担率 {ratio * 100:.1f}%：年化保费约占家庭年收入的这个比例，"
            f"经验区间为 5%–15%，当前{level}。",
            size=9.5,
            color=COLOR_TEXT,
            space_after=6,
        )

    for title, rows, label in (
        ("5.1 按投保人", premium["byMember"], "投保人"),
        ("5.2 按险种", premium["byType"], "险种"),
    ):
        _subheading(doc, title)
        if rows:
            _table(
                doc,
                [label, "年化保费", "占比"],
                [[row["name"], _money(row["amount"]), f"{row['ratio'] * 100:.1f}%"] for row in rows],
                [7.0, 5.0, 3.0],
                right_columns=(1, 2),
            )
        else:
            _paragraph(doc, "暂无数据。", color=COLOR_MUTED)

    _subheading(doc, "5.3 全年支出节奏")
    months = premium["byMonth"]
    if months:
        _table(
            doc,
            [f"{row['month']} 月" for row in months],
            [[_wan(row["amount"]) for row in months]],
            [1.25] * len(months),
            right_columns=range(len(months)),
            font_size=8,
        )
        _paragraph(doc, "月缴保费平摊到 12 个月，年缴 / 趸交落在保单生效月；合计等于年化保费。",
                   size=8.5, color=COLOR_MUTED, space_before=4)


def _policies(doc, data: dict) -> None:
    _heading(doc, "六", "保单汇总")
    rows = data["policies"]
    if not rows:
        _paragraph(doc, "还没有保单。", color=COLOR_MUTED)
        return

    _table(
        doc,
        ["产品", "公司", "险种", "状态", "被保人", "生效日", "保障至", "保额", "年化保费"],
        [
            [
                row["productName"],
                row["company"],
                row["insuranceType"],
                row["status"] + ("（已过期）" if row["expired"] else ""),
                "、".join(row["insuredNames"]) or "—",
                str(row["effectiveDate"]),
                str(row["coverageEndDate"]) if row["coverageEndDate"] else "终身",
                _wan(row["insuredAmount"]),
                _money(row["annualizedPremium"]) if row["counted"] else "—",
            ]
            for row in rows
        ],
        [3.0, 1.9, 1.4, 1.9, 1.9, 1.9, 1.9, 1.7, 1.6],
        right_columns=(7, 8),
        font_size=8.5,
    )
    _paragraph(
        doc,
        "已退保、已失效或已过保障期的保单不计入保障与保费合计。",
        size=8.5,
        color=COLOR_MUTED,
        space_before=4,
    )


def _payment_plan(doc, data: dict) -> None:
    plan = data["paymentPlan"]
    _heading(doc, "七", f"交费计划（未来 {plan['horizonDays']} 天）")
    items = plan["items"]
    if not items:
        _paragraph(doc, "未来一年没有待缴保费（可能都已缴清）。", color=COLOR_MUTED)
        return

    _table(
        doc,
        ["缴费日", "产品", "公司", "期次", "金额"],
        [
            [
                str(row["dueDate"]),
                row["productName"],
                row["company"],
                f"第 {row['periodIndex']} 期",
                _money(row["amount"]),
            ]
            for row in items
        ],
        [2.8, 5.8, 3.6, 2.3, 2.7],
        right_columns=(3, 4),
    )
    _paragraph(doc, f"合计 {_money(plan['total'])}", size=10, bold=True, color=COLOR_HEADING,
               space_before=4)


def _actions(doc, data: dict) -> None:
    _heading(doc, "八", "行动清单")
    actions = data["actions"]
    if not actions:
        _paragraph(doc, "按当前数据没有需要立刻处理的事项。", color=COLOR_MUTED)
        return

    _table(
        doc,
        ["优先级", "事项", "说明"],
        [
            [
                PRIORITY_TEXT.get(row["priority"], row["priority"]),
                row["title"],
                row["detail"],
            ]
            for row in actions
        ],
        [2.0, 6.2, 8.6],
        font_size=9,
    )


def _notes(doc, data: dict) -> None:
    overview = data["overview"]
    _heading(doc, "九", "测算口径与说明")
    notes = [
        "本报告按录入数据实时汇总生成，不是存档快照；保单信息变动后重新导出即为新数。",
        f"仅「生效中 / 已缴清」且在保障期内的保单计入合计；本次共排除 "
        f"{overview['endedCount'] + overview['expiredCount']} 张"
        f"（退保/失效 {overview['endedCount']} 张、已过期 {overview['expiredCount']} 张）。",
        "给付型保额 = 重大疾病 + 身故全残 + 意外；医疗险为报销型限额，不可叠加，单列展示。",
        "多被保人保单保额统一记在主被保人名下，其他被保人份额请对照条款核对。",
        f"{overview['coverageMissing']} 张保单未录入责任明细，相关金额按险种与保额粗算，精度有限。",
        "额度缺口为行业通行经验值，不是监管标准，也不构成投保建议；实际投保需结合体况、预算与条款。",
        "本报告不含现金价值、生存金、分红与增值服务信息——这些数据不在工具范围内，不做推测。",
    ]
    for index, text in enumerate(notes, start=1):
        _paragraph(doc, f"{index}. {text}", size=9, color=COLOR_TEXT, indent=0.3, space_after=3)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_document(data: dict) -> BytesIO:
    """按报告数据生成 .docx，返回可直接流式响应的 ``BytesIO``。"""
    document = Document()

    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(1.9)
    section.right_margin = Cm(1.9)
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.6)

    normal = document.styles["Normal"]
    normal.font.name = FONT_EN
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CN)
    normal.paragraph_format.line_spacing = 1.35

    _add_page_number_footer(section)

    _cover(document, data)
    _overview(document, data)
    _gaps(document, data)
    _diagnosis(document, data)
    _members(document, data)
    _premium(document, data)
    _policies(document, data)
    _payment_plan(document, data)
    _actions(document, data)
    _notes(document, data)

    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


__all__ = ["build_document"]
