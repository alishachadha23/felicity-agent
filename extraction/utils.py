"""
utils.py

Shared helper functions used across the extraction pipeline.
"""

import re
import pandas as pd
import pdfplumber


# ----------------------------------------------------
# Constants
# ----------------------------------------------------

SKIP_NARRATION = (
    "opening balance",
    "closing balance",
    "total ",
    "totals",
    "turnover",
    "statement summary",
    "pending charges",
    "charge date",
    "brought forward",
    "carried forward",
    "b/f",
    "c/f",
    "grand total",
    "total number of transactions",
)


# ----------------------------------------------------
# Value Cleaning
# ----------------------------------------------------

def to_amount(value):
    """
    Converts string amounts into float.

    Handles:
    ₹12,345.00
    CR / DR
    (CR)
    (DR)
    """

    if value is None:
        return None

    value = str(value).replace("\n", " ").strip()

    if value in ("", "-", "–", "—", "NA", "N/A", "."):
        return None

    lower = value.lower()

    cleaned = re.sub(
        r"\(cr\)|\(dr\)|\bcr\b|\bdr\b|inr|rs\.?|₹|\$|,|\s",
        "",
        lower,
    )

    if cleaned in ("", "-"):
        return None

    try:
        number = float(cleaned)
    except ValueError:
        return None

    if "(dr)" in lower:
        number = -abs(number)

    return number


# ----------------------------------------------------
# Date Cleaning
# ----------------------------------------------------

def to_iso(value):
    """
    Converts any supported date
    into YYYY-MM-DD.
    """

    if value is None:
        return None

    value = str(value).replace("\n", " ").strip()

    if not value:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value

    parsed = pd.to_datetime(
        value,
        dayfirst=True,
        errors="coerce",
    )

    if pd.isna(parsed):
        return None

    return parsed.strftime("%Y-%m-%d")


# ----------------------------------------------------
# Account Header Extraction
# ----------------------------------------------------

def _clean_holder(name):

    name = re.split(
        r"\b(account|customer|a/c|cust|number|no\.?|ifsc|branch|"
        r"nominee|address|statement|crn|pan|period)\b",
        name,
        flags=re.I,
    )[0]

    return name.strip(" :-–").strip() or None


def account_info(text):
    """
    Extract account holder
    and account number.
    """

    holder = None
    account = None

    holder_patterns = [

        r"primary holder[:\s]+([A-Za-z][A-Za-z .&/]{2,50})",

        r"account holder[:\s]+([A-Za-z][A-Za-z .&/]{2,50})",

        r"\bname\b\s*[:]\s*([A-Za-z][A-Za-z .&/]{2,50})",

    ]

    for pattern in holder_patterns:

        match = re.search(pattern, text, re.I)

        if match:
            holder = _clean_holder(match.group(1))
            break

    account_patterns = [

        r"account (?:no|number)\.?\s*[:]?\s*([0-9Xx]{6,})",

        r"a/c (?:no|number)\.?\s*[:]?\s*([0-9Xx]{6,})",

    ]

    for pattern in account_patterns:

        match = re.search(pattern, text, re.I)

        if match:
            account = match.group(1).strip()
            break

    return holder, account


# ----------------------------------------------------
# Table Helpers
# ----------------------------------------------------

def tables_from_page(page):
    """
    Extracts all possible tables
    from a PDF page.
    """

    tables = page.extract_tables() or []

    if not tables:

        tables = page.extract_tables({

            "vertical_strategy": "text",

            "horizontal_strategy": "text",

        }) or []

    return tables


def map_columns(header):
    """
    Maps column names into
    standardized schema.
    """

    mapping = {}

    for index, column in enumerate(header):

        column = (column or "").lower()

        column = column.replace("\n", " ").strip()

        if not column:
            continue

        if "value date" in column:
            mapping["value_date"] = index

        elif (
            "txn date" in column
            or "transaction date" in column
            or "tran date" in column
            or column == "date"
            or column.endswith(" date")
            or column.startswith("date")
        ):

            mapping["date"] = index

        elif (
            "narration" in column
            or "description" in column
            or "remarks" in column
            or "particular" in column
        ):

            mapping["narration"] = index

        elif (
            "reference" in column
            or "ref no" in column
            or "ref." in column
            or "cheque" in column
            or "chq" in column
        ):

            mapping["ref"] = index

        elif "withdrawal" in column or "debit" in column:

            mapping["debit"] = index

        elif "deposit" in column or "credit" in column:

            mapping["credit"] = index

        elif "balance" in column:

            mapping["balance"] = index

    return mapping


def score_mapping(mapping):

    required = [

        "date",

        "debit",

        "credit",

        "balance",

        "narration",

    ]

    return sum(key in mapping for key in required)


def find_header(table):
    """
    Finds the most likely header row.
    Supports multi-line headers.
    """

    best = (None, {}, 0)

    for i in range(min(4, len(table))):

        mapping = map_columns(table[i])

        score = score_mapping(mapping)

        if score > best[2]:
            best = (i, mapping, score)

        if i + 1 < len(table):

            merged = [

                ((a or "") + " " + (b or ""))

                for a, b in zip(table[i], table[i + 1])

            ]

            mapping = map_columns(merged)

            score = score_mapping(mapping)

            if score > best[2]:
                best = (i, mapping, score)

    return best


def get_cell(row, mapping, key):
    """
    Safely returns a cell value.
    """

    idx = mapping.get(key)

    if idx is None or idx >= len(row):
        return None

    value = row[idx]

    if value is None:
        return None

    return str(value).replace("\n", " ").strip()
