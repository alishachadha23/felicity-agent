"""
config.py

Global configuration for the Ledger Extraction Pipeline.
Edit this file to change models, thresholds, OCR settings,
or Ollama connection details.
"""

# -----------------------------
# LLM Configuration
# -----------------------------

MODEL = "gemma4:e4b"

OLLAMA_URL = "http://localhost:11434/api/generate"

NUM_CTX = 8192

PAGE_CHARS = 9000


# -----------------------------
# OCR Configuration
# -----------------------------

OCR_DPI = 300

CARD_OUTLIER_DAYS = 150


# -----------------------------
# Validation Configuration
# -----------------------------

# Minimum percentage of balance reconciliation
# required to accept digital parsing.

ACCEPT_BALANCE_PCT = 90.0


# -----------------------------
# Runtime Configuration
# -----------------------------

STREAM_RESPONSE = False

TEMPERATURE = 0

KEEP_ALIVE = "10m"


# -----------------------------
# Output Configuration
# -----------------------------

OUTPUT_FILE = "ledger_output.xlsx"
