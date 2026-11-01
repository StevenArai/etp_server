"""A deliberately bounded subset of Everything's filename search language."""

from datetime import datetime, timezone
import fnmatch
import re
import time
import unicodedata

import regex


def with_hint(predicate, hint):
    predicate.fts = hint
    return predicate


def literal_hint(text):
    text = normalize(text.replace("/", "\\"))
    return '"' + text.replace('"', '""') + '"' if len(text) >= 3 else None


def normalize(value, case=False, diacritics=False, punctuation=False, whitespace=False):
    if not diacritics:
        value = "".join(c for c in unicodedata.normalize("NFD", value)
                        if not unicodedata.combining(c))
    if not case:
        value = value.casefold()
    if punctuation:
        value = "".join(c for c in value if not unicodedata.category(c).startswith("P"))
    if whitespace:
        value = "".join(c for c in value if not c.isspace())
    return value


def tokenize(text):
    tokens, current, quoted = [], [], False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "<>" and "".join(current).lower() in (
                "size:", "dm:", "dc:", "date_modified:", "date_created:"):
            current.append(char)
        elif not quoted and (char.isspace() or char in "<>|!"):
            if current:
                tokens.append("".join(current))
                current = []
            if not char.isspace():
                tokens.append(char)
        else:
            current.append(char)
    if quoted:
        raise ValueError("Unclosed quote")
    if current:
        tokens.append("".join(current))
    return tokens


def size_value(text):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgtpe]?)(?:i?b)?", text, re.I)
    if not match:
        raise ValueError("Invalid size")
    return float(match[1]) * 1024 ** ("kmgtpe".find(match[2].lower()) + 1 if match[2] else 0)


def date_value(text):
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise ValueError("Dates must use ISO format: YYYY-MM-DD") from exc


def comparison(text, converter):
    if ".." in text:
        lower, upper = text.split("..", 1)
        lo = converter(lower) if lower else float("-inf")
        hi = converter(upper) if upper else float("inf")
        return lambda value: lo <= value <= hi
    match = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", text)
    if not match:
        raise ValueError("Missing comparison value")
    operand = converter(match[2])
    op = match[1] or "="
    return lambda value: {">": value > operand, "<": value < operand,
                          ">=": value >= operand, "<=": value <= operand,
                          "=": value == operand}[op]


class Matcher:
    def __init__(self, text, state, prefix="", deadline=None):
        self.deadline = deadline or time.monotonic() + 10
        self.options = {key: bool(state.get(prefix + key, 0)) for key in
                        ("case", "diacritics", "whole_word", "path", "regex", "prefix",
                         "suffix", "ignore_punctuation", "ignore_whitespace")}
        self.tokens = tokenize(text) if not self.options["regex"] else []
        self.position = 0
        if self.options["regex"]:
            self.matches = self.term(text, force_regex=True)
        else:
            self.matches = self.parse_or() if self.tokens else lambda row: True
            if self.position != len(self.tokens):
                raise ValueError("Unexpected search token")

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def parse_or(self):
        parts = [self.parse_and()]
        while self.peek() == "|":
            self.position += 1
            parts.append(self.parse_and())
        hints = [getattr(part, "fts", None) for part in parts]
        hint = "(" + " OR ".join(hints) + ")" if all(hints) else None
        return with_hint(lambda row: any(part(row) for part in parts), hint)

    def parse_and(self):
        parts = []
        while self.peek() is not None and self.peek() not in (">", "|"):
            parts.append(self.parse_unary())
        if not parts:
            raise ValueError("Missing search term")
        hints = [getattr(part, "fts", None) for part in parts]
        hint = "(" + " AND ".join(hint for hint in hints if hint) + ")" if any(hints) else None
        return with_hint(lambda row: all(part(row) for part in parts), hint)

    def parse_unary(self):
        token = self.peek()
        self.position += 1
        if token == "!":
            part = self.parse_unary()
            return lambda row: not part(row)
        if token == "<":
            part = self.parse_or()
            if self.peek() != ">":
                raise ValueError("Unclosed search group")
            self.position += 1
            return part
        if token is None or token in ("|", ">"):
            raise ValueError("Missing search term")
        return self.term(token)

    def term(self, text, force_regex=False):
        options = dict(self.options)
        target = "full" if options["path"] or "\\" in text or "/" in text else "name"
        if not force_regex:
            while ":" in text:
                function, argument = text.split(":", 1)
                function = function.lower()
                if len(function) == 1:  # virtual drive, e.g. N:\folder
                    break
                if function in ("file", "folder"):
                    nested = self.term(argument) if argument else lambda row: True
                    directory = function == "folder"
                    return with_hint(lambda row: bool(row["is_dir"]) == directory and nested(row),
                                     getattr(nested, "fts", None))
                if function == "ext":
                    extensions = {part.lower().lstrip(".") for part in argument.split(";")}
                    hints = [literal_hint(part) for part in extensions]
                    hint = "(" + " OR ".join(hints) + ")" if hints and all(hints) else None
                    return with_hint(lambda row: not row["is_dir"] and row["extension"].lower() in extensions, hint)
                if function in ("size", "dm", "dc", "date_modified", "date_created"):
                    column = {"size": "size", "dm": "modified", "dc": "created",
                              "date_modified": "modified", "date_created": "created"}[function]
                    compare = comparison(argument, size_value if column == "size" else date_value)
                    return lambda row: row[column] is not None and compare(row[column])
                if function in ("path", "name", "parent"):
                    target = {"path": "full", "name": "name", "parent": "path"}[function]
                elif function in ("case", "nocase"):
                    options["case"] = function == "case"
                elif function in ("regex", "wholeword", "diacritics"):
                    options[{"wholeword": "whole_word"}.get(function, function)] = True
                    if function == "regex":
                        text = argument
                        break
                else:
                    raise ValueError("Unsupported search function: " + function)
                text = argument
        if force_regex:
            options["regex"] = True
        def norm(value):
            return normalize(value, options["case"], options["diacritics"],
                             options["ignore_punctuation"], options["ignore_whitespace"])
        if options["regex"]:
            # Folding regex syntax (e.g. \\D) changes its meaning; only fold the haystack.
            pattern = regex.compile(text, 0 if options["case"] else regex.I | regex.FULLCASE)
        else:
            text = norm(text.replace("/", "\\"))
            if "*" in text or "?" in text:
                pattern = regex.compile(fnmatch.translate(text))
            else:
                pattern = None
        word_pattern = re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)") if (
            pattern is None and options["whole_word"]) else None
        def match(row):
            value = norm(row[target])
            if pattern is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Search timed out")
                return pattern.search(value, timeout=min(0.05, remaining)) is not None
            if word_pattern is not None:
                return word_pattern.search(value) is not None
            if options["prefix"]:
                return value.startswith(text)
            if options["suffix"]:
                return value.endswith(text)
            return text in value
        hint = None
        if (not options["regex"] and not options["ignore_punctuation"] and
                not options["ignore_whitespace"] and not (pattern is not None and "[" in text)):
            # A mandatory literal fragment also exists in the indexed full path.
            fragments = re.split(r"[*?]", text)
            fragment = max(fragments, key=len, default="")
            hint = literal_hint(fragment)
        return with_hint(match, hint)

    def __call__(self, row):
        if time.monotonic() > self.deadline:
            raise TimeoutError("Search timed out")
        return self.matches(row)

    def fts_hint(self, text):
        """Prefilter required literals derived from the parsed boolean expression."""
        return getattr(self.matches, "fts", None)
