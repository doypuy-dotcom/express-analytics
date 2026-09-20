"""
Parser for printed-report CSV exports from the Thai accounting package "Express".

The three files in data/raw/ are *report layouts* dumped to CSV, not tables:
page headers repeat every page, documents are made of a header line followed by
item lines, free-text remark lines float inside documents, and the whole thing is
cp874 (TIS-620) with CRLF endings and non-breaking spaces as the word separator.

This module turns them into flat, normalised UTF-8 tables in data/clean/.

Parsing strategy
----------------
Every logical record occupies exactly one physical line, so we split the file on
CRLF *first* and only then split each line into fields. We deliberately do NOT
feed the whole file to csv.reader: several column-header lines end with an
unterminated quote, which would make csv.reader swallow the rest of the report
into a single field. Line-at-a-time parsing contains that damage to the one bad
line (which is a header we skip anyway).

Records are then classified by shape (see classify_* functions) rather than by
position, because page headers can interrupt a document between its header line
and its item lines.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
CLEAN = ROOT / "data" / "clean"
SAMPLE = ROOT / "data" / "sample"
# Hand-maintained lookups that are an INPUT to the parse, not an output of it.
REFERENCE = ROOT / "data" / "reference"
FILE_PRODUCT_GROUPS = REFERENCE / "product_groups.csv"
# Files a human has to eyeball and sign off on -- deliberately NOT in clean/,
# because nothing in here should be joined into analysis until confirmed.
REVIEW = ROOT / "data" / "review"

# Thai VAT. A handful of documents are priced VAT-inclusive: their printed line
# amounts add up to the gross total instead of the ex-VAT goods value.
VAT_RATE = 0.07

# Rows of data/sample/*, for design mockups.
SAMPLE_ROWS = 200

SOURCE_ENCODING = "cp874"  # TIS-620 as written by Express

# utf-8-sig (UTF-8 + BOM) so Excel on a Thai Windows box renders the Thai text
# correctly on double-click. Change to "utf-8" if you only consume these in code.
OUTPUT_ENCODING = "utf-8-sig"

FILE_CASH = RAW / "ขายเงินสด.csv"  # cash sales      (HS...)
FILE_CREDIT = RAW / "ขายเงินเชื่อ.csv"  # credit sales    (IV...)
FILE_DEPOSIT = RAW / "รับมัดจำ.csv"  # deposits        (AI...)

# Lines that are page furniture rather than data. Matched as substrings against
# the whitespace-normalised line, so the tokens are normalised the same way
# (Express pads "หน้า   :" with spaces that norm() collapses to one).
PAGE_NOISE = tuple(
    re.sub(r"\s+", " ", t)
    for t in (
        "หน้า   :",  # "page : n"
        "รายงานใบกำกับสินค้า",  # report title, credit sales
        "รายงานขายเงินสด",  # report title, cash sales
        "รายงานใบรับมัดจำ",  # report title, deposits
        "วันที่จาก",  # "date from ... to ..."
        "รหัสลูกค้า",  # filter echo: customer code range
        "พนักงานขาย",  # filter echo: salesperson range
        "หมายเหตุ",  # footnote block
        "เกินกำหนด จะมีเครื่องหมาย",  # footnote continuation
        "รออนุมัติให้เกินวงเงิน",  # footnote: over-credit-limit marker
        "ยอดที่ตัดใบรับมัดจำ",  # report grand total of applied deposits
        "จบรายงาน",  # ">>>> end of report <<<<"
    )
)

# Report grand-total line, e.g. "รวม 168 ใบ",,,,,123.45,...  ("total N documents")
GRAND_TOTAL_RE = re.compile(r"^รวม\s")

# Deposit/receipt voucher applied against a sales document. Printed as a
# free-standing line inside the document block:
#   "        ตัดใบรับมัดจำ#  AI6811/2701      22493.00"
# AI = deposit receipt, SR = advance/receipt voucher. The amount is deducted
# from the document, which is why such documents' line totals exceed
# goods_value by exactly this amount.
VOUCHER_APPLIED_RE = re.compile(r"ตัดใบรับมัดจำ#\s*([A-Z]{2}[\w/]+)\s+([\d,]+\.\d{2})")
CANONICAL_VOUCHER_RE = re.compile(r"^(AI|SR)\d{4}/\d{4}$")

# The discount cell is usually an amount but is occasionally a rate, "5%".
PERCENT_RE = re.compile(r"^([\d.,]+)\s*%$")

# Per-customer running summary in the deposit report, printed as a free-standing
# single cell ending in "บาท" (baht): total / deposit taken / balance. The wording
# of the third line is not fixed -- clerks write "ค้างชำระ" (outstanding) or
# "ชำระวันจัดส่ง" (pay on delivery day) -- so match the shape, not the words.
BAHT_SUMMARY_RE = re.compile(r"บาท$")

# --- product classification ------------------------------------------------
# "ค่า..." is Thai for "charge for ...", so a product name starting with it is a
# service line, not a stocked good (ค่ารีดลอน = roll-forming charge,
# ค่าขนส่ง = freight, ค่าพับ = folding charge, ค่าตัดเหล็ก = steel cutting).
SERVICE_NAME_RE = re.compile(r"^ค่า")
# Units that count occurrences rather than goods: "times" and "trips".
SERVICE_UNITS = {"ครั้ง", "เที่ยว"}
# A SKU sold this often with no price at all is not stock being sold: it is a
# promotional giveaway or a bundle marker.
NON_STOCK_UNPRICED_RATIO = 0.95

# Column-header lines are identified by their label cells, not by position.
COLUMN_LABELS = {"รายละเอียด", "จำนวน", "ราคาต่อหน่วย", "จำนวนเงิน", "เลขที่", "วันที่"}

DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# "01-208 -  ลอน760/..."  ->  sku "01-208", name "ลอน760/..."
# Non-greedy head so the hyphen *inside* the sku (01-208) is not the split point;
# the separator is always " - " with surrounding whitespace.
SKU_SPLIT_RE = re.compile(r"^(.*?)\s+-\s*(.*)$")
# "  กนกพร ลอดจำปา /ก-217"  ->  name, customer code
CUSTOMER_GROUP_RE = re.compile(r"^(.*?)\s*/\s*([^/]+)$")

# Narrowest customer-name column across the three reports (cash = 29 chars;
# credit = 36; deposits = 52 -- all measured from the data, not guessed). A name
# at or beyond this length may have been clipped by the printer, so a prefix
# match against it is evidence of truncation rather than of a shorter name.
NAME_CLIP_MIN = 29
DIGITS_RE = re.compile(r"\d+")

# Codes confirmed by the business owner as the same customer under a clipped
# name (see data/review/customer_matches.csv). Applied at parse time so every
# downstream table agrees; anything not listed here gets a generated code.
CONFIRMED_CUSTOMER_MERGES = {
    "พงษ์เพ็ชรเมทัลชีท 4 สาขาทรัพย์ไพรวัล": ("พ-073", "truncated"),
    "เดอะแกรนด์ไลฟ์ พร็อพเพอร์ตี้ จำกัด": ("ด-057", "prefix"),
}
# Credit-only customers that exist in no coded report get a synthetic code.
# The CR- prefix keeps them visibly distinct from Express's own Thai-letter
# codes, so nobody mistakes a generated key for an accounting one.
GENERATED_CODE_FMT = "CR-{:03d}"

# Voucher families applied against a sale. AI is a genuine customer deposit;
# SR behaves like a same-day returns credit note (odd amounts, never present in
# the deposit report). Kept as a column so SR can be netted off later without
# re-parsing, per the decision to leave revenue unchanged for now.
VOUCHER_DOC_TYPE_RE = re.compile(r"^([A-Z]{2})")


# --------------------------------------------------------------------------
# Low-level text helpers
# --------------------------------------------------------------------------


def split_fields(line: str) -> list[str]:
    """Split one Express report line into raw fields.

    Express quotes text fields and leaves numbers/dates bare. It does *not*
    escape embedded double quotes, which appear in product names as the inch
    mark, e.g.

        "",9,"12-402 -  กรรไกรตัดสังกะสี 10"",1.00,"อัน",130.00,"",130.00

    Here `10""` is the text `10"` followed by the field's closing quote. The
    disambiguating rule, applied below: inside a quoted field a double quote
    only *closes* the field when the next character is a comma or end-of-line.
    Any other quote is a literal inch mark. Python's csv module has no such
    rule and mangles these 34 rows, which is why we split by hand.
    """
    fields: list[str] = []
    i, n = 0, len(line)
    while i <= n:
        if i == n:  # trailing empty field after a final comma
            fields.append("")
            break
        if line[i] == '"':
            i += 1
            buf = []
            while i < n:
                if line[i] == '"' and (i + 1 >= n or line[i + 1] == ","):
                    i += 1  # consume closing quote
                    break
                buf.append(line[i])
                i += 1
            fields.append("".join(buf))
        else:
            j = line.find(",", i)
            if j == -1:
                fields.append(line[i:])
                i = n
                break
            fields.append(line[i:j])
            i = j
        if i < n and line[i] == ",":
            i += 1
            if i == n:  # line ends with a comma -> one more empty field
                fields.append("")
                break
        else:
            break
    return fields


def norm(text: str) -> str:
    """Normalise a text cell: NBSP -> space, collapse runs of spaces, strip.

    Express pads report columns with U+00A0 and uses it as an intra-word
    separator, so a plain strip() is not enough.
    """
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def num(text: str):
    """Parse a numeric cell. Blank/'-' become None so they stay NULL in output."""
    t = norm(text).replace(",", "")
    if t in ("", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_discount(text: str) -> tuple[float | None, float | None]:
    """Return (discount_amount, discount_pct).

    Express prints this cell either as a baht amount or as a rate ("5%"). A rate
    is not an amount, so it gets its own column instead of being coerced --
    treating "5%" as 5 baht would silently corrupt the figures.
    """
    t = norm(text)
    m = PERCENT_RE.match(t)
    if m:
        return (None, num(m.group(1)))
    return (num(t), None)


def parse_date(text: str):
    """Return (original dd/mm/yyyy BE string, ISO Gregorian string).

    Express dates are Buddhist Era; Gregorian year = BE year - 543. A trailing
    '*' on a due date means "overdue" per the report footnote, so strip it.
    """
    t = norm(text).rstrip("*").strip()
    m = DATE_RE.match(t)
    if not m:
        return (t or None, None)
    dd, mm, yyyy = m.groups()
    return (t, f"{int(yyyy) - 543:04d}-{mm}-{dd}")


def is_noise(line_norm: str, fields: list[str]) -> bool:
    """True for page headers, rules, column headers and report footers."""
    if not line_norm:
        return True
    stripped = line_norm.strip('" ')
    if stripped and set(stripped) <= set("-= "):  # ---- and ==== rules
        return True
    if any(token in line_norm for token in PAGE_NOISE):
        return True
    # Column-header rows: several cells are literal column labels.
    if sum(1 for f in fields if norm(f) in COLUMN_LABELS) >= 2:
        return True
    return False


def fix_column_bleed(name: str, salesperson: str) -> tuple[str, str, bool]:
    """Repair a fixed-width overflow between customer name and salesperson code.

    The credit report prints the customer name in a fixed-width column. When a
    name is too long it fills the column completely and the *first digit* of the
    adjacent salesperson code is emitted inside the name cell instead:

        "เค.ซี.เมททอลชีท จำกัด(มหาชน) สาขาพิษ0"," 3",...

    The real values are a truncated name and salesperson "03". Detect this by
    the tell-tale pair (name ends in a digit, salesperson is a single digit),
    move the digit back, and flag the name as truncated so it is not silently
    trusted as a clean customer identity.
    """
    if len(salesperson) == 1 and salesperson.isdigit() and name and name[-1].isdigit():
        return name[:-1].strip(), name[-1] + salesperson, True
    return name, salesperson, False


def pad_code(code: str | None) -> str | None:
    """Zero-pad a numeric 2-digit code so the three reports agree on one form.

    Salesperson codes are identifiers, not numbers. The deposit report happens
    to print them already padded, but relying on that is luck -- a single-digit
    "3" from any report would join as a different salesperson from "03" and
    quietly split one person's revenue in two. Non-numeric codes pass through
    untouched.
    """
    if code and code.isdigit() and len(code) < 2:
        return code.zfill(2)
    return code


def split_sku(text: str) -> tuple[str | None, str]:
    """Split 'SKU -  product name' into (sku, name). Name may be empty."""
    t = norm(text)
    m = SKU_SPLIT_RE.match(t)
    if not m:
        return (None, t)
    sku, name = m.group(1).strip(), m.group(2).strip()
    return (sku or None, name)


def read_lines(path: Path) -> list[str]:
    """Decode the report and return physical lines, blanks removed."""
    raw = path.read_bytes().decode(SOURCE_ENCODING)
    return [ln for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]


# --------------------------------------------------------------------------
# File 1 + 2: sales reports
# --------------------------------------------------------------------------


def parse_sales(path: Path, sale_type: str, headers, lines_out, notes, applications,
                unparsed):
    """Parse a cash (HS) or credit (IV) sales report.

    Both files share the same document-block shape -- one header line followed
    by item lines and optional free-text remark lines -- but differ in column
    layout, so the two are handled by one walker with per-type field mapping.

    Row rules
      header : field[0] is a dd/mm/25xx date        (14 cols cash, 12 cols credit)
      item   : field[0] empty, field[1] is a number (line number)
      remark : field[0] and field[1] empty, field[2] free text
               -- these are the operator's delivery/contact notes typed into the
               document; they are real data, so they are collected rather than
               dropped.

    Item rows attach to the most recent header, which survives page breaks.
    """
    current_doc = None

    for lineno, line in enumerate(read_lines(path), start=1):
        fields = split_fields(line)
        line_norm = norm(line)

        if is_noise(line_norm, fields):
            continue

        f0 = norm(fields[0]) if fields else ""

        # ---- deposit/receipt voucher applied to the current document -------
        m = VOUCHER_APPLIED_RE.search(line_norm)
        if m:
            applications.append(
                {
                    "deposit_doc_no": m.group(1),
                    "applied_doc_no": current_doc,
                    "applied_date": None,
                    "applied_date_iso": None,
                    "cleared_date": None,
                    "cleared_date_iso": None,
                    "amount": num(m.group(2)),
                    "customer_code": None,
                    "source": f"{sale_type}_sales",
                }
            )
            continue

        # Report grand-total footer ("รวม 168 ใบ", "รวม *** ใบ").
        if GRAND_TOTAL_RE.match(f0):
            continue

        # A leading '*' marks a cancelled document (see the report footnote).
        cancelled = f0.startswith("*")
        f0_clean = f0.lstrip("*").strip()

        # ---- header row -------------------------------------------------
        if DATE_RE.match(f0_clean):
            date_raw, date_iso = parse_date(f0_clean)
            g = lambda i: norm(fields[i]) if i < len(fields) else ""  # noqa: E731

            if sale_type == "cash":
                # date, doc_no, cust_code, cust_name, salesperson, vat_flag,
                # discount, goods_value, vat, total, overpaid, cash, cheque, wht
                disc_amt, disc_pct = parse_discount(fields[6]) if len(fields) > 6 else (None, None)
                rec = {
                    "doc_no": g(1),
                    "sale_type": "cash",
                    "doc_date": date_raw,
                    "doc_date_iso": date_iso,
                    "customer_code": g(2) or None,
                    "customer_name": g(3) or None,
                    "salesperson_code": pad_code(g(4)) or None,
                    "name_truncated": False,
                    "vat_flag": g(5) or None,
                    "discount": disc_amt,
                    "discount_pct": disc_pct,
                    "goods_value": num(fields[7]) if len(fields) > 7 else None,
                    "vat": num(fields[8]) if len(fields) > 8 else None,
                    "total": num(fields[9]) if len(fields) > 9 else None,
                    "overpaid": num(fields[10]) if len(fields) > 10 else None,
                    "received_cash": num(fields[11]) if len(fields) > 11 else None,
                    "received_cheque": num(fields[12]) if len(fields) > 12 else None,
                    "withholding_tax": num(fields[13]) if len(fields) > 13 else None,
                    "due_date": None,
                    "due_date_iso": None,
                    "sales_order": None,
                    "collected_flag": None,
                    "is_cancelled": cancelled,
                }
            else:
                # date, doc_no, cust_name, salesperson, vat_flag, discount,
                # goods_value, vat, total, due_date, sales_order, collected
                due_raw, due_iso = parse_date(fields[9]) if len(fields) > 9 else (None, None)
                cname, scode, truncated = fix_column_bleed(g(2), g(3))
                disc_amt, disc_pct = parse_discount(fields[5]) if len(fields) > 5 else (None, None)
                rec = {
                    "doc_no": g(1),
                    "sale_type": "credit",
                    "doc_date": date_raw,
                    "doc_date_iso": date_iso,
                    "customer_code": None,  # not present in this report
                    "customer_name": cname or None,
                    "salesperson_code": pad_code(scode) or None,
                    "name_truncated": truncated,
                    "vat_flag": g(4) or None,
                    "discount": disc_amt,
                    "discount_pct": disc_pct,
                    "goods_value": num(fields[6]) if len(fields) > 6 else None,
                    "vat": num(fields[7]) if len(fields) > 7 else None,
                    "total": num(fields[8]) if len(fields) > 8 else None,
                    "overpaid": None,
                    "received_cash": None,
                    "received_cheque": None,
                    "withholding_tax": None,
                    "due_date": due_raw,
                    "due_date_iso": due_iso,
                    "sales_order": g(10) or None,
                    "collected_flag": g(11) or None,
                    "is_cancelled": cancelled,
                }
            headers.append(rec)
            current_doc = rec["doc_no"]
            continue

        # ---- item row ---------------------------------------------------
        second = norm(fields[1]) if len(fields) > 1 else ""
        if f0 == "" and second.isdigit():
            if sale_type == "cash":
                # '', line_no, 'sku - name', qty, unit, price, disc, amount, [so_ref]
                sku, name = split_sku(fields[2]) if len(fields) > 2 else (None, "")
                rec = {
                    "doc_no": current_doc,
                    "line_no": int(second),
                    "sku": sku,
                    "product_name": name or None,
                    "qty": num(fields[3]) if len(fields) > 3 else None,
                    "unit": norm(fields[4]) or None if len(fields) > 4 else None,
                    "unit_price": num(fields[5]) if len(fields) > 5 else None,
                    "discount": num(fields[6]) if len(fields) > 6 else None,
                    "amount": num(fields[7]) if len(fields) > 7 else None,
                    "so_ref": norm(fields[8]) or None if len(fields) > 8 else None,
                    "sale_type": "cash",
                }
            else:
                # '', line_no, 'sku -', description, qty, unit, price, disc,
                # amount, '', [so_ref]   -- sku and name live in separate cells
                sku, _ = split_sku(fields[2]) if len(fields) > 2 else (None, "")
                if sku is None and len(fields) > 2:
                    sku = norm(fields[2]).rstrip("-").strip() or None
                rec = {
                    "doc_no": current_doc,
                    "line_no": int(second),
                    "sku": sku,
                    "product_name": (norm(fields[3]) or None) if len(fields) > 3 else None,
                    "qty": num(fields[4]) if len(fields) > 4 else None,
                    "unit": norm(fields[5]) or None if len(fields) > 5 else None,
                    "unit_price": num(fields[6]) if len(fields) > 6 else None,
                    "discount": num(fields[7]) if len(fields) > 7 else None,
                    "amount": num(fields[8]) if len(fields) > 8 else None,
                    "so_ref": norm(fields[10]) or None if len(fields) > 10 else None,
                    "sale_type": "credit",
                }
            if current_doc is None:
                unparsed.append((path.name, lineno, "item before any header", line_norm))
                continue
            lines_out.append(rec)
            continue

        # ---- free-text remark row ----------------------------------------
        third = norm(fields[2]) if len(fields) > 2 else ""
        if f0 == "" and second == "" and third:
            notes.append(
                {"doc_no": current_doc, "sale_type": sale_type, "remark": third}
            )
            continue

        unparsed.append((path.name, lineno, "no rule matched", line_norm))


# --------------------------------------------------------------------------
# File 3: deposits, grouped by customer
# --------------------------------------------------------------------------


def parse_deposits(path: Path, deposits, applications, unparsed):
    """Parse the deposit report (รับมัดจำ), which is grouped by customer.

    Layout per customer group:

        "  กนกพร ลอดจำปา /ก-217"                <- group header: name /code
        "","AI6903/1401",14/03/2569,...          <- deposit row (13 cols)
        "",1,"ลอน760/...","",5802.00             <- description line(s)
        " ...ยอดทั้งหมด 15,802 บาท"               <- free-text summary, skipped
        "เอกสารที่ตัด:"                           <- "documents applied" section
        "","HS6903/1631",16/03/2569,16/03/2569,5802.00
        "รวม กนกพร ... "                          <- group total, skipped

    Description lines and applied-document lines both have 5 fields, so they are
    told apart by field[1]: a digit means a description line number, anything
    else is a document number. The `in_applied` flag is a secondary guard.
    """
    current_customer_name = None
    current_customer_code = None
    current_deposit = None
    in_applied = False
    descriptions: dict[str, list[str]] = {}

    for lineno, line in enumerate(read_lines(path), start=1):
        fields = split_fields(line)
        line_norm = norm(line)

        if is_noise(line_norm, fields):
            continue

        bare = line_norm.strip('"').strip()

        # Section marker: the documents this deposit was applied to.
        if "เอกสารที่ตัด" in line_norm:
            in_applied = True
            continue

        # Group totals and the running summary block carry no per-document data
        # beyond what the deposit row already gives us.
        if (
            bare.startswith("รวม")
            or re.match(r"^(ยอด|มัดจำ|ค้างชำระ)", bare)
            or (len(fields) == 1 and BAHT_SUMMARY_RE.search(bare))
        ):
            in_applied = False
            continue

        f0 = norm(fields[0]) if fields else ""
        cancelled = f0.startswith("*")
        second = norm(fields[1]) if len(fields) > 1 else ""

        # ---- customer group header (single cell, "name /code") -------------
        if len(fields) == 1:
            m = CUSTOMER_GROUP_RE.match(bare)
            if m:
                current_customer_name = m.group(1).strip()
                current_customer_code = m.group(2).strip()
                current_deposit = None
                in_applied = False
                continue
            unparsed.append((path.name, lineno, "unrecognised single-cell line", line_norm))
            continue

        # ---- deposit row ---------------------------------------------------
        if second.startswith("AI"):
            d_raw, d_iso = parse_date(fields[2]) if len(fields) > 2 else (None, None)
            due_raw, due_iso = parse_date(fields[8]) if len(fields) > 8 else (None, None)
            g = lambda i: norm(fields[i]) if i < len(fields) else ""  # noqa: E731
            current_deposit = second
            in_applied = False
            deposits.append(
                {
                    "doc_no": second,
                    "customer_code": current_customer_code,
                    "customer_name": current_customer_name,
                    "doc_date": d_raw,
                    "doc_date_iso": d_iso,
                    "salesperson_code": pad_code(g(3)) or None,
                    "vat_flag": g(4) or None,
                    "value": num(fields[5]) if len(fields) > 5 else None,
                    "vat": num(fields[6]) if len(fields) > 6 else None,
                    "total": num(fields[7]) if len(fields) > 7 else None,
                    "due_date": due_raw,
                    "due_date_iso": due_iso,
                    "outstanding": num(fields[9]) if len(fields) > 9 else None,
                    "cleared_flag": g(10) or None,
                    "received_cash": num(fields[11]) if len(fields) > 11 else None,
                    "received_cheque": num(fields[12]) if len(fields) > 12 else None,
                    "is_cancelled": cancelled,
                    "description": None,  # filled in below
                }
            )
            continue

        # ---- applied-document row ('', doc_no, date, date, amount) ---------
        if f0 == "" and second and not second.isdigit():
            a_raw, a_iso = parse_date(fields[2]) if len(fields) > 2 else (None, None)
            c_raw, c_iso = parse_date(fields[3]) if len(fields) > 3 else (None, None)
            applications.append(
                {
                    "deposit_doc_no": current_deposit,
                    "applied_doc_no": second,
                    "applied_date": a_raw,
                    "applied_date_iso": a_iso,
                    "cleared_date": c_raw,
                    "cleared_date_iso": c_iso,
                    "amount": num(fields[4]) if len(fields) > 4 else None,
                    "customer_code": current_customer_code,
                    "source": "deposit_report",
                }
            )
            continue

        # ---- description line ('', line_no, text, '', amount) --------------
        if f0 == "" and second.isdigit() and not in_applied:
            text = norm(fields[2]) if len(fields) > 2 else ""
            if current_deposit and text:
                descriptions.setdefault(current_deposit, []).append(text)
            continue

        unparsed.append((path.name, lineno, "no rule matched", line_norm))

    # A deposit can carry several description lines; join them.
    for d in deposits:
        parts = descriptions.get(d["doc_no"])
        if parts:
            d["description"] = " | ".join(parts)


# --------------------------------------------------------------------------
# Derived dimension tables
# --------------------------------------------------------------------------


def build_products(lines_df: pd.DataFrame) -> pd.DataFrame:
    """One row per SKU.

    The printed product_name is the *description as sold*, so it embeds the
    made-to-order length of each roofing sheet ("ลอน760/ซิงค์/0.30/6.00ม"). Taking
    every distinct string would yield ~14k pseudo-products for ~630 real ones,
    so we collapse to one row per SKU using the most frequently printed name and
    unit, and keep n_name_variants so the spread stays visible.

    sku_prefix is the part before the first '-': the Express product group
    (01/02 sheet, 12 tools, 77 services, 88 foam, 99 misc).
    """
    # Cancelled documents are excluded from all revenue/quantity aggregates.
    df = lines_df[lines_df["sku"].notna() & ~lines_df["is_cancelled"]].copy()
    df["product_name"] = df["product_name"].fillna("")
    df["unit"] = df["unit"].fillna("")

    def modal(s: pd.Series):
        m = s[s != ""].mode()
        return m.iloc[0] if len(m) else None

    out = (
        df.groupby("sku")
        .agg(
            product_name=("product_name", modal),
            unit=("unit", modal),
            n_lines=("sku", "size"),
            n_name_variants=("product_name", "nunique"),
            total_qty=("qty", "sum"),
            total_revenue_ex_vat=("amount_ex_vat", "sum"),
            pct_unpriced=("unit_price", lambda s: round(s.isna().mean(), 4)),
        )
        .reset_index()
    )
    out["sku_prefix"] = out["sku"].str.split("-").str[0]

    def item_type(r):
        """Classify a SKU as inventory / service / non-stock.

        Deliberately per-SKU rather than per-prefix: prefixes 77 and 99 are
        *mixed*. 77-1xx are roll-forming charges but 77-2xx are off-grade sheets
        and gutter caps (real goods); 99-2xx are promotional giveaways but
        99-5xx/6xx are planters and PU drums that are genuinely sold.
        """
        name = r["product_name"] or ""
        if SERVICE_NAME_RE.match(name) or r["unit"] in SERVICE_UNITS:
            return "service"
        if r["pct_unpriced"] >= NON_STOCK_UNPRICED_RATIO:
            return "non_stock"
        return "inventory"

    out["item_type"] = out.apply(item_type, axis=1)
    out["is_inventory"] = out["item_type"] == "inventory"
    return out[
        ["sku", "sku_prefix", "product_name", "unit", "item_type", "is_inventory",
         "n_lines", "n_name_variants", "pct_unpriced", "total_qty",
         "total_revenue_ex_vat"]
    ].sort_values("sku")


def load_product_groups() -> pd.DataFrame:
    """Load the hand-maintained sku_prefix -> category/group_name lookup.

    sku_prefix is read as a string and zero-padded because it is an identifier,
    not a number: pandas would otherwise infer int64 and turn "01" into 1, which
    then fails to join against the "01" produced by splitting a SKU. Same trap
    applies to anyone reading the output CSVs -- see the note in main().
    """
    if not FILE_PRODUCT_GROUPS.exists():
        raise FileNotFoundError(
            f"Missing reference file {FILE_PRODUCT_GROUPS}. It maps each sku_prefix "
            "to a category/group_name and must be maintained by hand."
        )
    g = pd.read_csv(FILE_PRODUCT_GROUPS, dtype=str, encoding="utf-8-sig")
    g["sku_prefix"] = g["sku_prefix"].str.strip().str.zfill(2)
    for col in ("category", "group_name", "main_unit"):
        g[col] = g[col].str.strip()
    dupes = g["sku_prefix"][g["sku_prefix"].duplicated()].tolist()
    if dupes:
        raise ValueError(f"product_groups.csv has duplicate sku_prefix rows: {dupes}")
    return g


def attach_groups(df: pd.DataFrame, groups_df: pd.DataFrame) -> pd.DataFrame:
    """Add sku_prefix/category/group_name to any table that has a `sku` column.

    Left join, so a SKU whose prefix is missing from the reference file keeps its
    row and surfaces as a null category rather than vanishing from revenue.
    """
    # Idempotent: re-attaching would otherwise produce category_x/category_y.
    df = df.drop(columns=["sku_prefix", "category", "group_name"], errors="ignore").copy()
    df["sku_prefix"] = df["sku"].str.split("-").str[0]
    return df.merge(
        groups_df[["sku_prefix", "category", "group_name"]], on="sku_prefix", how="left"
    )


def denormalize_lines(lines_df: pd.DataFrame, headers_df: pd.DataFrame) -> pd.DataFrame:
    """Copy the header keys every line-level question needs onto the lines.

    sales_lines is the fact table, but the printed report puts the date,
    salesperson and customer only on the document header. Without them even
    "how much did we sell last week" needs a join, and every consumer
    re-implements it slightly differently. These four columns are a strict
    function of doc_no, so the copy cannot drift from the header table.
    """
    keys = ["doc_date", "doc_date_iso", "salesperson_code", "customer_code"]
    idx = headers_df.set_index("doc_no")
    df = lines_df.drop(columns=keys, errors="ignore").copy()
    for k in keys:
        df[k] = df["doc_no"].map(idx[k])
    return df


def build_weekly_demand(lines_df: pd.DataFrame, products_df: pd.DataFrame,
                        groups_df: pd.DataFrame) -> pd.DataFrame:
    """Quantity per ISO week per product group, for reorder/forecasting.

    Three filters, all necessary for the quantities to be addable:
      - inventory items only: service lines (ค่าขนส่ง billed per เที่ยว) and
        non-stock giveaways have quantities that mean nothing in a stock model.
      - not cancelled.
      - the group's MAIN unit only, taken from the reference file. A SKU can be
        sold by the metre as sheet and by the piece as a fabricated end-cap, so
        mixing units inside one group would add metres to pieces. Dropping the
        minor units loses <1% of revenue and keeps qty a real physical quantity.

    week_start is the Monday of the week, so partial weeks at either end of the
    data are visible as low bars rather than silently rolled into a neighbour.

    is_complete_week marks whether the whole Mon-Sun window falls inside the
    data's date range. The export currently ends on a Monday, so its last week
    holds ONE day and ~1/5 of normal volume. Left unflagged that week reads as a
    demand collapse and would wreck any model scored against it -- computed from
    the data, not hard-coded, so it stays right as the export window moves.

    forecast_scope is carried through from the reference file so consumers do
    not have to re-derive which groups the owner actually procures against.
    """
    inv = set(products_df.loc[products_df["is_inventory"], "sku"])
    df = lines_df[
        lines_df["sku"].isin(inv)
        & ~lines_df["is_cancelled"]
        & lines_df["doc_date_iso"].notna()
        & lines_df["qty"].notna()
    ].copy()

    df = attach_groups(df, groups_df)
    main_unit = dict(zip(groups_df["sku_prefix"], groups_df["main_unit"]))
    df = df[df["unit"] == df["sku_prefix"].map(main_unit)]

    d = pd.to_datetime(df["doc_date_iso"], errors="coerce")
    df["week_start"] = (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")

    out = (
        df.groupby(["week_start", "sku_prefix", "group_name", "unit"], dropna=False)
        .agg(qty=("qty", "sum"))
        .reset_index()
    )
    out["qty"] = out["qty"].round(2)

    # Completeness is judged against the span of the SALES data, not of this
    # filtered frame, so a quiet week in one group is not mistaken for a
    # truncated one.
    all_dates = pd.to_datetime(lines_df["doc_date_iso"], errors="coerce").dropna()
    first, last = all_dates.min(), all_dates.max()
    ws = pd.to_datetime(out["week_start"])
    out["is_complete_week"] = (ws >= first) & (ws + pd.Timedelta(days=6) <= last)

    scope = dict(
        zip(groups_df["sku_prefix"], groups_df["forecast_scope"].str.lower() == "true")
    )
    out["forecast_scope"] = out["sku_prefix"].map(scope).fillna(False)

    return out.sort_values(["week_start", "sku_prefix"]).reset_index(drop=True)


def build_monthly_sales(lines_df: pd.DataFrame, headers_df: pd.DataFrame,
                        groups_df: pd.DataFrame) -> pd.DataFrame:
    """Revenue per month x category x group x salesperson.

    Revenue is sum(amount_ex_vat) per the agreed metric, NOT header goods_value
    (which is net of applied deposits and understates by ~10%).

    n_invoices counts DISTINCT documents, not lines -- an invoice spanning three
    groups contributes 1 to each group's count, so the column is only additive
    within a group. Summing it across groups would overcount.
    """
    df = lines_df[~lines_df["is_cancelled"]].copy()
    df = attach_groups(df, groups_df)

    df["month"] = df["doc_date_iso"].str.slice(0, 7)

    out = (
        df.groupby(["month", "category", "group_name", "salesperson_code"], dropna=False)
        .agg(
            revenue_ex_vat=("amount_ex_vat", "sum"),
            n_invoices=("doc_no", "nunique"),
            n_lines=("doc_no", "size"),
        )
        .reset_index()
    )
    out["revenue_ex_vat"] = out["revenue_ex_vat"].round(2)
    return out.sort_values(
        ["month", "category", "group_name", "salesperson_code"]
    ).reset_index(drop=True)


def build_customer_matches(customers_df: pd.DataFrame,
                           headers_df: pd.DataFrame) -> pd.DataFrame:
    """Propose a customer_code for the credit-sale customers that have none.

    The credit report prints customer names without codes, so a handful of
    customers exist only as a name. This suggests a match against the coded
    customers from the cash/deposit reports and scores it. Nothing is merged --
    the output is a review file for a human to confirm, because a wrong merge
    silently reattributes revenue between customers.

    Each report clips the name column at its own fixed width -- measured from
    the data: cash 29 chars, credit 36, deposits 52. So the SAME customer can
    appear as two different strings, and neither is necessarily the full name.
    A prefix match therefore has to be tested in BOTH directions, and only
    counts as truncation evidence if the shorter string is long enough to have
    actually hit a clip point (>= NAME_CLIP_MIN).

    Signals, strongest first:
      exact     - names identical after normalisation.
      truncated - one name is a prefix of the other and the shorter one is at
                  or past the narrowest report width, i.e. it was clipped.
      prefix    - one name is a prefix of the other but is short enough to be a
                  genuinely different, shorter name. Scored as fuzzy, since
                  "X Co." being a prefix of "X Trading Co." proves nothing.
      fuzzy     - difflib similarity, for typos and spacing differences.

    A high fuzzy score is NOT the same as a correct match. Thai businesses very
    often run numbered sister companies, so names differing only by a digit
    ("... รูฟ 9 จำกัด" vs "... รูฟ จำกัด") score ~0.95 while being separate
    legal entities. `digits_differ` flags exactly that trap for the reviewer.
    """
    from difflib import SequenceMatcher

    coded = customers_df[customers_df["has_code"]][["customer_code", "customer_name"]]
    candidates = list(zip(coded["customer_name"], coded["customer_code"]))
    uncoded = customers_df[~customers_df["has_code"]]["customer_name"].tolist()

    # Which documents each uncoded name appears on, to help the reviewer judge.
    doc_counts = headers_df.groupby("customer_name")["doc_no"].agg(["size", "first"])

    rows = []
    for name in uncoded:
        best_score, best_code, best_name, best_type = 0.0, None, None, "none"
        for cand_name, cand_code in candidates:
            short, long_ = sorted((name, cand_name), key=len)
            if name == cand_name:
                score, mtype = 1.0, "exact"
            elif long_.startswith(short):
                ratio = len(short) / len(long_)
                if len(short) >= NAME_CLIP_MIN:
                    score, mtype = 0.90 + 0.09 * ratio, "truncated"
                else:
                    score, mtype = ratio, "prefix"
            else:
                score, mtype = SequenceMatcher(None, name, cand_name).ratio(), "fuzzy"
            if score > best_score:
                best_score, best_code, best_name, best_type = score, cand_code, cand_name, mtype

        n_docs = int(doc_counts.loc[name, "size"]) if name in doc_counts.index else 0
        example = doc_counts.loc[name, "first"] if name in doc_counts.index else None
        rows.append(
            {
                "customer_name": name,
                "n_documents": n_docs,
                "example_doc_no": example,
                "suggested_customer_code": best_code,
                "suggested_customer_name": best_name,
                "match_type": best_type,
                "match_score": round(best_score, 4),
                # True when the two names carry different digit groups -- the
                # numbered-sister-company trap. Treat a True here as "probably
                # a different customer" regardless of how high the score is.
                "digits_differ": DIGITS_RE.findall(name) != DIGITS_RE.findall(best_name or ""),
                "name_maybe_clipped": len(name) >= NAME_CLIP_MIN,
                "confirmed": "",  # reviewer writes Y/N here
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values(["digits_differ", "match_score"], ascending=[True, False])


def build_units_by_group(lines_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Unit usage per product group, plus the SKUs sold in more than one unit.

    Mixed units within a group make quantity totals meaningless to sum (metres
    of sheet + pieces of screw), so this shows how much of each group's quantity
    and revenue sits in each unit. A SKU appearing under two units is either a
    genuine repack or a data-entry slip; both need a human eye.
    """
    df = lines_df[lines_df["sku"].notna() & ~lines_df["is_cancelled"]].copy()
    df["sku_prefix"] = df["sku"].str.split("-").str[0]
    df["unit"] = df["unit"].fillna("(blank)")

    grp = (
        df.groupby(["sku_prefix", "unit"])
        .agg(
            n_skus=("sku", "nunique"),
            n_lines=("sku", "size"),
            total_qty=("qty", "sum"),
            total_revenue_ex_vat=("amount_ex_vat", "sum"),
        )
        .reset_index()
    )
    tot = grp.groupby("sku_prefix")[["total_qty", "total_revenue_ex_vat"]].transform("sum")
    grp["qty_share_pct"] = (100 * grp["total_qty"] / tot["total_qty"]).round(2)
    grp["revenue_share_pct"] = (
        100 * grp["total_revenue_ex_vat"] / tot["total_revenue_ex_vat"]
    ).round(2)
    grp["total_qty"] = grp["total_qty"].round(2)
    grp["total_revenue_ex_vat"] = grp["total_revenue_ex_vat"].round(2)

    # SKUs sold under more than one unit.
    per_sku = df.groupby("sku")["unit"].nunique()
    multi = sorted(per_sku[per_sku > 1].index)
    grp["group_has_multi_unit_sku"] = grp["sku_prefix"].isin(
        {s.split("-")[0] for s in multi}
    )

    detail = (
        df[df["sku"].isin(multi)]
        .groupby(["sku", "unit"])
        .agg(
            n_lines=("sku", "size"),
            total_qty=("qty", "sum"),
            total_revenue_ex_vat=("amount_ex_vat", "sum"),
            example_product_name=("product_name", lambda s: s.mode().iloc[0] if len(s.mode()) else None),
        )
        .reset_index()
        .sort_values(["sku", "n_lines"], ascending=[True, False])
    )
    detail["total_qty"] = detail["total_qty"].round(2)
    detail["total_revenue_ex_vat"] = detail["total_revenue_ex_vat"].round(2)

    return grp.sort_values(["sku_prefix", "revenue_share_pct"],
                           ascending=[True, False]), detail


def build_prefix_summary(lines_df: pd.DataFrame, products_df: pd.DataFrame) -> pd.DataFrame:
    """One row per sku_prefix (the Express product group) for naming the groups.

    top_5_product_names is ranked by how many line items carry the name, so it
    describes what the group actually sells rather than listing rare variants.
    Revenue is ex-VAT line revenue, per the agreed metric.
    """
    df = lines_df[lines_df["sku"].notna() & ~lines_df["is_cancelled"]].copy()
    df["sku_prefix"] = df["sku"].str.split("-").str[0]

    def top_names(s: pd.Series) -> str:
        return " | ".join(s.dropna().value_counts().head(5).index)

    summary = (
        df.groupby("sku_prefix")
        .agg(
            n_skus=("sku", "nunique"),
            n_lines=("sku", "size"),
            total_qty=("qty", "sum"),
            total_revenue_ex_vat=("amount_ex_vat", "sum"),
            main_unit=("unit", lambda s: s.mode().iloc[0] if len(s.mode()) else None),
            top_5_product_names=("product_name", top_names),
        )
        .reset_index()
    )

    # Carry the inventory/service/non-stock split so a group's nature is visible.
    types = (
        products_df.groupby(["sku_prefix", "item_type"]).size().unstack(fill_value=0)
    )
    for col in ("inventory", "service", "non_stock"):
        summary[f"n_{col}_skus"] = (
            summary["sku_prefix"].map(types[col]).fillna(0).astype(int)
            if col in types
            else 0
        )

    summary["total_qty"] = summary["total_qty"].round(2)
    summary["total_revenue_ex_vat"] = summary["total_revenue_ex_vat"].round(2)
    return summary[
        ["sku_prefix", "n_skus", "n_inventory_skus", "n_service_skus",
         "n_non_stock_skus", "n_lines", "total_qty", "total_revenue_ex_vat",
         "main_unit", "top_5_product_names"]
    ].sort_values("sku_prefix")


def flag_vat_inclusive(headers_df: pd.DataFrame, lines_df: pd.DataFrame) -> pd.DataFrame:
    """Mark documents whose printed line amounts already include VAT.

    Almost every document carries vat = 0.00 and line amounts that sum to
    goods_value. A few are priced the other way round: the operator typed
    VAT-inclusive unit prices, so the lines sum to `total` (gross) while
    goods_value holds the ex-VAT figure. Those are exactly the documents where

        vat > 0  and  sum(lines) == total  and  sum(lines) != goods_value

    Detecting them by this identity -- rather than by a flag in the file, which
    does not exist -- is what lets amount_ex_vat be computed correctly below.
    """
    sums = lines_df.groupby("doc_no", dropna=True)["amount"].sum(min_count=1)
    line_sum = headers_df["doc_no"].map(sums)
    headers_df["vat_inclusive"] = (
        (headers_df["vat"].fillna(0) > 0)
        & ((line_sum - headers_df["total"].fillna(0)).abs() <= 0.01)
        & ((line_sum - headers_df["goods_value"].fillna(0)).abs() > 0.01)
    ).fillna(False)
    return headers_df


def add_amount_ex_vat(lines_df: pd.DataFrame, headers_df: pd.DataFrame) -> pd.DataFrame:
    """Add the VAT-exclusive line amount -- the revenue metric for this dataset.

    Use this, not header goods_value, when summing revenue: goods_value is net
    of any deposit/receipt voucher applied to the document, so summing it
    understates sales by the value of every deposit redeemed.
    """
    inclusive = set(headers_df.loc[headers_df["vat_inclusive"], "doc_no"])
    lines_df["vat_inclusive"] = lines_df["doc_no"].isin(inclusive)
    lines_df["amount_ex_vat"] = lines_df["amount"].where(
        ~lines_df["vat_inclusive"],
        (lines_df["amount"] / (1 + VAT_RATE)).round(2),
    )

    # Propagate the document's cancellation flag down to its lines so any
    # line-level analysis can exclude voided documents without re-joining the
    # header. Cancelled documents must never contribute to revenue.
    cancelled = set(headers_df.loc[headers_df["is_cancelled"], "doc_no"])
    lines_df["is_cancelled"] = lines_df["doc_no"].isin(cancelled)
    return lines_df


def repair_voucher_numbers(df: pd.DataFrame) -> pd.DataFrame:
    """Repair voucher numbers mangled by the sales report's fixed-width cell.

    The "ตัดใบรับมัดจำ#" cell is too narrow for some numbers, so Express shifts a
    digit and truncates: AI6902/0401 is printed as "AI06902/040". Left alone the
    mangled number never matches its deposit, and the application gets counted
    twice (once per report).

    Rather than hard-coding the broken value we repair it from the data: for any
    voucher number that fails the canonical AA9999/9999 shape, look for a
    deposit-report row covering the same document for the same amount, and adopt
    that (authoritative) number.
    """
    bad_mask = ~df["deposit_doc_no"].fillna("").str.match(CANONICAL_VOUCHER_RE)
    if not bad_mask.any():
        return df

    authoritative = {
        (r.applied_doc_no, None if pd.isna(r.amount) else round(r.amount, 2)): r.deposit_doc_no
        for r in df[df["source"] == "deposit_report"].itertuples()
    }
    for idx in df.index[bad_mask]:
        key = (
            df.at[idx, "applied_doc_no"],
            None if pd.isna(df.at[idx, "amount"]) else round(df.at[idx, "amount"], 2),
        )
        if key in authoritative:
            df.at[idx, "deposit_doc_no"] = authoritative[key]
    return df


def dedupe_applications(apps_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the deposit->document link table to one row per pair.

    The same application is printed twice in the source data, once from each
    side: the deposit report lists it under "เอกสารที่ตัด:" (with dates), and the
    sales report prints a "ตัดใบรับมัดจำ#" line inside the document. Keeping both
    would double every applied amount.

    We union the two sources and keep one row per (deposit_doc_no,
    applied_doc_no), preferring the deposit-report row because it carries the
    dates. `sources` records which reports attested the link -- pairs seen in
    only one report are the interesting ones (SR vouchers, for instance, appear
    only on the sales side).
    """
    if apps_df.empty:
        return apps_df

    df = repair_voucher_numbers(apps_df.copy())
    # Sort so the dated deposit-report row wins the "first" aggregation.
    df["_pref"] = (df["source"] != "deposit_report").astype(int)
    df = df.sort_values("_pref")

    grouped = df.groupby(["deposit_doc_no", "applied_doc_no"], dropna=False)
    out = grouped.agg(
        applied_date=("applied_date", "first"),
        applied_date_iso=("applied_date_iso", "first"),
        cleared_date=("cleared_date", "first"),
        cleared_date_iso=("cleared_date_iso", "first"),
        amount=("amount", "first"),
        customer_code=("customer_code", "first"),
        sources=("source", lambda s: ",".join(sorted(set(s)))),
        amount_max_spread=("amount", lambda s: round(s.max() - s.min(), 2)),
    ).reset_index()

    # AI vs SR. Express prints both under the same "ตัดใบรับมัดจำ#" label, so the
    # only way to tell them apart is the voucher prefix. Revenue is deliberately
    # NOT adjusted for SR here -- this column just makes the split addressable.
    out["doc_type"] = (
        out["deposit_doc_no"].fillna("").str.extract(VOUCHER_DOC_TYPE_RE)[0].fillna("unknown")
    )
    return out


def build_customers(headers_df: pd.DataFrame, deposits_df: pd.DataFrame) -> pd.DataFrame:
    """Unique customers.

    Only the cash-sales and deposit reports carry a customer code; the credit
    report has names only. We therefore build a name -> code lookup from the two
    coded sources and back-fill credit-sale customers by exact name match.
    """
    coded = []
    for df, src in ((headers_df, "sales"), (deposits_df, "deposit")):
        if df.empty:
            continue
        sub = df[df["customer_code"].notna() & df["customer_name"].notna()]
        coded.append(sub[["customer_code", "customer_name"]].assign(source=src))
    lookup = {}
    if coded:
        allc = pd.concat(coded, ignore_index=True)
        for name, code in zip(allc["customer_name"], allc["customer_code"]):
            lookup.setdefault(name, code)

    names = pd.concat(
        [
            headers_df["customer_name"].dropna(),
            deposits_df["customer_name"].dropna() if not deposits_df.empty else pd.Series(dtype=str),
        ],
        ignore_index=True,
    ).drop_duplicates()

    rows = [{"customer_code": lookup.get(n), "customer_name": n} for n in sorted(names)]
    out = pd.DataFrame(rows)
    out["has_code"] = out["customer_code"].notna()
    return out


def build_customer_aliases(customers_df: pd.DataFrame) -> pd.DataFrame:
    """Resolve every uncoded customer name to a single customer_code.

    Two populations, handled differently on purpose:

      confirmed_merge - the owner confirmed this clipped name is an existing
                        customer, so it maps onto that customer's real code and
                        the alias row is the only record that the two strings
                        were ever different.
      generated       - no counterpart exists; the customer is real but was only
                        ever billed on credit. It gets a synthetic CR-nnn code.

    Generated codes are assigned in sorted-name order so the same input always
    produces the same codes. If they were assigned in encounter order, adding
    one December invoice would renumber every customer after it and silently
    invalidate any report that had quoted a CR- code.
    """
    uncoded = sorted(customers_df.loc[~customers_df["has_code"], "customer_name"])
    rows, n_generated = [], 0
    for name in uncoded:
        if name in CONFIRMED_CUSTOMER_MERGES:
            code, mtype = CONFIRMED_CUSTOMER_MERGES[name]
            rows.append({"raw_name": name, "customer_code": code, "match_type": mtype})
        else:
            n_generated += 1
            rows.append(
                {
                    "raw_name": name,
                    "customer_code": GENERATED_CODE_FMT.format(n_generated),
                    "match_type": "generated",
                }
            )
    out = pd.DataFrame(rows, columns=["raw_name", "customer_code", "match_type"])
    if not out.empty:
        # The canonical name for a merged alias is the one already on file under
        # that code; for a generated code the raw name IS canonical.
        canon = dict(zip(customers_df["customer_code"], customers_df["customer_name"]))
        out["canonical_name"] = [
            canon.get(c, n) for c, n in zip(out["customer_code"], out["raw_name"])
        ]
    return out


def apply_customer_aliases(headers_df: pd.DataFrame, customers_df: pd.DataFrame,
                           aliases_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stamp the resolved code onto sales headers and collapse the customer list.

    After this, customer_code is non-null everywhere, so downstream joins can
    use it as a key without silently dropping the credit-only customers.
    """
    if aliases_df.empty:
        return headers_df, customers_df

    amap = dict(zip(aliases_df["raw_name"], aliases_df["customer_code"]))
    # build_customers resolved most credit-sale names to a code by exact match,
    # but only inside the customer table -- the headers themselves were left
    # null. Back-fill from BOTH lookups or the 15 exact-matched credit customers
    # keep an empty code on 134 invoices and drop out of any keyed join.
    exact = dict(
        zip(
            customers_df.loc[customers_df["has_code"], "customer_name"],
            customers_df.loc[customers_df["has_code"], "customer_code"],
        )
    )
    filled = headers_df["customer_name"].map(lambda n: amap.get(n) or exact.get(n))
    headers_df["customer_code"] = headers_df["customer_code"].fillna(filled)

    # Rebuild the customer list on the resolved code. A confirmed merge folds two
    # name strings into one row; a generated code adds a row that now has a key.
    cmap = dict(zip(aliases_df["raw_name"], aliases_df["canonical_name"]))
    customers_df = customers_df.copy()
    customers_df["customer_code"] = customers_df["customer_code"].fillna(
        customers_df["customer_name"].map(amap)
    )
    customers_df["customer_name"] = [
        cmap.get(n, n) if not h else n
        for n, h in zip(customers_df["customer_name"], customers_df["has_code"])
    ]
    customers_df["code_source"] = customers_df["customer_code"].map(
        lambda c: "generated" if isinstance(c, str) and c.startswith("CR-") else "express"
    )
    customers_df = (
        customers_df.drop(columns=["has_code"])
        .drop_duplicates(subset=["customer_code"])
        .sort_values("customer_code")
        .reset_index(drop=True)
    )
    return headers_df, customers_df


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(headers_df, lines_df, deposits_df, apps_df, products_df, customers_df,
             notes_df, unparsed, counts, weekly_df=None, monthly_df=None,
             groups_df=None):
    """Print the QA report described in the task brief."""
    p = print
    p("=" * 78)
    p("EXPRESS PARSE - VALIDATION REPORT")
    p("=" * 78)

    p("\n1. DOCUMENTS PARSED")
    for label, n, expected in counts:
        flag = "" if expected is None else ("  OK" if n == expected else f"  !! expected ~{expected}")
        p(f"   {label:<34} {n:>7,}{flag}")
    p(f"   {'sales line items':<34} {len(lines_df):>7,}")
    p(f"   {'deposit applications':<34} {len(apps_df):>7,}")
    p(f"   {'free-text remark lines':<34} {len(notes_df):>7,}")

    cancelled = int(headers_df["is_cancelled"].sum()) + (
        int(deposits_df["is_cancelled"].sum()) if not deposits_df.empty else 0
    )
    p(f"   {'cancelled documents (* prefix)':<34} {cancelled:>7,}")

    # --- header vs lines reconciliation ------------------------------------
    # goods_value is NET of any deposit/receipt voucher applied to the document,
    # so the identity to check is:
    #     sum(line amounts) - applied vouchers - header discount == goods_value
    p("\n2. HEADER goods_value vs SUM(line amount_ex_vat)")
    sums = lines_df.groupby("doc_no", dropna=True)["amount_ex_vat"].sum(min_count=1)
    nulls = lines_df.assign(_n=lines_df["amount"].isna()).groupby("doc_no")["_n"].sum()
    vouchers = (
        apps_df[apps_df["applied_doc_no"].notna()].groupby("applied_doc_no")["amount"].sum()
        if not apps_df.empty
        else pd.Series(dtype=float)
    )

    chk = headers_df[
        ["doc_no", "sale_type", "goods_value", "discount", "discount_pct", "vat",
         "total", "vat_inclusive"]
    ].copy()
    chk["line_sum"] = chk["doc_no"].map(sums)
    chk["n_lines"] = chk["doc_no"].map(lines_df.groupby("doc_no").size()).fillna(0).astype(int)
    chk["voucher_applied"] = chk["doc_no"].map(vouchers).fillna(0.0)
    chk["null_amount_lines"] = chk["doc_no"].map(nulls).fillna(0).astype(int)

    # Expected goods_value = lines - vouchers applied - discount (amount or rate).
    gross = chk["line_sum"].fillna(0) - chk["voucher_applied"]
    expected = gross - chk["discount"].fillna(0)
    pct = chk["discount_pct"].fillna(0)
    expected = expected.where(pct == 0, gross * (1 - pct / 100))
    chk["diff"] = (expected - chk["goods_value"].fillna(0)).round(2)

    def classify(r):
        """Bucket a residual difference by its known business cause."""
        if abs(r["diff"]) <= 0.01:
            return "ok"
        # VAT-inclusive documents are converted line by line, so each line can
        # carry up to half a satang of rounding; allow one satang per line.
        if r["vat_inclusive"] and abs(r["diff"]) <= 0.01 * max(r["n_lines"], 1):
            return "ok_vat_rounding"
        if r["null_amount_lines"] > 0:
            return "blank_amount_lines"
        return "unexplained"

    chk["status"] = chk.apply(classify, axis=1)
    mism = chk[~chk["status"].isin(("ok", "ok_vat_rounding"))]
    ok = len(chk) - len(mism)
    p(f"   VAT-inclusive documents      : {int(chk['vat_inclusive'].sum()):,} "
      f"(line amounts converted at {VAT_RATE:.0%})")

    p(f"   documents checked            : {len(chk):,}")
    p(f"   reconciling exactly          : {ok:,}  ({100 * ok / max(len(chk), 1):.2f}%)")
    p(f"   mismatched                   : {len(mism):,}")
    if len(mism):
        for status, n in Counter(mism["status"]).most_common():
            p(f"     {status:<26} {n:,}")
        p("   first 20 mismatches:")
        for _, r in mism.head(20).iterrows():
            p(f"     {r['doc_no']:<16} {r['sale_type']:<7} "
              f"header={r['goods_value']!s:>11} lines={r['line_sum']!s:>11} "
              f"diff={r['diff']:>10,.2f} blank={r['null_amount_lines']:<3} {r['status']}")
        mism.to_csv(CLEAN / "_reconciliation_mismatches.csv", index=False,
                    encoding=OUTPUT_ENCODING)
        p("   -> full list written to data/clean/_reconciliation_mismatches.csv")

    # --- deposit reconciliation --------------------------------------------
    if not deposits_df.empty:
        p("\n3. DEPOSIT APPLICATIONS")
        applied = apps_df.groupby("deposit_doc_no")["amount"].sum(min_count=1)
        d = deposits_df[["doc_no", "total", "outstanding"]].copy()
        d["applied_sum"] = d["doc_no"].map(applied)
        no_app = d["applied_sum"].isna().sum()
        over = d[(d["applied_sum"].fillna(0) - d["total"].fillna(0)).round(2) > 0.01]
        p(f"   deposits (AI)                : {len(d):,}")
        p(f"   link rows (deduped)          : {len(apps_df):,}")
        by_src = Counter(apps_df["sources"])
        for src, n in by_src.most_common():
            p(f"     attested by {src:<28} {n:,}")
        # Where both reports describe the same link they must agree on amount.
        spread = apps_df[apps_df["amount_max_spread"].fillna(0) > 0.01]
        p(f"   cross-source amount conflicts: {len(spread):,}")
        p(f"   deposits w/ no applied doc   : {no_app:,}")
        p(f"   applied > deposit total      : {len(over):,}")
        orphan = apps_df["deposit_doc_no"].isna().sum()
        p(f"   applications w/o parent      : {orphan:,}")
        # Vouchers applied inside the window but issued before it are expected:
        # the deposit report only covers 01/12/2568 onward.
        known = set(deposits_df["doc_no"])
        unknown = apps_df[~apps_df["deposit_doc_no"].isin(known)]
        p(f"   vouchers not in deposit report: {len(unknown):,} "
          f"(issued before the report window, or SR-type)")
        p(f"     of which SR (non-deposit)   : "
          f"{unknown['deposit_doc_no'].fillna('').str.startswith('SR').sum():,}")

    # --- unparsed -----------------------------------------------------------
    p("\n4. UNPARSED ROWS")
    p(f"   total unparsed                : {len(unparsed):,}")
    if unparsed:
        by_reason = Counter(u[2] for u in unparsed)
        for reason, n in by_reason.most_common():
            p(f"     {reason:<30} {n:,}")
        p("   first 20 examples:")
        for src, ln, reason, text in unparsed[:20]:
            p(f"     {src}:{ln} [{reason}] {text[:110]}")

    # --- dimensions ---------------------------------------------------------
    p("\n5. DIMENSIONS")
    p(f"   unique SKUs                   : {products_df['sku'].nunique():,}")
    for t, n in products_df["item_type"].value_counts().items():
        rev = products_df.loc[products_df["item_type"] == t, "total_revenue_ex_vat"].sum()
        p(f"     {t:<26} {n:>4} SKUs   revenue {rev:>15,.2f}")
    # The agreed revenue metric: ex-VAT line revenue, NOT header goods_value
    # (which is net of applied deposits and so understates sales).
    rev_total = lines_df["amount_ex_vat"].sum()
    gv_total = headers_df["goods_value"].sum()
    p(f"   revenue (sum amount_ex_vat)   : {rev_total:>15,.2f}")
    p(f"   header goods_value (NOT used) : {gv_total:>15,.2f}  "
      f"understates by {rev_total - gv_total:,.2f}")
    p(f"   unique customers              : {len(customers_df):,}")
    src = customers_df["code_source"].value_counts()
    p(f"     Express code                : {int(src.get('express', 0)):,}")
    p(f"     generated CR- code          : {int(src.get('generated', 0)):,}")
    p(f"   headers still missing a code  : {int(headers_df['customer_code'].isna().sum()):,}")

    sp = pd.concat(
        [headers_df["salesperson_code"].dropna(),
         deposits_df["salesperson_code"].dropna() if not deposits_df.empty else pd.Series(dtype=str)]
    ).unique()
    sp = sorted(x for x in sp if x)
    p(f"   unique salesperson codes      : {len(sp)}  {sp}")

    units = sorted(u for u in lines_df["unit"].dropna().unique() if u)
    p(f"   unique units of measure       : {len(units)}  {units[:15]}")

    prefixes = sorted(products_df["sku_prefix"].dropna().unique())
    p(f"   sku prefixes (product groups) : {len(prefixes)}  {prefixes}")

    p("\n6. DATE RANGE")
    for label, df, col in (
        ("cash sales", headers_df[headers_df.sale_type == "cash"], "doc_date_iso"),
        ("credit sales", headers_df[headers_df.sale_type == "credit"], "doc_date_iso"),
        ("deposits", deposits_df, "doc_date_iso"),
    ):
        s = df[col].dropna() if not df.empty else pd.Series(dtype=str)
        if len(s):
            p(f"   {label:<14} {s.min()} .. {s.max()}   ({len(s):,} docs)")
        else:
            p(f"   {label:<14} (none)")

    bad = headers_df["doc_date_iso"].isna().sum()
    p(f"\n   headers with unparseable date : {bad:,}")

    if weekly_df is not None and groups_df is not None:
        p("\n7. ANALYSIS TABLE COVERAGE")
        p(f"   monthly_sales revenue         : {monthly_df['revenue_ex_vat'].sum():>15,.2f}"
          f"   ({'reconciles' if abs(monthly_df['revenue_ex_vat'].sum() - rev_total) < 0.01 else 'MISMATCH'})")

        # weekly_demand deliberately keeps only inventory SKUs sold in the
        # group's main unit. That is what makes qty addable, but it also means
        # some groups are only partly represented -- print how much of each
        # group's revenue the weekly quantities actually stand for, so nobody
        # forecasts group 11 off 23% of its sales without knowing.
        inv = set(products_df.loc[products_df["is_inventory"], "sku"])
        mu = dict(zip(groups_df["sku_prefix"], groups_df["main_unit"]))
        live = lines_df[~lines_df["is_cancelled"]]
        kept = live[live["sku"].isin(inv) & (live["unit"] == live["sku_prefix"].map(mu))]
        tot_by = live.groupby("sku_prefix")["amount_ex_vat"].sum()
        kept_by = kept.groupby("sku_prefix")["amount_ex_vat"].sum()
        pct = (kept_by / tot_by * 100).reindex(tot_by.index).fillna(0.0)

        p(f"   weekly_demand rows            : {len(weekly_df):,}  "
          f"({weekly_df['sku_prefix'].nunique()} of {len(groups_df)} groups, "
          f"{weekly_df['week_start'].nunique()} weeks)")
        inscope = weekly_df[weekly_df["forecast_scope"]]
        incomplete = sorted(weekly_df.loc[~weekly_df["is_complete_week"], "week_start"].unique())
        p(f"     in forecast scope           : {inscope['sku_prefix'].nunique()} groups, "
          f"{inscope['is_complete_week'].sum():,} complete-week rows")
        if incomplete:
            p(f"     INCOMPLETE weeks (excluded) : {incomplete}  "
              "(partial Mon-Sun window at the edge of the export)")
        p(f"   revenue represented           : {kept['amount_ex_vat'].sum():>15,.2f}"
          f"   ({kept['amount_ex_vat'].sum() / rev_total * 100:.1f}% of total)")
        thin = pct[pct < 90].sort_values()
        if len(thin):
            p(f"   groups under 90% covered      : {len(thin)}  "
              "(main unit misses most of the group -- forecast with care)")
            names = dict(zip(groups_df["sku_prefix"], groups_df["group_name"]))
            for pref, v in thin.items():
                p(f"     {pref}  {v:>5.1f}%  main_unit={mu.get(pref, '?'):<8} {names.get(pref, '')}")
        unconfirmed = groups_df[groups_df["needs_confirm"].str.lower() == "yes"]
        if len(unconfirmed):
            p(f"   group names awaiting confirm  : {len(unconfirmed)}  "
              f"{sorted(unconfirmed['sku_prefix'])}")
    p("=" * 78)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def run_pipeline(
    file_cash: Path = FILE_CASH,
    file_credit: Path = FILE_CREDIT,
    file_deposit: Path = FILE_DEPOSIT,
) -> dict:
    """Parse three Express exports into the full set of clean tables.

    The single source of truth for what the pipeline produces. The CLI (main)
    writes the result to data/clean/; the web upload endpoint writes the same
    result to Postgres. Neither re-implements a step, so the website and the
    local run cannot drift apart -- which is the point, because the reference
    figures (44,493,479.89 revenue; 6,088 cash; 168 credit) are asserted
    against both.

    Returns {tables, review, counts, notes_df, unparsed}.
    """
    headers: list[dict] = []
    lines_out: list[dict] = []
    notes: list[dict] = []
    deposits: list[dict] = []
    applications: list[dict] = []
    unparsed: list[tuple] = []

    n0 = len(headers)
    parse_sales(file_cash, "cash", headers, lines_out, notes, applications, unparsed)
    n_cash = len(headers) - n0

    n0 = len(headers)
    parse_sales(file_credit, "credit", headers, lines_out, notes, applications, unparsed)
    n_credit = len(headers) - n0

    parse_deposits(file_deposit, deposits, applications, unparsed)

    headers_df = pd.DataFrame(headers)
    lines_df = pd.DataFrame(lines_out)
    notes_df = pd.DataFrame(notes)
    deposits_df = pd.DataFrame(deposits)
    apps_df = dedupe_applications(pd.DataFrame(applications))

    # Attach remarks to their header so nothing typed by the operator is lost.
    if not notes_df.empty:
        agg = notes_df.groupby("doc_no")["remark"].apply(lambda s: " | ".join(s))
        headers_df["remark"] = headers_df["doc_no"].map(agg)
    else:
        headers_df["remark"] = None

    # VAT-inclusive documents must be identified before any revenue is summed.
    headers_df = flag_vat_inclusive(headers_df, lines_df)
    lines_df = add_amount_ex_vat(lines_df, headers_df)

    products_df = build_products(lines_df)
    customers_df = build_customers(headers_df, deposits_df)
    prefix_df = build_prefix_summary(lines_df, products_df)

    # Review artefacts: suggestions for a human, never applied automatically.
    # Built BEFORE the aliases are applied, so the file keeps showing the
    # unresolved names and stays a record of what was decided and why.
    matches_df = build_customer_matches(customers_df, headers_df)
    units_df, multi_unit_df = build_units_by_group(lines_df)

    # Confirmed merges + generated codes for the credit-only customers.
    aliases_df = build_customer_aliases(customers_df)
    headers_df, customers_df = apply_customer_aliases(headers_df, customers_df, aliases_df)

    # Product grouping comes from a hand-maintained reference file.
    groups_df = load_product_groups()
    lines_df = attach_groups(lines_df, groups_df)
    # Done after the aliases, so lines carry the RESOLVED customer_code.
    lines_df = denormalize_lines(lines_df, headers_df)
    products_df = products_df.merge(
        groups_df[["sku_prefix", "category", "group_name"]], on="sku_prefix", how="left"
    )

    weekly_df = build_weekly_demand(lines_df, products_df, groups_df)
    monthly_df = build_monthly_sales(lines_df, headers_df, groups_df)

    return {
        "tables": {
            "sales_header.csv": headers_df,
            "sales_lines.csv": lines_df,
            "deposits.csv": deposits_df,
            "deposit_applications.csv": apps_df,
            "products.csv": products_df,
            "customers.csv": customers_df,
            "customer_aliases.csv": aliases_df,
            "prefix_summary.csv": prefix_df,
            "weekly_demand.csv": weekly_df,
            "monthly_sales.csv": monthly_df,
        },
        "review": {
            "customer_matches.csv": matches_df,
            "units_by_group.csv": units_df,
            "multi_unit_skus.csv": multi_unit_df,
        },
        "counts": [
            ("cash sales documents (HS)", n_cash, 6088),
            ("credit sales documents (IV)", n_credit, 168),
            ("deposit documents (AI)", len(deposits_df), None),
        ],
        "notes_df": notes_df,
        "unparsed": unparsed,
    }


def main() -> int:
    CLEAN.mkdir(parents=True, exist_ok=True)

    result = run_pipeline(FILE_CASH, FILE_CREDIT, FILE_DEPOSIT)
    outputs = result["tables"]
    review_outputs = result["review"]
    notes_df = result["notes_df"]
    unparsed = result["unparsed"]

    headers_df = outputs["sales_header.csv"]
    lines_df = outputs["sales_lines.csv"]
    deposits_df = outputs["deposits.csv"]
    apps_df = outputs["deposit_applications.csv"]
    products_df = outputs["products.csv"]
    customers_df = outputs["customers.csv"]
    weekly_df = outputs["weekly_demand.csv"]
    monthly_df = outputs["monthly_sales.csv"]
    groups_df = load_product_groups()

    # Writing fails with PermissionError if a CSV is open in Excel, which is easy
    # to do by accident. Collect the locked files and report them all at once
    # rather than dying on the first with a bare traceback.
    REVIEW.mkdir(parents=True, exist_ok=True)

    locked = []
    for folder, group in ((CLEAN, outputs), (REVIEW, review_outputs)):
        for name, df in group.items():
            try:
                df.to_csv(folder / name, index=False, encoding=OUTPUT_ENCODING)
            except PermissionError:
                locked.append(f"{folder.name}/{name}")
    if locked:
        print(
            "ERROR: could not write these files because another program has them "
            "open (usually Excel):\n"
            + "\n".join(f"   data/{n}" for n in locked)
            + "\nClose them and re-run. No other output was affected.",
            file=sys.stderr,
        )

    # Small extracts for design mockups -- same columns, first N rows.
    # Regenerated from the final frames, so they carry category/group_name too.
    SAMPLE.mkdir(parents=True, exist_ok=True)
    for name, df in (
        ("sales_lines.csv", lines_df),
        ("sales_header.csv", headers_df),
        ("customers.csv", customers_df),
        ("products.csv", products_df),
    ):
        df.head(SAMPLE_ROWS).to_csv(SAMPLE / name, index=False, encoding=OUTPUT_ENCODING)

    if unparsed:
        pd.DataFrame(unparsed, columns=["source_file", "line_no", "reason", "text"]).to_csv(
            CLEAN / "_unparsed.csv", index=False, encoding=OUTPUT_ENCODING
        )

    validate(
        headers_df, lines_df, deposits_df, apps_df, products_df, customers_df, notes_df,
        unparsed,
        counts=result["counts"],
        weekly_df=weekly_df, monthly_df=monthly_df, groups_df=groups_df,
    )

    print("\nWrote to data/clean/:")
    for name, df in outputs.items():
        print(f"   {name:<28} {len(df):>7,} rows x {len(df.columns)} cols")
    if unparsed:
        print(f"   _unparsed.csv                {len(unparsed):>7,} rows")

    print("\nWrote to data/review/ (suggestions only -- nothing applied):")
    for name, df in review_outputs.items():
        print(f"   {name:<28} {len(df):>7,} rows x {len(df.columns)} cols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
