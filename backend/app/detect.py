"""Identify which Express report an uploaded file is, from its CONTENT.

The user uploads three files in any order and under any filename. We must not
trust the filename: these are re-exported and renamed constantly ("ขายสด.csv",
"cash (1).csv", "report.csv"). The report title on line 2 is printed by Express
itself and is the reliable signal.

Two independent signals are checked and must agree:

    title  -- the Thai report name in the page header
    prefix -- the document-number prefix in the body (HS / IV / AI)

If they disagree we refuse the file rather than guess, because guessing wrong
routes a credit report through the cash parser and silently produces a
plausible-looking but wrong revenue number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SOURCE_ENCODING = "cp874"

# Line 2 of every Express printed report.
TITLE_MARKERS = {
    "cash": "รายงานขายเงินสด",
    "credit": "รายงานใบกำกับสินค้า",
    "deposit": "รายงานใบรับมัดจำ",
}

# Document-number prefixes found in the body of each report.
PREFIX_MARKERS = {"HS": "cash", "IV": "credit", "AI": "deposit"}

DOC_NO_RE = re.compile(r"\b(HS|IV|AI)\d{4}/\d{4}\b")

THAI_LABEL = {
    "cash": "ขายเงินสด",
    "credit": "ขายเงินเชื่อ",
    "deposit": "รับมัดจำ",
}

# Read enough to see the title and a useful sample of document numbers, but not
# the whole 4.6 MB cash file -- detection must be cheap.
SNIFF_BYTES = 200_000


@dataclass
class Detection:
    kind: str | None          # cash | credit | deposit | None
    confidence: str           # both | title_only | prefix_only | none
    title_kind: str | None
    prefix_kind: str | None
    n_doc_numbers: int
    reason_th: str            # shown to the user, in Thai
    reason_en: str


def decode(raw: bytes) -> str:
    """Decode an Express export.

    cp874 is what Express writes. utf-8 is accepted because a file that has
    been opened and re-saved in Excel or Google Sheets often comes back as
    utf-8, and rejecting those would be an unhelpful surprise.
    """
    for enc in (SOURCE_ENCODING, "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode(SOURCE_ENCODING, errors="replace")


def detect(raw: bytes) -> Detection:
    text = decode(raw[:SNIFF_BYTES])

    title_kind = None
    head = "\n".join(text.splitlines()[:40])
    for kind, marker in TITLE_MARKERS.items():
        if marker in head:
            title_kind = kind
            break

    found = DOC_NO_RE.findall(text)
    prefix_kind = None
    if found:
        # Most common prefix wins; stray references to other documents (a
        # deposit voucher cited inside a sales report) must not flip the call.
        top = max(set(found), key=found.count)
        prefix_kind = PREFIX_MARKERS.get(top)

    if title_kind and prefix_kind and title_kind == prefix_kind:
        return Detection(title_kind, "both", title_kind, prefix_kind, len(found),
                         f"ตรวจพบรายงาน{THAI_LABEL[title_kind]}",
                         f"detected {title_kind} report (title and document numbers agree)")

    if title_kind and prefix_kind and title_kind != prefix_kind:
        return Detection(None, "conflict", title_kind, prefix_kind, len(found),
                         f"ไฟล์นี้ไม่สอดคล้องกัน: หัวรายงานเป็น{THAI_LABEL[title_kind]} "
                         f"แต่เลขที่เอกสารเป็นของ{THAI_LABEL[prefix_kind]} "
                         f"กรุณาส่งออกไฟล์จาก Express ใหม่อีกครั้ง",
                         f"conflict: title says {title_kind}, document numbers say {prefix_kind}")

    if title_kind:
        return Detection(title_kind, "title_only", title_kind, None, len(found),
                         f"ตรวจพบรายงาน{THAI_LABEL[title_kind]} (ไม่พบเลขที่เอกสาร)",
                         f"detected {title_kind} from title only; no document numbers found")

    if prefix_kind:
        return Detection(prefix_kind, "prefix_only", None, prefix_kind, len(found),
                         f"ตรวจพบรายงาน{THAI_LABEL[prefix_kind]} (จากเลขที่เอกสาร)",
                         f"detected {prefix_kind} from document numbers only; title not found")

    return Detection(None, "none", None, None, 0,
                     "ไม่สามารถระบุชนิดของไฟล์ได้ "
                     "กรุณาอัปโหลดไฟล์ CSV ที่ส่งออกจากโปรแกรม Express "
                     "(รายงานขายเงินสด / ขายเงินเชื่อ / รับมัดจำ)",
                     "could not identify this file as an Express report")


def detect_set(files: list[tuple[str, bytes]]) -> tuple[dict[str, tuple[str, bytes]], list[str]]:
    """Route a batch of uploads to their report kinds.

    Returns (mapping kind -> (filename, bytes), list of Thai error messages).
    Duplicates and missing reports are errors: the pipeline needs exactly one
    of each, and silently accepting two cash files would double revenue.
    """
    routed: dict[str, tuple[str, bytes]] = {}
    errors: list[str] = []

    for name, raw in files:
        d = detect(raw)
        if d.kind is None:
            errors.append(f"«{name}» {d.reason_th}")
            continue
        if d.kind in routed:
            prev = routed[d.kind][0]
            errors.append(
                f"«{name}» เป็นรายงาน{THAI_LABEL[d.kind]}ซ้ำกับไฟล์ «{prev}» "
                f"กรุณาอัปโหลดแต่ละรายงานเพียงไฟล์เดียว"
            )
            continue
        routed[d.kind] = (name, raw)

    for kind in ("cash", "credit", "deposit"):
        if kind not in routed:
            errors.append(f"ไม่พบไฟล์รายงาน{THAI_LABEL[kind]}")

    return routed, errors
