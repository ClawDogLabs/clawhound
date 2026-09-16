"""Value formatters for the text report and the HTML report. Shared so both
surfaces render the same number the same way."""

import re


def fmt_rate(x):
    return "n/a" if x is None else "{:.0f}%".format(x * 100)


def fmt_score(x):
    return "n/a" if x is None else "{:.2f}".format(x)


def is_known_free_provider(model_id):
    """True only for providers we KNOW cost nothing to run: local/self-hosted
    endpoints (ollama, and other local-inference prefixes as they show up).
    This is the only thing allowed to earn the "(free)" label on a $0 cost -
    a $0 reading from a PAID provider is not evidence of a free response, it
    is evidence the cost accounting for that record is broken (a promptfoo
    cache replay, a provider-side token-accounting gap, a judge missing from
    the grading price table - this session hit all three). Extend this list
    only for providers that are genuinely metered at $0, never to silence a
    suspicious zero."""
    if not model_id:
        return False
    m = model_id.lower()
    return m.startswith("ollama:") or m.startswith("ollama/")


def fmt_cost(x, model_id=None):
    # Cost per 100 tests, as a bare dollar figure - the unit lives in the column
    # header / axis title ("cost /100"), not repeated on every value. Absolute run
    # totals use their own formatter (the run-cost panel).
    if x is None:
        return "n/a"
    if x == 0:
        if is_known_free_provider(model_id):
            return "$0 (free)"
        # A paid provider reading exactly $0 is a red flag, not good news -
        # every real cause we've found is a cost-accounting gap, never a
        # genuinely free paid response. Surface it for a human to check
        # rather than asserting "free".
        return "$0 (verify pricing)"
    per_c = x * 100.0
    if per_c < 0.01:
        return "<$0.01"
    return "${:,.2f}".format(per_c)


def fmt_cost_u(x, model_id=None):
    """fmt_cost with the '/100' unit appended - for inline spots (per-service
    routing, the chart tooltip) that have no column header to carry the unit."""
    c = fmt_cost(x, model_id)
    already_qualified = c in ("$0 (free)", "$0 (verify pricing)")
    return c + "/100" if c.startswith("$") and not already_qualified else c


def fmt_latency(x):
    """Median latency in seconds (input is already seconds), rounded to tenths."""
    if x is None:
        return "n/a"
    return "{:.1f}".format(x) + "s"


def fmt_model_name(provider_id):
    """Convert a full provider ID to a human-readable model name.

    anthropic:messages:claude-opus-5 -> Claude Opus 5
    openai:gpt-5-mini -> GPT-5 Mini
    google:gemini-3.1-pro-preview -> Gemini 3.1 Pro Preview
    ollama:chat:llama3.1 -> Llama 3.1
    ollama:chat:deepseek-r1:14b -> Deepseek R1 14b
    """
    if not provider_id:
        return provider_id
    parts = provider_id.split(":")
    # For ollama models like "ollama:chat:model:size", take everything after "chat"
    if parts and parts[0] == "ollama" and len(parts) > 2 and parts[1] == "chat":
        model_part = ":".join(parts[2:])  # Join everything after "chat"
    else:
        model_part = parts[-1] if parts else provider_id
        if model_part in ("messages", "chat"):
            model_part = parts[-2] if len(parts) >= 2 else provider_id
    # Replace underscores and colons with spaces (word separators)
    name = model_part.replace("_", " ").replace(":", " ")
    # Replace hyphens with spaces ONLY between letters (e.g., "pro-preview" -> "pro preview")
    # Keep hyphens that are part of version numbers (e.g., "3.1" or "4-5" stay as-is)
    name = re.sub(r'-([a-z])', r' \1', name)  # Insert space before lowercase after hyphen
    name = re.sub(r'([a-z])-', r'\1 ', name)  # Insert space after letters before hyphen
    # Add spaces between letters and numbers where needed (e.g., "llama3.1" -> "llama 3.1")
    # But keep version identifiers compact (r1, v2, etc.) and parameter sizes (14b, 8b)
    name = re.sub(r'([a-z]{2,})(\d)', r'\1 \2', name)  # Split only multi-letter + digits (llama3 -> llama 3)
    name = re.sub(r'(\d)([a-z])', lambda m: m.group(1) + m.group(2) if m.group(2) in 'bm' else m.group(1) + ' ' + m.group(2), name)
    words = name.split()
    # Drop everything after the parameter-size token (e.g. "12b", "27b") - that's
    # the model's identity; trailing quant/tuning tags (Q4_K_M, it, GGUF, ...) are
    # noise for a routing table. "gemma4:12b-it-q4_K_M" -> stop at "12b".
    for i, w in enumerate(words):
        if re.match(r'^\d+b$', w, re.IGNORECASE):
            words = words[:i + 1]
            break
    formatted = []
    acronyms = {"gpt": "GPT", "llm": "LLM", "deepseek": "DeepSeek"}
    for w in words:
        # Preserve all-caps acronyms like "GPT", treat version numbers as-is
        if w.isupper() and len(w) <= 3:
            formatted.append(w)
        elif w[0].isdigit():
            formatted.append(w)
        elif w.lower() in acronyms:
            formatted.append(acronyms[w.lower()])
        else:
            formatted.append(w[0].upper() + w[1:] if w else w)
    return " ".join(formatted)
