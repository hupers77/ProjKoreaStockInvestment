"""종목 순위 내보내기: CSV · XLSX · Markdown.

XLSX는 추가 패키지 없이 표준 라이브러리(zipfile)로 직접 만든다.
"""
import csv
import io
import zipfile
from xml.sax.saxutils import escape

GRADE_LABEL = {"S": "S", "A": "A", "B": "B", "C": "C", "X": "제외"}
HEADERS = ["순위", "종목코드", "종목명", "시장", "업종", "등급", "최종점수", "변화", "단기", "중기", "장기",
           "커버리지(%)", "현재가", "등락률(%)", "시가총액(억)", "보유", "비고"]


def _r(x, d=1):
    if x is None:
        return None
    return int(round(float(x))) if d == 0 else round(float(x), d)


def build_rows(scores, order, prev_map, held):
    """order(티커 목록) 순서대로 표 행을 만든다."""
    by = {r["ticker"]: r for r in scores}
    out = []
    for i, tk in enumerate(t for t in order if t in by):
        r = by[tk]
        prev = prev_map.get(tk)
        out.append([i + 1, tk, r["name"], r["market"], r["sector"] or "", GRADE_LABEL.get(r["grade"], r["grade"]),
                    _r(r["final"]), _r(r["final"] - prev) if prev is not None else None,
                    _r(r["s_short"]), _r(r["s_mid"]), _r(r["s_long"]), _r((r["coverage"] or 0) * 100, 0),
                    _r(r["close"], 0), _r(r["chg"], 2), _r((r["mcap"] or 0) / 1e8, 0),
                    "보유" if tk in held else "", r["knockout"] or r["gate"] or ""])
    return out


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    w.writerows(["" if v is None else v for v in row] for row in rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM: 엑셀에서 한글이 깨지지 않게


def to_md(rows, title, note):
    def cell(v):
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
    lines = [f"# {title}", "", note, "", "| " + " | ".join(HEADERS) + " |",
             "|" + "|".join("---:" if h in ("순위", "최종점수", "변화", "단기", "중기", "장기", "커버리지(%)",
                                             "현재가", "등락률(%)", "시가총액(억)") else "---" for h in HEADERS) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def to_xlsx(rows, sheet="종목순위"):
    def c(ref, v, style=0):
        st = f' s="{style}"' if style else ""
        if v is None or v == "":
            return f'<c r="{ref}"{st}/>'
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"{st}><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"{st}><is><t>{escape(str(v))}</t></is></c>'

    xml_rows = ['<row r="1">' + "".join(c(f"{_col(j)}1", h, 1) for j, h in enumerate(HEADERS)) + "</row>"]
    for i, row in enumerate(rows, start=2):
        xml_rows.append(f'<row r="{i}">' + "".join(c(f"{_col(j)}{i}", v) for j, v in enumerate(row)) + "</row>")
    widths = [6, 9, 18, 8, 14, 6, 9, 7, 7, 7, 7, 10, 10, 9, 12, 6, 40]
    cols = "".join(f'<col min="{j+1}" max="{j+1}" width="{w}" customWidth="1"/>' for j, w in enumerate(widths))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 f'<cols>{cols}</cols><sheetData>{"".join(xml_rows)}</sheetData>'
                 f'<autoFilter ref="A1:{_col(len(HEADERS)-1)}{max(len(rows)+1, 1)}"/></worksheet>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{escape(sheet)}'!$A$1:${_col(len(HEADERS)-1)}${max(len(rows)+1, 1)}</definedName></definedNames></workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE9ECF0"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()
