"""
ledger_pipeline.py  —  Bank / credit-card statement -> audited ledger

Design principle: DETERMINISTIC FIRST, MODEL AS FALLBACK.
Everything that is arithmetic (direction from balance delta, reconciliation,
burn, savings ratio, EMI load) is computed in code so it is exact, reproducible
and auditable. The language model is used only where judgement is genuinely
needed: (a) extracting rows from messy / scrambled layouts that the rule
parsers can't handle, and (b) classifying narrations the keyword rules miss.

Pipeline stages
  0  Config
  1  Value cleaners            to_amount, to_iso
  2  Header / doc type         account_info, is_card_text
  3  Extraction (rules first)  parse_tables -> parse_digital_lines -> parse_scanned -> llm_fallback
  4  Deterministic fixes       fix_debit_credit (balance delta), dedupe_exact
  5  Verification              validate (row), statement_audit (statement), verify_and_finalize
  6  Cross-file relations      flag_duplicates_and_contra
  7  Classification            apply_keyword_categories + llm_classify (fallback)
  8  Analytics                 analyze_ledger  (deterministic)
"""

import re, json, time, hashlib
from datetime import datetime
import pandas as pd
import requests

# ============================================================ 0. CONFIG
OLLAMA_URL = "http://localhost:11434/api/generate"

# Model is used ONLY for (a) extraction fallback on messy docs and (b) classifying
# narrations the keyword rules miss. Pick by the client's hardware:
#   qwen3:8b            - GPU 8GB+, best structured extraction (recommended)
#   gemma4:e4b          - GPU 8-10GB, multimodal (can also do card images), strong
#   qwen3:4b-instruct-2507 / gemma4:e2b - CPU-only i5 clients (fast, smaller)
MODEL      = "gemma4:e4b"
NUM_CTX    = 8192
PAGE_CHARS = 9000
OCR_DPI    = 300
CARD_OUTLIER_DAYS = 150
ACCEPT_BALANCE_PCT = 90.0      # accept a digital-table parse if it reconciles >= this
USE_LLM_CLASSIFY  = True       # set False on very slow CPU boxes to stay fully deterministic

# Ledger category taxonomy. Extend CATEGORY_RULES per client; keyword rules are
# deterministic and always win over the model.
CATEGORIES = [
    "self_transfer", "cash_withdrawal", "loan_emi", "staff_payment",
    "bills_utilities", "rent", "food_delivery", "shopping", "travel",
    "insurance", "medical", "investment", "interest_income",
    "income_active", "income_passive", "tax_payment", "tax_refund",
    "bank_charges", "miscellaneous",
]
# Which categories count as EXPENSE / INCOME / neutral, for the analytics.
INCOME_CATS   = {"income_active", "income_passive", "interest_income", "tax_refund"}
NEUTRAL_CATS  = {"self_transfer", "investment", "cash_withdrawal"}  # not "burn"
FIXED_CATS    = {"loan_emi", "rent", "bills_utilities", "staff_payment", "insurance"}

SKIP_NARRATION = ("opening balance", "closing balance", "total ", "totals",
                  "turnover", "statement summary", "pending charges",
                  "charge date", "brought forward", "carried forward",
                  "b/f", "c/f", "grand total", "total number of transactions")
CARD_SKIP = ("total amount due", "minimum amount due", "previous balance",
             "statement dated", "available credit", "credit limit")
CARD_KEYWORDS = ("total amount due", "minimum amount due", "credit limit",
                 "available credit", "reward points", "credit card statement",
                 "statement of credit card")


# ============================================================ 1. CLEANERS
def to_amount(s):
    if s is None:
        return None
    s = str(s).replace("\n", " ").strip()
    if s in ("", "-", "\u2013", "\u2014", "NA", "N/A", "."):
        return None
    low = s.lower()
    s2 = re.sub(r"\(cr\)|\(dr\)|\bcr\b|\bdr\b|inr|rs\.?|\u20b9|\$|,|\s", "", low)
    if s2 in ("", "-"):
        return None
    try:
        v = float(s2)
    except ValueError:
        return None
    if "(dr)" in low:
        v = -abs(v)
    return v


def to_iso(s):
    if s is None:
        return None
    s = str(s).replace("\n", " ").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    d = pd.to_datetime(s, dayfirst=True, errors="coerce")
    return None if pd.isna(d) else d.strftime("%Y-%m-%d")


# ============================================================ 2. HEADER / TYPE
def _clean_holder(h):
    h = re.split(r"\b(account|customer|a/c|cust|number|no\.?|ifsc|branch|"
                 r"nominee|address|statement|crn|pan|period)\b", h, flags=re.I)[0]
    return h.strip(" :-\u2013").strip() or None


def account_info(text):
    holder = number = None
    for pat in (r"primary holder[:\s]+([A-Za-z][A-Za-z .&/]{2,50})",
                r"account holder[:\s]+([A-Za-z][A-Za-z .&/]{2,50})",
                r"\bname\b\s*[:]\s*([A-Za-z][A-Za-z .&/]{2,50})"):
        mt = re.search(pat, text, re.I)
        if mt:
            holder = _clean_holder(mt.group(1)); break
    for pat in (r"account (?:no|number)\.?\s*[:]?\s*([0-9Xx]{6,})",
                r"a/c (?:no|number)\.?\s*[:]?\s*([0-9Xx]{6,})"):
        mt = re.search(pat, text, re.I)
        if mt:
            number = mt.group(1).strip(); break
    return holder, number


def is_card_text(text):
    t = (text or "").lower()
    return sum(k in t for k in CARD_KEYWORDS) >= 2


# ============================================================ 3. EXTRACTION
# ---- 3a. digital tables (best case) ----
def map_columns(header):
    m = {}
    for i, h in enumerate(header):
        h = (h or "").lower().replace("\n", " ").strip()
        if not h:
            continue
        if "value date" in h and "value_date" not in m:
            m["value_date"] = i
        elif (("txn date" in h or "transaction date" in h or "tran date" in h
               or h == "date" or h.endswith(" date") or h.startswith("date"))
              and "date" not in m):
            m["date"] = i
        elif (("narration" in h or "description" in h or "remarks" in h
               or "particular" in h) and "narration" not in m):
            m["narration"] = i
        elif (("cheque" in h or "reference" in h or "ref no" in h or "chq" in h
               or "ref." in h or "ref " in h) and "ref" not in m):
            m["ref"] = i
        elif ("withdrawal" in h or "debit" in h) and "debit" not in m:
            m["debit"] = i
        elif ("deposit" in h or "credit" in h) and "credit" not in m:
            m["credit"] = i
        elif "balance" in h and "balance" not in m:
            m["balance"] = i
    return m


def _score(m):
    return sum(1 for k in ("date", "debit", "credit", "balance", "narration") if k in m)


def find_header(table):
    best = (None, {}, 0)
    for i in range(min(4, len(table))):
        m = map_columns(table[i])
        if _score(m) > best[2]:
            best = (i, m, _score(m))
        if i + 1 < len(table):
            combo = [((a or "") + " " + (b or "")) for a, b in
                     zip(table[i], table[i + 1])]
            m2 = map_columns(combo)
            if _score(m2) > best[2]:
                best = (i, m2, _score(m2))
    return best


def _cell(row, m, key):
    i = m.get(key)
    if i is None or i >= len(row):
        return None
    v = row[i]
    return None if v is None else str(v).replace("\n", " ").strip()


def tables_from_page(page):
    tbls = page.extract_tables() or []
    if not tbls:
        tbls = page.extract_tables(
            {"vertical_strategy": "text", "horizontal_strategy": "text"}) or []
    return tbls


def parse_tables(pdf_path):
    import pdfplumber
    rows, full_text = [], ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"
            for table in tables_from_page(page):
                if not table or len(table) < 2:
                    continue
                hi, m, sc = find_header(table)
                if hi is None or "date" not in m or not ("debit" in m or "credit" in m):
                    continue
                for r in table[hi + 1:]:
                    date = to_iso(_cell(r, m, "date"))
                    if not date:
                        continue
                    narr = _cell(r, m, "narration")
                    if narr and any(k in narr.lower() for k in SKIP_NARRATION):
                        continue
                    deb = to_amount(_cell(r, m, "debit"))
                    cr = to_amount(_cell(r, m, "credit"))
                    if deb is None and cr is None:
                        continue
                    rows.append({"date": date,
                                 "value_date": to_iso(_cell(r, m, "value_date")),
                                 "ref": _cell(r, m, "ref"), "narration": narr,
                                 "debit_amount": deb, "credit_amount": cr,
                                 "running_balance": to_amount(_cell(r, m, "balance"))})
    holder, number = account_info(full_text)
    for r in rows:
        r["account_holder"] = holder; r["account_number"] = number
    return rows, full_text


# ---- 3b. line parser (shared by digital-text-layer and OCR paths) ----
DATE_RE = re.compile(r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})"
                     r"|(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})")
AMT_RE = re.compile(r"\d[\d,]*\.\d{2}")


def parse_txn_line(line, is_card=False):
    """Parse one physical line into a txn dict.
    Bank layout: '... narration amount balance'  -> 2nd-last number = amount,
    last = running balance.  Card layout / single number: last = amount.
    Date fragments (27.02) are excluded from amount matching.
    """
    s = line.strip()
    if len(s) < 8:
        return None
    md = DATE_RE.search(s)
    if not md or md.start() > 6:
        return None
    iso = to_iso(md.group(0))
    if not iso:
        return None

    tail = s[md.end():]
    y, mo, dy = iso.split("-")
    lookalikes = {f"{int(dy)}.{mo}", f"{dy}.{mo}",
                  f"{int(dy):02d}.{int(mo):02d}", f"{int(dy)}.{int(mo)}"}
    amts = [a for a in AMT_RE.finditer(tail)
            if a.group(0).replace(",", "") not in lookalikes]
    if not amts:
        return None

    if is_card or len(amts) == 1:
        amt_m, bal = amts[-1], None
    else:
        amt_m, bal = amts[-2], to_amount(amts[-1].group(0))
    amt = to_amount(amt_m.group(0))
    if amt is None:
        return None

    after = tail[amt_m.end(): amt_m.end() + 6].upper()
    is_credit = "CR" in after
    narr = tail[:amt_m.start()]
    refm = re.search(r"\b(\d{8,})\b", narr)
    ref = refm.group(1) if refm else None
    if ref:
        narr = narr.replace(ref, "", 1)
    narr = re.sub(r"\s+", " ", narr).strip(" -|")
    narr = re.sub(r"\s+\d+\s*$", "", narr)
    if not narr:
        return None
    if any(k in narr.lower() for k in CARD_SKIP + SKIP_NARRATION):
        return None
    return {"date": iso, "value_date": None, "ref": ref, "narration": narr,
            "debit_amount": (None if is_credit else amt),
            "credit_amount": (amt if is_credit else None),
            "running_balance": bal}


def parse_digital_lines(pdf_path, is_card):
    """For PDFs that have a text layer but no extractable TABLE."""
    import pdfplumber
    rows, full = [], ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            full += txt + "\n"
            for line in txt.splitlines():
                r = parse_txn_line(line, is_card=is_card)
                if r:
                    rows.append(r)
    holder, number = account_info(full)
    for r in rows:
        r["account_holder"], r["account_number"] = holder, number
    return rows, full


def drop_date_outliers(rows, days):
    ds = sorted(pd.to_datetime([r["date"] for r in rows if r["date"]]))
    if len(ds) < 3:
        return rows
    med = ds[len(ds) // 2]
    return [r for r in rows if r["date"] and
            abs((pd.to_datetime(r["date"]) - med).days) <= days]


def parse_scanned(pdf_path, is_card=False):
    rows, full_text = [], ""
    try:
        import pytesseract
        from pdf2image import convert_from_path
        for img in convert_from_path(str(pdf_path), dpi=OCR_DPI):
            text = pytesseract.image_to_string(img, config="--psm 6")
            full_text += text + "\n"
            for line in text.splitlines():
                r = parse_txn_line(line, is_card=is_card)
                if r:
                    rows.append(r)
    except Exception as e:
        print("   ! OCR parse failed:", e)
        return [], ""
    rows = drop_date_outliers(rows, CARD_OUTLIER_DAYS)
    holder, number = account_info(full_text)
    for r in rows:
        r["account_holder"], r["account_number"] = holder, number
    return rows, full_text


# ---- 3c. LLM fallback (last resort), with the full narration contract ----
EXTRACT_SCHEMA = {"type": "object", "properties": {"transactions": {"type": "array",
    "items": {"type": "object", "properties": {
        "date": {"type": ["string", "null"]},
        "narration": {"type": ["string", "null"]},
        "reference": {"type": ["string", "null"]},
        "spend_category": {"type": ["string", "null"]},
        "debit_amount": {"type": ["number", "null"]},
        "credit_amount": {"type": ["number", "null"]},
        "running_balance": {"type": ["number", "null"]}}}}},
    "required": ["transactions"]}

EXTRACT_PROMPT = (
    "You extract transactions from an Indian bank or credit-card statement page "
    "into JSON with a 'transactions' list. One object per real transaction row, "
    "in the exact order shown, including any opening-balance and closing-balance "
    "rows that appear.\n"
    "FIELDS per row:\n"
    "- date: the transaction date. Dates are DAY-FIRST (DD/MM/YYYY); output "
    "YYYY-MM-DD. Drop rows with no real date and rows whose year differs from the "
    "statement period.\n"
    "- narration: THE MOST IMPORTANT FIELD. The full transaction description "
    "exactly as printed - merchant/payee name, UPI handle, IMPS/NEFT/RTGS details, "
    "remarks, BY/TO fields, everything. Copy VERBATIM. Do NOT shorten, summarize, "
    "paraphrase or translate. Never leave it blank for a real transaction. If a "
    "description wraps across multiple physical lines, join them into ONE string.\n"
    "- reference: ONLY the cheque number, UPI reference or transaction ID. Keep it "
    "SEPARATE from narration; never invent it (use null if absent).\n"
    "- spend_category: the statement's own spend-category label if it prints one, "
    "else null. (Ledger classification happens later - do not guess here.)\n"
    "- debit_amount / credit_amount: a row normally has EITHER a debit OR a credit, "
    "never both. Money OUT (withdrawal, purchase, recharge, transfer out, charge, "
    "fee) = debit_amount. Money IN (deposit, payment received, refund, anything "
    "marked CR) = credit_amount. Put the amount in the correct field, set the "
    "other to null.\n"
    "- running_balance: the balance printed for THAT row. If the statement prints "
    "no per-row balance (e.g. credit-card statements), set null.\n"
    "IGNORE non-transactions: terms & conditions, reward/offer/ad text, EMI "
    "illustrations, and summary boxes (Total Amount Due, Minimum Amount Due, "
    "Statement Summary). Never invent rows.\n"
    "All numbers plain: no currency symbols, commas or CR/DR suffix.\n\n"
    "PAGE:\n----\n{page}\n----")


def _llm_json(prompt, schema, timeout=600):
    payload = {"model": MODEL, "prompt": prompt, "stream": False, "format": schema,
               "keep_alive": "10m", "options": {"temperature": 0, "num_ctx": NUM_CTX}}
    r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
    return json.loads(r.json()["response"])


def llm_page(text):
    text = text[:PAGE_CHARS]
    try:
        return _llm_json(EXTRACT_PROMPT.format(page=text),
                         EXTRACT_SCHEMA).get("transactions") or []
    except Exception as e:
        print("   ! llm page failed:", e); return []


def llm_fallback(pdf_path, full_text):
    import pdfplumber
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
    except Exception:
        pass
    if not any(len(p.strip()) > 40 for p in pages):
        pages = [full_text] if full_text else []
    holder, number = account_info(full_text)
    rows = []
    for pg in pages:
        if len(pg.strip()) < 20:
            continue
        for t in llm_page(pg):
            rows.append({"account_holder": holder, "account_number": number,
                         "date": to_iso(t.get("date")), "value_date": None,
                         "ref": t.get("reference"), "narration": t.get("narration"),
                         "spend_category": t.get("spend_category"),
                         "debit_amount": to_amount(t.get("debit_amount")),
                         "credit_amount": to_amount(t.get("credit_amount")),
                         "running_balance": to_amount(t.get("running_balance"))})
    return rows


# ============================================================ 4. DETERMINISTIC FIXES
def fix_debit_credit(rows):
    """Direction is arithmetic wherever a running balance exists: balance up =>
    credit, down => debit. Fixes swapped sides and fills a missing amount from the
    delta. Returns count changed."""
    fixed, prev = 0, None
    for r in rows:
        bal = r.get("running_balance")
        d, c = r.get("debit_amount"), r.get("credit_amount")
        amt = d if d is not None else c
        if prev is not None and bal is not None:
            delta = round(bal - prev, 2)
            if amt is not None and abs(abs(delta) - amt) < 0.02:
                if delta > 0 and c is None:
                    r["credit_amount"], r["debit_amount"] = amt, None; fixed += 1
                elif delta < 0 and d is None:
                    r["debit_amount"], r["credit_amount"] = amt, None; fixed += 1
            elif amt is None and abs(delta) > 0.009:
                if delta > 0: r["credit_amount"] = abs(delta)
                else:         r["debit_amount"] = abs(delta)
                fixed += 1
        if bal is not None:
            prev = bal
    return fixed


def _rowkey(r):
    return (r.get("account_number"), r.get("date"), (r.get("narration") or "").strip().lower(),
            r.get("debit_amount"), r.get("credit_amount"), r.get("running_balance"))


def dedupe_exact(rows):
    """Remove a row only when it is identical to the IMMEDIATELY preceding row on
    every field INCLUDING running balance. Two genuinely separate transactions
    leave different balances, so an identical-including-balance repeat is a
    re-extraction artifact, safe to drop. Genuine repeated UPI transfers are kept."""
    out, prev = [], None
    for r in rows:
        k = _rowkey(r)
        if prev is not None and k == prev and (r.get("running_balance") is not None):
            continue
        out.append(r); prev = k
    return out


# ============================================================ 5. VERIFICATION
def validate(rows):
    """Row-level: does prev_balance +credit -debit = balance? Writes row_issues.
    Returns (consistency_pct_over_checkable_rows, n_checked)."""
    by = {}
    for i, r in enumerate(rows):
        by.setdefault((r.get("account_number"), r.get("account_holder")), []).append(i)
    chk = ok = 0
    for idxs in by.values():
        prev = None
        for i in idxs:
            r = rows[i]; issues = []
            d = r.get("debit_amount") or 0; c = r.get("credit_amount") or 0
            b = r.get("running_balance")
            if (r.get("debit_amount") not in (None, 0)) and (r.get("credit_amount") not in (None, 0)):
                issues.append("BOTH FILLED")
            if (d or c) and not (r.get("narration") or "").strip():
                issues.append("MISSING NARRATION")
            if b is not None and prev is not None:
                delta = round(b - prev, 2); net = round(c - d, 2); chk += 1
                if abs(delta - net) > 0.01:
                    issues.append("DEBIT/CREDIT SWAPPED" if abs(delta + net) <= 0.01
                                  else f"BALANCE OFF {round(net - delta, 2)}")
                else:
                    ok += 1
            if b is not None:
                prev = b
            r["row_issues"] = ", ".join(issues)
    return (round(100 * ok / chk, 1) if chk else None), chk


_NUM = r"([\d,]+\.\d{2})"
def statement_audit(rows, full_text, is_card):
    """Statement-level integrity, independent of per-row checks."""
    audit = {"checks": 0, "passed": 0, "problems": []}
    def find(*pats):
        for p in pats:
            m = re.search(p, full_text or "", re.I)
            if m:
                return to_amount(m.group(1))
        return None
    sd = round(sum(r.get("debit_amount") or 0 for r in rows), 2)
    sc = round(sum(r.get("credit_amount") or 0 for r in rows), 2)
    audit["total_debit"], audit["total_credit"] = sd, sc

    if is_card:
        chg = find(r"(?:purchases?|total\s+spends?)(?:\s*(?:&|and|/)\s*(?:charges?|debits?))?\D{0,15}" + _NUM)
        pay = find(r"payments?(?:\s*(?:&|and|/)\s*credits?)?\D{0,15}" + _NUM)
        for name, stmt, ours in (("charges", chg, sd), ("payments", pay, sc)):
            if stmt is not None:
                audit["checks"] += 1
                if abs(stmt - ours) < 0.02: audit["passed"] += 1
                else: audit["problems"].append(f"{name}: statement {stmt} vs extracted {ours}")
    else:
        opening = find(r"opening\s+balance\D{0,20}" + _NUM, r"b/?f(?:\s+balance)?\D{0,20}" + _NUM)
        closing = find(r"closing\s+balance\D{0,20}" + _NUM)
        bals = [r["running_balance"] for r in rows if r.get("running_balance") is not None]
        if closing is None and bals:
            closing = bals[-1]
        if opening is not None and closing is not None:
            audit["checks"] += 1
            calc = round(opening + sc - sd, 2)
            if abs(calc - closing) < 0.02: audit["passed"] += 1
            else: audit["problems"].append(
                f"opening {opening} + cr {sc} - dr {sd} = {calc} != closing {closing}")
        m = re.search(r"(?:total\s+)?(?:no\.?|number)\s+of\s+transactions\D{0,10}(\d+)",
                      full_text or "", re.I)
        if m:
            audit["checks"] += 1
            if int(m.group(1)) == len(rows): audit["passed"] += 1
            else: audit["problems"].append(f"statement says {m.group(1)} txns, extracted {len(rows)}")
    audit["verdict"] = ("UNVERIFIED" if audit["checks"] == 0 else
                        "PASS" if audit["passed"] == audit["checks"] else "FAIL")
    return audit


def verify_and_finalize(fname, rows, full_text, method, is_card=False):
    """Run row validation + statement audit, attach fields, return
    (rows, stmt_check, verification_note)."""
    pct, chk = validate(rows) if rows else (None, 0)
    audit = statement_audit(rows, full_text, is_card) if rows else \
            {"verdict": "EMPTY", "checks": 0, "passed": 0, "problems": [],
             "total_debit": 0, "total_credit": 0}
    coverage = round(100 * chk / len(rows), 1) if rows else 0.0
    for r in rows:
        r["verification"] = audit["verdict"]
    note = f"{audit['verdict']} (stmt {audit['passed']}/{audit['checks']}, row-recon {pct}%)"
    stmt_check = {"balance_consistency_pct": pct, "balance_coverage_pct": coverage,
                  "problems": audit["problems"], "verdict": audit["verdict"],
                  "total_debit": audit["total_debit"], "total_credit": audit["total_credit"]}
    return rows, stmt_check, note


# ============================================================ ROUTER
def process(pdf_path):
    t0 = time.time()
    rows, full_text = parse_tables(pdf_path)
    card = is_card_text(full_text)
    method = "table"
    if not rows:
        drows, dtext = parse_digital_lines(pdf_path, card)
        if dtext.strip():
            full_text = full_text or dtext
            card = card or is_card_text(dtext)
        if len(drows) >= 3:
            rows, method = drows, "digital-lines"
    if not rows:
        srows, stext = parse_scanned(pdf_path, is_card=card)
        if stext:
            full_text = stext
            card = card or is_card_text(stext)
        if len(srows) >= 3:
            rows, method = srows, "ocr-lines"
        else:
            rows, method = llm_fallback(pdf_path, full_text), "llm"

    if rows and not card:
        fixed = fix_debit_credit(rows)
        if fixed:
            print(f"   direction fixed from balance delta: {fixed} rows")

    # if a digital table barely reconciles, try the model and keep whichever is better
    pct, chk = validate(rows) if rows else (None, 0)
    if rows and method == "table" and chk > 0 and pct is not None and pct < ACCEPT_BALANCE_PCT:
        fb = llm_fallback(pdf_path, full_text)
        if fb and not card:
            fix_debit_credit(fb)
        fpct, _ = validate(fb) if fb else (None, 0)
        if fb and (fpct or 0) > (pct or 0):
            rows, method = fb, "llm"

    card_rec = reconcile_card(rows, full_text) if card else None
    return rows, method, pct, chk, card_rec, round(time.time() - t0, 1)


def reconcile_card(rows, text):
    if not rows:
        return None
    def find(pat):
        m = re.search(pat, text or "", re.I)
        return to_amount(m.group(1)) if m else None
    sd = round(sum(r.get("debit_amount") or 0 for r in rows), 2)
    sc = round(sum(r.get("credit_amount") or 0 for r in rows), 2)
    return {"sum_debit": sd, "sum_credit": sc,
            "stmt_charges": find(r"purchase[s]?\s*/?\s*charge[s]?\D{0,12}([\d,]+\.\d{2})"),
            "stmt_payments": find(r"payment[s]?\s*/?\s*credit[s]?\D{0,12}([\d,]+\.\d{2})")}


# ============================================================ 6. CROSS-FILE RELATIONS
def flag_duplicates_and_contra(rows, contra_days=5):
    """Runs once over ALL files together. Sets:
      possible_duplicate_of : same account+date+amount+direction+balance in another
                              file (overlapping statements) - FLAG, do not delete.
      contra_pair_of        : opposite-direction equal-amount row in a DIFFERENT
                              account within contra_days (a self / inter-account
                              transfer seen from both sides).
    Indices are 1-based row numbers within the combined output.
    """
    for i, r in enumerate(rows):
        r.setdefault("possible_duplicate_of", None)
        r.setdefault("contra_pair_of", None)

    # possible duplicates (same account, identical signature, different file)
    seen = {}
    for i, r in enumerate(rows):
        amt = r.get("debit_amount") if r.get("debit_amount") is not None else r.get("credit_amount")
        side = "D" if r.get("debit_amount") is not None else "C"
        key = (r.get("account_number"), r.get("date"), amt, side, r.get("running_balance"))
        if None in (key[0], key[1], key[2]):
            continue
        if key in seen:
            first = seen[key]
            if rows[first].get("source_file") != r.get("source_file"):
                r["possible_duplicate_of"] = first + 1
        else:
            seen[key] = i

    # contra / inter-account transfers
    def amt_side(r):
        if r.get("debit_amount") is not None:
            return r["debit_amount"], "D"
        if r.get("credit_amount") is not None:
            return r["credit_amount"], "C"
        return None, None
    dt = {i: pd.to_datetime(r["date"], errors="coerce") for i, r in enumerate(rows) if r.get("date")}
    for i, r in enumerate(rows):
        if r.get("contra_pair_of") is not None or i not in dt:
            continue
        ai, si = amt_side(r)
        if ai is None:
            continue
        for j in range(i + 1, len(rows)):
            if j not in dt or rows[j].get("contra_pair_of") is not None:
                continue
            aj, sj = amt_side(rows[j])
            if aj is None or si == sj or abs(ai - aj) > 0.01:
                continue
            if rows[i].get("account_number") == rows[j].get("account_number"):
                continue
            if pd.isna(dt[i]) or pd.isna(dt[j]) or abs((dt[i] - dt[j]).days) > contra_days:
                continue
            rows[i]["contra_pair_of"] = j + 1
            rows[j]["contra_pair_of"] = i + 1
            break
    return rows


# ============================================================ 7. CLASSIFICATION
CATEGORY_RULES = [
    (r"swiggy|zomato|instamart|zepto|blinkit|bigbasket|dominos|kfc|mcdonald|eatclub|nuzo|cafe|coffee|hotel\b|restaurant", "food_delivery"),
    (r"\bemi\b|loan\s*(repay|instal|a/?c)|bajaj\s*fin|hdb\s*fin|cred\s*loan", "loan_emi"),
    (r"\brent\b|house\s*rent|rent\s*pay", "rent"),
    (r"salary|payroll", "income_active"),
    (r"int\.?\s*pd|:int\.pd:|sb\s*int|sbint|monthly\s+interest|interest\s+payout", "interest_income"),
    (r"dividend|mutual\s*fund\s*div", "income_passive"),
    (r"itd\s*tax\s*refund|income\s*tax\s*refund|tax\s*refund", "tax_refund"),
    (r"gst|advance\s*tax|self\s*assessment|tds\s*pay", "tax_payment"),
    (r"airtel|\bjio\b|vodafone|\bvi\b\s*bill|bses|tata\s*power|torrent\s*power|broadband|billdesk|bbps|igl\b|water\s*bill|electricity|bharatkosh", "bills_utilities"),
    (r"lic\b|life\s*insuranc|insurance|policy\s*prem|hdfc\s*life|max\s*life", "insurance"),
    (r"hospital|pharma|medical|clinic|diagnostic|apollo|medplus|chemist", "medical"),
    (r"\bsip\b|mutual\s*fund|\bmf\b|groww|zerodha|kite|upstox|etmoney|indianclearingcorp|nextbillion|tatamf|initial\s+payin\s+fd|nach.*(mut|mf)|icclmf|nps\b|ppf\b", "investment"),
    (r"irctc|makemytrip|ola\b|uber|redbus|indigo|air\s*india|vistara|goibibo|travel", "travel"),
    (r"amazon|flipkart|myntra|ajio|nykaa|shopping|retail|mega\s*mart|dmart|reliance\s*retail", "shopping"),
    (r"atm|cash\s*wdl|cash\s*withdrawal|by\s*cash|to\s*cash|self\s*wdl", "cash_withdrawal"),
    (r"amb\s+non\s+maintenance|sms.?(chgs|alert)|dcardfee|debit\s*card\s*fee|cashtxnchgs|rtn\s*chg|charge.*gst|processing\s*fee|penal|folio\s*chg", "bank_charges"),
    (r"staff|wages|salary\s*to|emp\s*pay", "staff_payment"),
]


def apply_keyword_categories(rows):
    """Deterministic categories. Own-name inter-account moves -> self_transfer
    (also uses contra_pair_of if set). First matching keyword wins."""
    for r in rows:
        n = (r.get("narration") or "").upper()
        holder = (r.get("account_holder") or "").upper()
        toks = [t for t in re.split(r"\s+", holder) if len(t) > 2]
        cat = None
        if r.get("contra_pair_of") is not None:
            cat = "self_transfer"
        elif toks and sum(t in n for t in toks) >= max(1, len(toks) - 1) and len(toks) >= 1:
            # narration names the account holder -> money moving between own accounts
            cat = "self_transfer"
        else:
            for pat, c in CATEGORY_RULES:
                if re.search(pat, n, re.I):
                    cat = c; break
        r["category"] = cat  # may be None -> LLM pass fills it


CLASSIFY_SCHEMA = {"type": "object", "properties": {"labels": {"type": "array",
    "items": {"type": "object", "properties": {
        "i": {"type": "integer"},
        "category": {"type": "string", "enum": CATEGORIES}}}}},
    "required": ["labels"]}

CLASSIFY_PROMPT = (
    "Classify each numbered Indian bank/card narration into EXACTLY one category "
    "from this list: " + ", ".join(CATEGORIES) + ".\n"
    "Guidance: income_active = salary/business receipts; income_passive = "
    "interest/dividend/rent received; interest_income = bank interest paid to the "
    "account; self_transfer = money between the customer's own accounts; loan_emi = "
    "loan instalments; staff_payment = wages/salary paid out; bills_utilities = "
    "electricity/telecom/gas; investment = SIP/MF/FD/stocks; bank_charges = fees/GST "
    "on charges; cash_withdrawal = ATM/cash. Use miscellaneous ONLY if nothing fits.\n"
    'Return {"labels":[{"i":<number>,"category":<one label>}, ...]} for every line.\n\n'
    "NARRATIONS:\n{lines}")


def llm_classify(rows, batch_size=40):
    """Fill category for rows the keyword rules left as None. Model-as-fallback."""
    todo = [i for i, r in enumerate(rows) if not r.get("category")]
    if not todo or not USE_LLM_CLASSIFY:
        for i in todo:
            rows[i]["category"] = "miscellaneous"
        return 0
    filled = 0
    for s in range(0, len(todo), batch_size):
        chunk = todo[s:s + batch_size]
        lines = "\n".join(f"{k+1}. {(rows[idx].get('narration') or '')[:160]}"
                          for k, idx in enumerate(chunk))
        try:
            out = _llm_json(CLASSIFY_PROMPT.format(lines=lines), CLASSIFY_SCHEMA)
            got = {l["i"]: l["category"] for l in out.get("labels", [])
                   if l.get("category") in CATEGORIES}
        except Exception as e:
            print("   ! classify batch failed:", e); got = {}
        for k, idx in enumerate(chunk):
            rows[idx]["category"] = got.get(k + 1, "miscellaneous")
            if k + 1 in got:
                filled += 1
    return filled


def classify_all(rows):
    apply_keyword_categories(rows)
    llm_classify(rows)
    return rows


# ============================================================ 8. ANALYTICS (deterministic)
def _months_span(dates):
    ds = [pd.to_datetime(d, errors="coerce") for d in dates if d]
    ds = [d for d in ds if not pd.isna(d)]
    if not ds:
        return 1, None, None
    lo, hi = min(ds), max(ds)
    months = (hi.year - lo.year) * 12 + (hi.month - lo.month) + 1
    return max(1, months), lo, hi


def analyze_ledger(rows):
    """All figures computed in code from classified rows, so they are exact and
    auditable. Definitions are returned alongside the numbers."""
    real = [r for r in rows if r.get("possible_duplicate_of") is None]
    n_months, lo, hi = _months_span([r.get("date") for r in real])

    def dsum(cats=None, exclude=None):
        t = 0.0
        for r in real:
            if r.get("debit_amount") is None:
                continue
            c = r.get("category")
            if cats and c not in cats: continue
            if exclude and c in exclude: continue
            t += r["debit_amount"]
        return round(t, 2)

    def csum(cats=None):
        t = 0.0
        for r in real:
            if r.get("credit_amount") is None:
                continue
            if cats and r.get("category") not in cats: continue
            t += r["credit_amount"]
        return round(t, 2)

    total_income = csum(INCOME_CATS) or 0.0
    # burn = expense debits, excluding self-transfers / investments / cash moves
    burn_total = dsum(exclude=NEUTRAL_CATS)
    fixed = dsum(FIXED_CATS)
    variable = round(burn_total - fixed, 2)
    emi = dsum({"loan_emi"})
    invest = dsum({"investment"})

    monthly_burn = round(burn_total / n_months, 2)
    savings_ratio = round((total_income - burn_total) / total_income, 3) if total_income else None
    emi_load_pct = round(100 * emi / total_income, 1) if total_income else None
    fixed_pct = round(100 * fixed / burn_total, 1) if burn_total else None
    variable_pct = round(100 * variable / burn_total, 1) if burn_total else None

    # lifestyle inflation: variable monthly spend, first half vs second half of period
    infl = None
    if lo is not None and hi is not None and n_months >= 4:
        mid = lo + (hi - lo) / 2
        v1 = [r["debit_amount"] for r in real
              if r.get("debit_amount") and r.get("category") not in (NEUTRAL_CATS | FIXED_CATS)
              and pd.to_datetime(r["date"], errors="coerce") <= mid]
        v2 = [r["debit_amount"] for r in real
              if r.get("debit_amount") and r.get("category") not in (NEUTRAL_CATS | FIXED_CATS)
              and pd.to_datetime(r["date"], errors="coerce") > mid]
        h = max(1, n_months // 2)
        a1, a2 = round(sum(v1) / h, 2), round(sum(v2) / h, 2)
        if a1 > 0:
            chg = round(100 * (a2 - a1) / a1, 1)
            infl = f"variable spend {a1}/mo -> {a2}/mo ({'+' if chg>=0 else ''}{chg}%)"

    # risk signals (deterministic keyword + arithmetic)
    signals = []
    text_all = " ".join((r.get("narration") or "").lower() for r in real)
    if re.search(r"funds?\s*insufficient|insufficient\s*bal|return\s*charge|rtn\s*chg|cheque\s*return|ecs\s*return|bounce", text_all):
        signals.append("returned/bounced transactions present")
    if re.search(r"amb\s*non\s*maintenance|min(imum)?\s*balance\s*charge|penal", text_all):
        signals.append("minimum-balance / penal charges")
    if re.search(r"casino|bet365|dream11|rummy|gambl|1xbet", text_all):
        signals.append("gambling-related merchants")
    if emi_load_pct is not None and emi_load_pct > 40:
        signals.append(f"high EMI load ({emi_load_pct}% of income)")
    if savings_ratio is not None and savings_ratio < 0:
        signals.append("negative savings (spending exceeds income)")

    return {
        "period_start": None if lo is None else lo.strftime("%Y-%m-%d"),
        "period_end": None if hi is None else hi.strftime("%Y-%m-%d"),
        "months": n_months,
        "total_income": total_income,
        "total_expense_burn": burn_total,
        "total_investment": invest,
        "monthly_burn": monthly_burn,
        "fixed_expense_pct": fixed_pct,
        "variable_expense_pct": variable_pct,
        "savings_ratio": savings_ratio,
        "emi_load_pct": emi_load_pct,
        "lifestyle_inflation_note": infl,
        "risk_signals": signals,
        "_definitions": ("burn=expense debits excl. self_transfer/investment/cash; "
                         "savings_ratio=(income-burn)/income; emi_load=EMI/income; "
                         "fixed=loan_emi+rent+bills+staff+insurance"),
    }


# ============================================================ UPLOAD DEDUPE
_seen_hashes = {}
def is_duplicate_upload(fname, payload):
    h = hashlib.sha256(payload).hexdigest()
    if h in _seen_hashes:
        print(f"[SKIP] {fname} is an exact duplicate file of {_seen_hashes[h]}")
        return True
    _seen_hashes[h] = fname
    return False
