import re


_TSS_TEXT_REPLACEMENTS = {
    "\u00bd": "1/2",
    "\u00ae": "",
    "\u2122": "",
    "\u00a9": "",
}


def tss_unsafe_characters(value: str) -> list[str]:
    chars = []
    seen = set()
    for char in str(value or ""):
        if char not in _TSS_TEXT_REPLACEMENTS:
            continue
        if char in seen:
            continue
        seen.add(char)
        chars.append(char)
    return chars


def format_tss_unsafe_character(char: str) -> str:
    if char in {"\r", "\n", "\t"}:
        display = repr(char)[1:-1]
    elif char.isprintable():
        display = char
    else:
        display = repr(char)[1:-1]
    return f"'{display}' (U+{ord(char):04X})"


def tss_safe_text_suggestion(value: str) -> str:
    text = str(value or "")
    for source, replacement in _TSS_TEXT_REPLACEMENTS.items():
        text = text.replace(source, replacement)
    return re.sub(r"\s+", " ", text).strip()


def tss_unsafe_value_message(label: str, value: str, max_chars: int = 4) -> str:
    chars = tss_unsafe_characters(value)
    if not chars:
        return ""
    formatted = ", ".join(format_tss_unsafe_character(char) for char in chars[:max_chars])
    extra = f" (+{len(chars) - max_chars} more)" if len(chars) > max_chars else ""
    message = f"{label} contains characters that TSS may reject: {formatted}{extra}."
    suggestion = tss_safe_text_suggestion(value)
    if suggestion and suggestion != str(value or "").strip():
        message += f" Suggested safe text: {suggestion}."
    else:
        message += " Replace special characters with plain ASCII text before sending this data to TSS."
    return message


def tss_unsafe_value_tip(value: str) -> str:
    chars = tss_unsafe_characters(value)
    if not chars:
        return ""
    suggestion = tss_safe_text_suggestion(value)
    if suggestion and suggestion != str(value or "").strip():
        return f"Use plain ASCII text here. Suggested safe text: {suggestion}"
    return "Use plain ASCII text here and remove special characters before sending to TSS."


def tss_text_replacements() -> dict[str, str]:
    return dict(_TSS_TEXT_REPLACEMENTS)
