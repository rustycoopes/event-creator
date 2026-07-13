"""Name-to-initials helper for the dashboard's agreed-by chips (ported from organize-me #100)."""


def to_initials(name: str) -> str:
    """First letter of the first word + first letter of the last word, uppercased.

    A single-word name falls back to just its first letter. An empty or
    whitespace-only name returns an empty string.
    """
    words = name.split()
    if not words:
        return ""
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()
