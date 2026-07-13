from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from production_hub.core.config.input_lists import input_list_by_key, row_cell


WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class SongSearchMatch:
    name: str
    library: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "library": self.library, "score": round(self.score, 3)}


def normalized_text(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def phonetic_key(value: str) -> str:
    text = normalized_text(value)
    replacements = (
        ("ng", "n"),
        ("nj", "n"),
        ("zh", "l"),
        ("sh", "s"),
        ("ch", "s"),
        ("th", "t"),
        ("dh", "d"),
        ("kh", "k"),
        ("gh", "g"),
        ("ph", "f"),
        ("bh", "b"),
        ("ck", "k"),
        ("qu", "k"),
        ("q", "k"),
        ("c", "k"),
        ("w", "v"),
        ("x", "ks"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"(.)\1+", r"\1", text)
    tokens = []
    for token in WORD_RE.findall(text):
        if not token:
            continue
        first = token[0]
        rest = re.sub(r"[aeiouy]", "", token[1:])
        tokens.append(first + rest)
    return " ".join(tokens)


def token_sort_key(value: str) -> str:
    return " ".join(sorted(WORD_RE.findall(normalized_text(value))))


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def song_score(query: str, title: str) -> float:
    query_norm = normalized_text(query)
    title_norm = normalized_text(title)
    query_is_number = bool(query_norm) and query_norm.isdigit()
    query_phonetic = phonetic_key(query)
    title_phonetic = phonetic_key(title)
    scores = [
        similarity(query_norm, title_norm),
        similarity(token_sort_key(query_norm), token_sort_key(title_norm)),
        similarity(query_phonetic, title_phonetic),
    ]
    if query_is_number:
        title_tokens = WORD_RE.findall(title_norm)
        numeric_tokens = [token for token in title_tokens if token.isdigit()]
        if numeric_tokens:
            if numeric_tokens[0] == query_norm:
                scores.append(1.0)
            elif query_norm in numeric_tokens:
                scores.append(0.98)
            elif any(query_norm in token for token in numeric_tokens):
                scores.append(0.82)
    elif query_norm and query_norm in title_norm:
        scores.append(0.96)
    if not query_is_number and query_phonetic and query_phonetic in title_phonetic:
        scores.append(0.9)
    query_tokens = set(WORD_RE.findall(query_norm))
    title_tokens = set(WORD_RE.findall(title_norm))
    if query_tokens and title_tokens:
        scores.append(len(query_tokens & title_tokens) / len(query_tokens | title_tokens))
    return max(scores)


def song_titles_from_input_list(context: Any, list_key: str = "song_library") -> list[tuple[str, str]]:
    definition = input_list_by_key(context.config, list_key)
    if definition is None:
        return []
    titles: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in definition.rows:
        if not row.enabled:
            continue
        library = str(row_cell(row, "library_name").value or definition.name).strip() or definition.name
        songs = row_cell(row, "songs").value
        if isinstance(songs, str):
            songs = [songs]
        if not isinstance(songs, list):
            continue
        for item in songs:
            name = str(item or "").strip()
            key = normalized_text(name)
            if not name or key in seen:
                continue
            seen.add(key)
            titles.append((name, library))
    if titles:
        return titles
    for item in definition.items:
        if item.enabled:
            titles.append((item.label, definition.name))
    return titles


def search_song_library(context: Any, query: str, list_key: str = "song_library", limit: int = 25) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    limit = max(1, min(25, int(limit or 25)))
    if not query:
        return []
    matches = [
        SongSearchMatch(name, library, song_score(query, name))
        for name, library in song_titles_from_input_list(context, list_key)
    ]
    matches.sort(key=lambda item: (-item.score, item.name.lower()))
    return [item.to_dict() for item in matches[:limit] if item.score >= 0.35]
