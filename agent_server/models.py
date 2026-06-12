"""Single source of truth for available model endpoints and defaults."""

AVAILABLE_MODELS = [
    {"endpoint": "databricks-claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    {"endpoint": "databricks-claude-opus-4-6",   "label": "Claude Opus 4.6"},
    {"endpoint": "databricks-claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
    {"endpoint": "databricks-claude-haiku-4-5",  "label": "Claude Haiku 4.5"},
    {"endpoint": "databricks-gpt-oss-120b",      "label": "GPT OSS 120B"},
    {"endpoint": "databricks-gpt-oss-20b",       "label": "GPT OSS 20B"},
    {"endpoint": "databricks-gemma-3-12b",       "label": "Gemma 3 12B"},
    {"endpoint": "poc-lor-classifier",           "label": "Llama 3.3 70B Instruct"},
]

DEFAULT_MODEL = "databricks-claude-sonnet-4-6"
