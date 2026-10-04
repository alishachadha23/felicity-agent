# felicity-agent

# Felicity Agent

### AI-Assisted Bank & Credit Card Statement → Audited Financial Ledger

Felicity Agent is a document-processing and financial analysis pipeline that converts **Indian bank and credit-card statements in PDF format into structured, validated, and analysis-ready ledgers**.

The system follows a **deterministic-first, model-as-fallback architecture**: conventional parsing and arithmetic are used wherever possible, while a local LLM is invoked only when documents are difficult to parse or transaction classification requires contextual understanding.

The goal is to make financial statement processing **structured, reproducible, auditable, and automation-ready**.

---

## What It Does

Given a bank or credit-card statement, Felicity Agent can:

- Extract transaction data from structured PDFs
- Parse PDFs with digital text layers
- Process scanned/image-based statements using OCR
- Use an LLM fallback for difficult document layouts
- Normalize dates and monetary values
- Identify debit and credit transactions
- Extract transaction narration and references
- Detect and remove duplicate transactions
- Reconcile transactions against running balances
- Automatically classify transactions into financial categories
- Calculate financial metrics and spending patterns
- Identify potential financial risk signals
- Export the processed ledger to Excel

---

## Architecture

The core design principle is:

> **Deterministic first. Model as fallback.**

Financial calculations should not depend on an LLM when they can be computed exactly with code.

The pipeline therefore uses traditional parsing and deterministic logic for extraction, validation, reconciliation, and analytics. The local LLM is reserved for tasks where contextual interpretation is genuinely useful.

```text
                 Bank / Credit Card PDF
                           │
                           ▼
                  Document Detection
                           │
                           ▼
              ┌────────────────────────┐
              │   Extraction Pipeline  │
              └────────────────────────┘
                           │
          ┌────────────────┼─────────────────┐
          ▼                ▼                 ▼
     Table Parser     Text Parser       OCR Parser
          │                │                 │
          └────────────────┼─────────────────┘
                           │
                           ▼
                    LLM Fallback
                (only when required)
                           │
                           ▼
              Transaction Normalization
                           │
                           ▼
              Debit/Credit Reconciliation
                           │
                           ▼
                 Duplicate Detection
                           │
                           ▼
                    Verification
                           │
                           ▼
                  Transaction Classification
                           │
                           ▼
                  Financial Analytics
                           │
                           ▼
                    Excel Ledger
```

The implementation explicitly separates extraction, deterministic fixes, verification, cross-file relationships, classification, and analytics.

---

## Key Features

### 1. Multi-Stage PDF Extraction

The system supports different types of financial statements:

**Structured statements**

Extracts tables directly from PDFs using `pdfplumber`.

**Digital text statements**

Parses transaction lines when the PDF contains text but does not expose a clean table structure.

**Scanned statements**

Uses OCR through:

- Tesseract
- `pdf2image`
- Pillow

**LLM fallback**

When conventional parsing fails, the pipeline can send extracted page text to a locally hosted model through Ollama and request structured JSON transaction data.

---

### 2. Deterministic Debit/Credit Correction

One of the important design decisions is that the LLM is **not trusted to determine financial arithmetic**.

When a running balance is available, the system checks the balance delta:

```text
Current Balance - Previous Balance
```

A positive delta indicates money entering the account, while a negative delta indicates money leaving it.

This allows the pipeline to detect incorrectly assigned debit/credit values and recover missing amounts when the balance difference provides enough evidence.

---

### 3. Transaction Classification

Transactions are categorized using a two-stage approach:

```text
Keyword / Rule Classification
              ↓
       Unclassified rows
              ↓
        LLM Classification
              ↓
        Final Category
```

The system includes categories such as:

- Income
- Passive income
- Interest income
- Self transfers
- Loan EMIs
- Staff payments
- Bills & utilities
- Investments
- Bank charges
- Cash withdrawals
- Miscellaneous

The LLM only handles transactions that deterministic rules cannot confidently classify.

---

### 4. Financial Analytics

After classification, financial metrics are calculated entirely in code.

The pipeline can calculate:

- Total income
- Total expenditure
- Monthly burn
- Fixed expenditure
- Variable expenditure
- EMI load
- Investment amount
- Savings ratio
- Fixed vs. variable spending percentage
- Variable-spending changes over time
- Potential financial risk signals

This keeps the numerical analysis reproducible and auditable rather than relying on generated answers from an LLM.

---

### 5. Financial Risk Signals

The system also looks for patterns that may require attention, including indicators such as:

- Returned transactions
- Bounced payments
- Insufficient-balance events
- Other transaction-level risk patterns

These signals are generated from deterministic rules and transaction data rather than asking an LLM to make unsupported financial judgments.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python |
| PDF extraction | pdfplumber |
| Data processing | Pandas, NumPy |
| OCR | Tesseract |
| PDF → images | pdf2image |
| Image processing | Pillow |
| Local LLM inference | Ollama |
| Spreadsheet output | OpenPyXL |
| HTTP/API communication | Requests |

The current repository dependencies are listed in `requirements.txt`.

---

## Project Structure

```text
felicity-agent/
│
├── extraction/
│   └── ...
│
├── ledger_pipeline.py
│   └── Core extraction, validation,
│       classification and analytics pipeline
│
├── config.py
│   └── Model, OCR, validation and output configuration
│
├── requirements.txt
│
├── gitignore
│
└── README.md
```

---

## Pipeline Stages

The main processing pipeline consists of the following stages:

### 0. Configuration

Controls:

- Local LLM model
- Ollama endpoint
- OCR resolution
- Validation thresholds
- Output configuration

The current configuration uses a local Ollama model and a 90% balance-reconciliation threshold for accepting digital parsing.

### 1. Value Cleaning

Normalizes:

- Monetary amounts
- Dates
- Missing values
- Statement-specific formatting

### 2. Document Detection

Determines:

- Account information
- Statement type
- Bank vs. credit-card format

### 3. Extraction

Attempts extraction in order:

```text
Table extraction
      ↓
Digital text parsing
      ↓
OCR
      ↓
LLM fallback
```

### 4. Deterministic Fixes

Applies:

- Debit/credit correction
- Balance-delta checks
- Duplicate detection
- Data normalization

### 5. Verification

Validates:

- Individual transaction rows
- Statement-level consistency
- Balance reconciliation

### 6. Cross-File Relationships

Looks for relationships between transactions and statements, including possible duplicates and contra/self-transfer patterns.

### 7. Classification

Uses:

```text
Rules → LLM fallback
```

to assign transaction categories.

### 8. Analytics

Computes financial metrics directly from the finalized ledger.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/alishachadha23/felicity-agent.git
cd felicity-agent
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

Activate it:

**Windows**

```bash
venv\Scripts\activate
```

**macOS / Linux**

```bash
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Local LLM Setup

Felicity Agent uses **Ollama** for local model inference.

Install Ollama and make sure the Ollama server is running locally.

The current configuration expects:

```text
http://localhost:11434/api/generate
```

The model can be configured in:

```text
config.py
```

For example:

```python
MODEL = "gemma4:e4b"
OLLAMA_URL = "http://localhost:11434/api/generate"
```

The LLM is intentionally used only as a fallback for difficult extraction and transaction classification rather than for deterministic financial calculations.

---

## Configuration

Important configuration parameters are available in `config.py`.

```python
MODEL = "gemma4:e4b"

OCR_DPI = 300

ACCEPT_BALANCE_PCT = 90.0

OUTPUT_FILE = "ledger_output.xlsx"
```

This makes it possible to adapt the pipeline to different hardware, OCR requirements, validation thresholds, and output formats.

---

## Example Output

The resulting ledger is designed to contain structured transaction information such as:

```text
Date
Account Holder
Account Number
Transaction Reference
Narration
Debit Amount
Credit Amount
Running Balance
Category
```

Example:

```text
2026-05-04
UPI/ABC STORE/...
Debit: 850.00
Credit: -
Balance: 42,150.00
Category: Shopping
```

The structured output can then be used for downstream financial analytics, reporting, or integration into larger financial workflows.

---

## Why Deterministic-First?

Financial document processing is particularly sensitive to hallucinations and arithmetic errors.

Instead of asking an LLM to:

> "Read this statement and calculate my finances."

Felicity Agent separates the problem:

```text
DOCUMENT
   ↓
EXTRACT
   ↓
VALIDATE
   ↓
RECONCILE
   ↓
CLASSIFY
   ↓
CALCULATE
```

The LLM is therefore treated as an **interpretation layer**, not as the source of truth for financial arithmetic.

This architecture improves:

- Reproducibility
- Auditability
- Error detection
- Debuggability
- Local/private processing
- Reliability of numerical calculations

---

## Privacy & Local Processing

The project is designed around local processing.

The LLM inference layer communicates with a local Ollama endpoint rather than requiring financial documents to be sent to a hosted LLM API.

**Important:** Never commit real bank statements, account numbers, credentials, API keys, or other sensitive financial information to the repository.

Use synthetic or anonymized statements when testing or demonstrating the project.

---

## Limitations

Financial statements vary significantly between banks and document formats.

Current limitations may include:

- Highly unusual statement layouts
- Poor-quality scans
- OCR errors
- Unrecognized transaction formats
- Statements with ambiguous balance information
- New transaction categories not covered by existing rules

The LLM fallback helps with some of these cases, but it should not be considered a guarantee of perfect extraction.

Always validate financial outputs against the original statement before using them for financial decisions.

---

## Future Improvements

Potential extensions include:

- Support for additional bank statement formats
- Better table-layout detection
- Multimodal document understanding
- Confidence scores for extracted transactions
- Human-in-the-loop verification
- Automatic anomaly detection
- Financial trend visualization
- Monthly financial reports
- REST API deployment
- Web interface for document upload
- Database-backed ledger storage
- Multi-statement financial profiling
- Automated report generation

---

## Use Cases

Felicity Agent can serve as a foundation for:

- Financial document automation
- Personal finance applications
- Banking workflows
- Financial data extraction
- Loan underwriting pipelines
- Expense management systems
- Accounting automation
- Fintech document-processing systems
- Financial analysis agents

---

## Author

**Alisha Chadha**

Built as part of hands-on work in **AI agents, document intelligence, financial automation, and applied AI/ML**.

[GitHub](https://github.com/alishachadha23)

---

## License

This project is currently provided for educational and development purposes.
