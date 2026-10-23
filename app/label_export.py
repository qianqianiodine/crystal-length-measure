"""把选中的照片名字导成一张清单（用户口中的「标签」）。

用户的照片名字就是初筛来源的孔位（例如 `50-JBScreen-H-9-1`），这个模块只把
名字按 Excel / txt 列出来，方便他回头翻每张照片的条件。

纯函数：不碰数据库、不写磁盘。写文件在 `app/api/images.py` 的端点里做。

Excel 用 stdlib 的 `zipfile` 现场拼一个最小 xlsx，**不依赖 openpyxl** ——
同事包里那个自带 Python 没有 pip，装不了第三方库（见 wiki 的打包约定）。
"""
import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from app.exporter import UNCATEGORIZED


def folder_paths(folders, member_ids):
    """把文件夹 id 还原成**完整路径**（`2026/20260922_IDO1_KK_28~30/28`）。

    用库里原来的名字，不做 `_safe_segment` 那套洗白 —— 这是给人看的清单，
    不是磁盘路径。带环的护栏和 exporter.export_plan 一样，数据被外部改坏时不转死。
    """
    by_id = {r["id"]: r for r in folders}
    out = []
    for fid in member_ids:
        parts, seen, cur = [], set(), fid
        while cur is not None and cur not in seen:
            seen.add(cur)
            row = by_id.get(cur)
            if row is None:          # 文件夹被删了，但关系还在（防一手）
                break
            parts.append(str(row["name"]))
            cur = row["parent_id"]
        if parts:
            out.append("/".join(reversed(parts)))
    return out


def build_rows(images, folders, members) -> list[dict]:
    """算出每张照片要导出的两样东西：名字、文件夹（多选时用「、」连起来）。

    images:  sqlite3.Row 列表，每行要 id / name / original_filename
    folders: sqlite3.Row 列表，每行要 id / name / parent_id（整张 folders 表）
    members: {image_id: [folder_id, ...]}
    """
    rows = []
    for img in images:
        label = (img["name"] or "").strip()
        if not label:                # 理论上名字不会空，空了退回原文件名
            label = Path(img["original_filename"] or "").stem or "未命名"
        paths = folder_paths(folders, members.get(img["id"], []))
        rows.append({"label": label,
                     "folder": "、".join(paths) or UNCATEGORIZED})
    return rows


def to_txt(rows, with_folders: bool) -> str:
    """纯文本。带上文件夹时，文件夹名单独一行、只出现一次，下面是它的标签。

    例：
        2026/20261020_TREX1_16
        50-JBScreen-H-9-1
        50-JBScreen-G-3-1
    """
    if not rows:
        return ""
    if not with_folders:
        return "\n".join(r["label"] for r in rows) + "\n"

    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(r["folder"], []).append(r["label"])
    return "\n\n".join(f"{folder}\n" + "\n".join(labels)
                       for folder, labels in groups.items()) + "\n"


# ---------------- 最小 xlsx ----------------
#
# 结构：6 个小 XML 打进一个 zip。`inlineStr` 直接写字符串，不用 sharedStrings。
# 这条路线在 Excel / WPS 里都能打开；测试用 openpyxl 读回来核对每一格。

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="标签" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _cell(ref: str, text: str, header: bool = False) -> str:
    style = ' s="1"' if header else ""
    return (f'<c r="{ref}" t="inlineStr"{style}>'
            f'<is><t xml:space="preserve">{escape(text)}</t></is></c>')


def to_xlsx(rows, with_folders: bool) -> bytes:
    """一列「标签」，或两列「标签 + 文件夹」。表头加粗。"""
    headers = ["标签"] + (["文件夹"] if with_folders else [])
    cols = ['<col min="1" max="1" width="40" customWidth="1"/>']
    if with_folders:
        cols.append('<col min="2" max="2" width="70" customWidth="1"/>')

    lines = ['<row r="1">' +
             "".join(_cell(chr(65 + i) + "1", h, True)
                     for i, h in enumerate(headers)) +
             "</row>"]
    for n, r in enumerate(rows, start=2):
        cells = [_cell(f"A{n}", r["label"])]
        if with_folders:
            cells.append(_cell(f"B{n}", r["folder"]))
        lines.append(f'<row r="{n}">' + "".join(cells) + "</row>")

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<cols>' + "".join(cols) + '</cols>'
             '<sheetData>' + "".join(lines) + '</sheetData>'
             '</worksheet>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
