"""Value formatters for the text report and the HTML report. Shared so both
surfaces render the same number the same way."""

import re


def fmt_rate(x):
    return "n/a" if x is None else "{:.0f}%".format(x * 100)


def fmt_score(x):
    return "n/a" if x is None else "{:.2f}".format(x)


def fmt_cost(x):
    # Cost per 100 tests, as a bare dollar figure - the unit lives in the column
    # header / axis title ("cost /100"), not repeated on every value. Absolute run
    # totals use their own formatter (the run-cost panel).
    if x is None:
        return "n/a"
    if x == 0:
        return "$0 (free)"
    per_c = x * 100.0
    if per_c < 0.01:
        return "<$0.01"
    return "${:,.2f}".format(per_c)


def fmt_cost_u(x):
    """fmt_cost with the '/100' unit appended - for inline spots (per-service
    routing, the chart tooltip) that have no column header to carry the unit."""
    c = fmt_cost(x)
    return c + "/100" if c.startswith("$") and c != "$0 (free)" else c


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
