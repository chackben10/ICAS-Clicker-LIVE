from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any

from production_hub.core.config.input_lists import row_cell
from production_hub.core.config.models import InputListDefinition


WORD_RE = re.compile(r"[a-z0-9]+")
MAX_SEARCH_QUERY_CHARS = 256
MAX_SEARCH_QUERY_TOKENS = 12


@dataclass(frozen=True)
class SongSearchSource:
    name: str
    library: str
    uuid: str = ""
    lyrics: str = ""


@dataclass(frozen=True)
class SongSearchMatch:
    name: str
    library: str
    score: float
    uuid: str = ""
    match_field: str = ""
    lyric_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = {"name": self.name, "uuid": self.uuid, "library": self.library, "score": round(self.score, 3)}
        if self.match_field:
            result["match_field"] = self.match_field
        if self.match_field == "lyrics" and self.lyric_preview:
            result["lyric_preview"] = self.lyric_preview
        return result


@dataclass(frozen=True)
class LyricWord:
    normalized: str
    phonetic: str
    start: int
    end: int


@dataclass(frozen=True)
class CompiledSong:
    source: SongSearchSource
    title: str
    sorted_title: str
    phonetic_title: str
    title_tokens: frozenset[str]
    lyrics: str
    lyric_tokens: frozenset[str]
    lyric_phonetic_tokens: frozenset[str]
    lyric_words: tuple[LyricWord, ...]


@dataclass(frozen=True)
class SongSearchIndex:
    songs: tuple[CompiledSong, ...]
    lyric_token_postings: dict[str, int]
    lyric_phonetic_token_postings: dict[str, int]
    lyric_vocabulary_by_length: dict[int, tuple[str, ...]]


def normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(
        character
        if character.isalnum() or unicodedata.category(character).startswith("M")
        else " "
        for character in text
    )
    return " ".join(text.split())


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(normalized_text(value).split())


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
        first = token[0]
        rest = re.sub(r"[aeiouy]", "", token[1:])
        tokens.append(first + rest)
    return " ".join(tokens)


def token_sort_key(value: str) -> str:
    return " ".join(sorted(_tokens(value)))


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _title_score(
    query_norm: str,
    query_sorted: str,
    query_phonetic: str,
    query_tokens: frozenset[str],
    title_norm: str,
    title_sorted: str,
    title_phonetic: str,
    title_tokens: frozenset[str],
) -> float:
    query_is_number = bool(query_norm) and query_norm.isdigit()
    query_word_tokens = tuple(token for token in query_norm.split() if not token.isdigit())
    query_has_complete_phonetics = bool(query_word_tokens) and all(
        phonetic_key(token) for token in query_word_tokens
    )
    missing_unphonetic_token = any(
        not phonetic_key(token) and token not in title_tokens
        for token in query_word_tokens
    )
    query_phonetic_tokens = tuple(token for token in query_phonetic.split() if not token.isdigit())
    title_phonetic_tokens = tuple(token for token in title_phonetic.split() if not token.isdigit())
    phonetic_windows = [
        " ".join(title_phonetic_tokens[start : start + len(query_phonetic_tokens)])
        for start in range(max(0, len(title_phonetic_tokens) - len(query_phonetic_tokens) + 1))
        if query_phonetic_tokens
        and title_phonetic_tokens[start]
        and title_phonetic_tokens[start][0] == query_phonetic_tokens[0][0]
    ]
    phonetic_comparable = (
        query_has_complete_phonetics
        and len("".join(query_phonetic_tokens)) >= 3
        and bool(phonetic_windows)
    )
    scores = [] if missing_unphonetic_token else [
        similarity(query_norm, title_norm),
        similarity(query_sorted, title_sorted),
    ]
    if phonetic_comparable:
        phonetic_similarity = max(similarity(query_phonetic, window) for window in phonetic_windows)
        if phonetic_similarity >= 0.72:
            scores.append(phonetic_similarity)
    if query_is_number:
        numeric_tokens = [token for token in title_norm.split() if token.isdigit()]
        if numeric_tokens:
            if numeric_tokens[0] == query_norm:
                scores.append(1.0)
            elif query_norm in numeric_tokens:
                scores.append(0.98)
            elif any(query_norm in token for token in numeric_tokens):
                scores.append(0.82)
    elif query_norm and query_norm in title_norm:
        scores.append(0.96)
    if not query_is_number and phonetic_comparable and query_phonetic in phonetic_windows:
        scores.append(0.9)
    if query_tokens and title_tokens:
        scores.append(len(query_tokens & title_tokens) / len(query_tokens | title_tokens))
    return max(scores, default=0.0)


def song_score(query: str, title: str) -> float:
    query_norm = normalized_text(query)
    title_norm = normalized_text(title)
    return _title_score(
        query_norm,
        token_sort_key(query_norm),
        phonetic_key(query_norm),
        frozenset(query_norm.split()),
        title_norm,
        token_sort_key(title_norm),
        phonetic_key(title_norm),
        frozenset(title_norm.split()),
    )


def _lyric_words(value: str) -> tuple[LyricWord, ...]:
    text = str(value or "")
    words: list[LyricWord] = []
    start: int | None = None
    for index, character in enumerate(text):
        is_word_character = character.isalnum() or unicodedata.category(character).startswith("M")
        if is_word_character and start is None:
            start = index
        if is_word_character:
            continue
        if start is not None:
            normalized = normalized_text(text[start:index])
            if normalized:
                words.append(LyricWord(normalized, phonetic_key(normalized), start, index))
            start = None
    if start is not None:
        normalized = normalized_text(text[start:])
        if normalized:
            words.append(LyricWord(normalized, phonetic_key(normalized), start, len(text)))
    return tuple(words)


@lru_cache(maxsize=1)
def _compiled_index(sources: tuple[SongSearchSource, ...]) -> SongSearchIndex:
    songs: list[CompiledSong] = []
    token_postings: dict[str, int] = {}
    phonetic_token_postings: dict[str, int] = {}
    for index, source in enumerate(sources):
        title = normalized_text(source.name)
        lyrics = normalized_text(source.lyrics)
        lyric_words = _lyric_words(source.lyrics)
        lyric_tokens = frozenset(word.normalized for word in lyric_words)
        lyric_phonetic_tokens = frozenset(word.phonetic for word in lyric_words if word.phonetic)
        songs.append(
            CompiledSong(
                source=source,
                title=title,
                sorted_title=" ".join(sorted(title.split())),
                phonetic_title=phonetic_key(title),
                title_tokens=frozenset(title.split()),
                lyrics=lyrics,
                lyric_tokens=lyric_tokens,
                lyric_phonetic_tokens=lyric_phonetic_tokens,
                lyric_words=lyric_words,
            )
        )
        bit = 1 << index
        for token in lyric_tokens:
            token_postings[token] = token_postings.get(token, 0) | bit
        for token in lyric_phonetic_tokens:
            phonetic_token_postings[token] = phonetic_token_postings.get(token, 0) | bit
    vocabulary_by_length: dict[int, list[str]] = {}
    for token in token_postings:
        vocabulary_by_length.setdefault(len(token), []).append(token)
    return SongSearchIndex(
        tuple(songs),
        token_postings,
        phonetic_token_postings,
        {length: tuple(tokens) for length, tokens in vocabulary_by_length.items()},
    )


def _definition_by_key(context: Any, list_key: str) -> InputListDefinition | None:
    for raw_definition in context.config.ui.input_lists:
        definition = (
            raw_definition
            if isinstance(raw_definition, InputListDefinition)
            else InputListDefinition.from_dict(raw_definition)
        )
        if definition.key == list_key:
            return definition
    return None


def song_records_from_input_list(context: Any, list_key: str = "song_library") -> list[SongSearchSource]:
    definition = _definition_by_key(context, list_key)
    if definition is None:
        return []
    records: list[SongSearchSource] = []
    seen: set[str] = set()
    for row in definition.rows:
        if not row.enabled:
            continue
        library = str(row_cell(row, "library_name").value or definition.name).strip() or definition.name
        songs = row_cell(row, "songs").value
        if isinstance(songs, dict):
            song_items: list[Any] = [
                {"name": name, "uuid": uuid, "lyrics": ""}
                for name, uuid in songs.items()
            ]
        elif isinstance(songs, str):
            song_items = [songs]
        elif isinstance(songs, list):
            song_items = songs
        else:
            continue
        for item in song_items:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or item.get("label") or "").strip()
                uuid = str(item.get("uuid") or item.get("UUID") or item.get("value") or "").strip()
                lyrics = str(item.get("lyrics") or "")
            else:
                name = str(item or "").strip()
                uuid = ""
                lyrics = ""
            normalized_name = normalized_text(name)
            dedupe_key = f"uuid:{uuid.casefold()}" if uuid else f"title:{normalized_name}"
            if not name or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(SongSearchSource(name, library, uuid, lyrics))
    if records or definition.rows:
        return records
    for item in definition.items:
        if item.enabled:
            uuid = item.value if item.value != item.label else ""
            records.append(SongSearchSource(item.label, definition.name, uuid, ""))
    return records


def song_titles_from_input_list(context: Any, list_key: str = "song_library") -> list[tuple[str, str, str]]:
    return [(item.name, item.library, item.uuid) for item in song_records_from_input_list(context, list_key)]


def _within_typo_distance(left: str, right: str, max_distance: int) -> bool:
    if left == right:
        return True
    if not left or not right or abs(len(left) - len(right)) > max_distance:
        return False
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_character in enumerate(right, start=1):
            value = min(
                current[right_index - 1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_character != right_character),
            )
            if (
                previous_previous is not None
                and left_index > 1
                and right_index > 1
                and left_character == right[right_index - 2]
                and left[left_index - 2] == right_character
            ):
                value = min(value, previous_previous[right_index - 2] + 1)
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > max_distance:
            return False
        previous_previous, previous = previous, current
    return previous[-1] <= max_distance


def _candidate_mask(
    index: SongSearchIndex,
    query_norm: str,
    query_tokens: tuple[str, ...],
    query_phonetic_tokens: tuple[str, ...],
) -> tuple[int, tuple[int, ...]]:
    if len(query_norm) < 3:
        all_songs = (1 << len(index.songs)) - 1
        return all_songs, (all_songs,)

    token_masks: list[int] = []
    for index_in_query, token in enumerate(query_tokens):
        if len(token) < 3:
            continue
        token_mask = index.lyric_token_postings.get(token, 0)
        phonetic = query_phonetic_tokens[index_in_query] if index_in_query < len(query_phonetic_tokens) else ""
        if len(phonetic) >= 3:
            token_mask |= index.lyric_phonetic_token_postings.get(phonetic, 0)
        for candidate_length, candidates in index.lyric_vocabulary_by_length.items():
            if candidate_length < len(token):
                continue
            for candidate in candidates:
                if token in candidate:
                    token_mask |= index.lyric_token_postings[candidate]
        max_distance = 1 if len(token) <= 6 else 2
        for candidate_length in range(max(3, len(token) - max_distance), len(token) + max_distance + 1):
            for candidate in index.lyric_vocabulary_by_length.get(candidate_length, ()):
                if _within_typo_distance(token, candidate, max_distance):
                    token_mask |= index.lyric_token_postings[candidate]
        token_masks.append(token_mask)

    if not token_masks:
        all_songs = (1 << len(index.songs)) - 1
        return all_songs, ()
    required_tokens = max(1, (2 * len(token_masks) + 2) // 3)
    result = 0
    for song_index in range(len(index.songs)):
        bit = 1 << song_index
        if sum(bool(mask & bit) for mask in token_masks) >= required_tokens:
            result |= bit
    return result, tuple(token_masks)


def _lyric_word_similarity(query_token: str, query_phonetic: str, word: LyricWord) -> float:
    if not query_token:
        return 0.0
    if query_token == word.normalized:
        return 1.0
    if query_token in word.normalized:
        return 0.96
    if len(query_token) < 3:
        return 0.0

    def bigram_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        left_pairs = {left[index : index + 2] for index in range(max(1, len(left) - 1))}
        right_pairs = {right[index : index + 2] for index in range(max(1, len(right) - 1))}
        return (2 * len(left_pairs & right_pairs)) / (len(left_pairs) + len(right_pairs))

    scores = [bigram_similarity(query_token, word.normalized)]
    if len(query_phonetic) >= 3 and len(word.phonetic) >= 3:
        if query_phonetic == word.phonetic:
            scores.append(0.98)
        else:
            scores.append(bigram_similarity(query_phonetic, word.phonetic) * 0.96)
    return max(scores)


def _best_lyric_window(
    query_tokens: tuple[str, ...],
    query_phonetic_tokens: tuple[str, ...],
    song: CompiledSong,
) -> tuple[float, tuple[int, int] | None]:
    if not query_tokens or not song.lyric_words:
        return 0.0, None
    window_size = min(len(query_tokens), len(song.lyric_words))
    if window_size == len(query_tokens):
        for start in range(len(song.lyric_words) - window_size + 1):
            window = song.lyric_words[start : start + window_size]
            if tuple(word.normalized for word in window) == query_tokens:
                return 1.0, (start, start + window_size)
        for start in range(len(song.lyric_words) - window_size + 1):
            window = song.lyric_words[start : start + window_size]
            if all(
                query_token == word.normalized
                or (
                    len(query_phonetic_tokens[index]) >= 3
                    and query_phonetic_tokens[index] == word.phonetic
                )
                for index, (query_token, word) in enumerate(zip(query_tokens, window))
            ):
                return 0.98, (start, start + window_size)
    if len(query_tokens) == 1:
        for index, word in enumerate(song.lyric_words):
            if query_tokens[0] in word.normalized:
                return 0.96, (index, index + 1)
    best_score = 0.0
    best_span: tuple[int, int] | None = None
    for start in range(len(song.lyric_words) - window_size + 1):
        scores = [
            _lyric_word_similarity(
                query_tokens[offset],
                query_phonetic_tokens[offset] if offset < len(query_phonetic_tokens) else "",
                song.lyric_words[start + offset],
            )
            for offset in range(window_size)
        ]
        score = sum(scores) / len(query_tokens)
        if score > best_score:
            best_score = score
            best_span = (start, start + window_size)

    # Queries whose words are spread across a lyric still need a useful excerpt.
    # Fall back to the single closest word when no local phrase matched well.
    best_word_score = 0.0
    best_word_index = -1
    for query_index, query_token in enumerate(query_tokens):
        query_phonetic = query_phonetic_tokens[query_index] if query_index < len(query_phonetic_tokens) else ""
        for word_index, word in enumerate(song.lyric_words):
            score = _lyric_word_similarity(query_token, query_phonetic, word)
            if score > best_word_score:
                best_word_score = score
                best_word_index = word_index
    if best_span is None or (best_score < 0.5 and best_word_score > best_score):
        return best_word_score / len(query_tokens), (
            (best_word_index, best_word_index + 1) if best_word_index >= 0 else None
        )
    return best_score, best_span


def _lyric_preview(song: CompiledSong, span: tuple[int, int] | None, max_chars: int = 180) -> str:
    if span is None or not song.lyric_words or not song.source.lyrics:
        return ""
    match_start, match_end = span
    if match_start < 0 or match_end <= match_start or match_end > len(song.lyric_words):
        return ""
    left = max(0, match_start - 9)
    right = min(len(song.lyric_words), match_end + 9)

    def render(start: int, end: int) -> str:
        raw = song.source.lyrics[song.lyric_words[start].start : song.lyric_words[end - 1].end]
        excerpt = " ".join(raw.split())
        prefix = "… " if start > 0 else ""
        suffix = " …" if end < len(song.lyric_words) else ""
        return f"{prefix}{excerpt}{suffix}"

    preview = render(left, right)
    while len(preview) > max_chars and (left < match_start or right > match_end):
        left_context = song.lyric_words[match_start].start - song.lyric_words[left].start
        right_context = song.lyric_words[right - 1].end - song.lyric_words[match_end - 1].end
        if left < match_start and (right <= match_end or left_context >= right_context):
            left += 1
        elif right > match_end:
            right -= 1
        preview = render(left, right)
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


def _lyric_score(
    query_norm: str,
    query_tokens: tuple[str, ...],
    query_phonetic_tokens: tuple[str, ...],
    song: CompiledSong,
    candidate_coverage: float,
) -> float:
    if not query_norm or not song.lyrics:
        return 0.0
    if query_norm in song.lyrics:
        return 0.82
    if len(query_norm) < 3:
        return 0.0
    if not query_tokens or not song.lyric_tokens:
        return 0.0

    if len(query_tokens) <= len(song.lyric_words):
        for start in range(len(song.lyric_words) - len(query_tokens) + 1):
            window = song.lyric_words[start : start + len(query_tokens)]
            if all(
                query_token == word.normalized
                or (
                    len(query_phonetic_tokens[index]) >= 3
                    and query_phonetic_tokens[index] == word.phonetic
                )
                for index, (query_token, word) in enumerate(zip(query_tokens, window))
            ):
                return 0.79

    token_matches = []
    for index, token in enumerate(query_tokens):
        if token in song.lyric_tokens:
            token_matches.append(1.0)
            continue
        phonetic = query_phonetic_tokens[index] if index < len(query_phonetic_tokens) else ""
        token_matches.append(
            0.96 if len(phonetic) >= 3 and phonetic in song.lyric_phonetic_tokens else 0.0
        )
    coverage = sum(token_matches) / len(query_tokens)
    if coverage >= 1:
        return 0.74

    scores: list[float] = []
    if coverage >= 0.5:
        scores.append(0.38 + (0.32 * coverage))
    if candidate_coverage >= 1:
        scores.append(0.68)
    elif candidate_coverage >= 0.67:
        scores.append(0.5)
    return max(scores, default=0.0)


def search_song_library(context: Any, query: str, list_key: str = "song_library", limit: int = 25) -> list[dict[str, Any]]:
    query = str(query or "").strip()[:MAX_SEARCH_QUERY_CHARS]
    limit = max(1, min(25, int(limit or 25)))
    sources = tuple(song_records_from_input_list(context, list_key))
    if not query:
        matches = [SongSearchMatch(song.name, song.library, 1.0, song.uuid) for song in sources]
        matches.sort(key=lambda item: normalized_text(item.name))
        return [item.to_dict() for item in matches[:limit]]

    index = _compiled_index(sources)
    query_norm = " ".join(normalized_text(query).split()[:MAX_SEARCH_QUERY_TOKENS])
    if not query_norm:
        return []
    query_sorted = " ".join(sorted(query_norm.split()))
    query_phonetic = phonetic_key(query_norm)
    query_token_sequence = tuple(query_norm.split())
    query_tokens = frozenset(query_token_sequence)
    query_phonetic_tokens = tuple(phonetic_key(token) for token in query_token_sequence)
    query_is_number = query_norm.isdigit()
    lyric_candidates, lyric_token_candidate_masks = (
        (0, ())
        if query_is_number
        else _candidate_mask(index, query_norm, query_token_sequence, query_phonetic_tokens)
    )
    matches: list[tuple[SongSearchMatch, CompiledSong]] = []
    for song_index, song in enumerate(index.songs):
        title_score = _title_score(
            query_norm,
            query_sorted,
            query_phonetic,
            query_tokens,
            song.title,
            song.sorted_title,
            song.phonetic_title,
            song.title_tokens,
        )
        song_bit = 1 << song_index
        candidate_coverage = (
            sum(bool(mask & song_bit) for mask in lyric_token_candidate_masks)
            / len(lyric_token_candidate_masks)
            if lyric_token_candidate_masks
            else 0.0
        )
        lyrics_score = (
            _lyric_score(
                query_norm,
                query_token_sequence,
                query_phonetic_tokens,
                song,
                candidate_coverage,
            )
            if lyric_candidates & song_bit
            else 0.0
        )
        lyrics_won = lyrics_score > title_score
        matches.append(
            (
                SongSearchMatch(
                    song.source.name,
                    song.source.library,
                    max(title_score, lyrics_score),
                    song.source.uuid,
                    "lyrics" if lyrics_won else "title",
                ),
                song,
            )
        )
    matches.sort(
        key=lambda pair: (
            -pair[0].score,
            pair[0].match_field == "lyrics",
            normalized_text(pair[0].name),
        )
    )
    results: list[dict[str, Any]] = []
    for match, song in (pair for pair in matches if pair[0].score >= 0.35):
        if len(results) >= limit:
            break
        if match.match_field == "lyrics":
            _window_score, span = _best_lyric_window(query_token_sequence, query_phonetic_tokens, song)
            match = SongSearchMatch(
                match.name,
                match.library,
                match.score,
                match.uuid,
                match.match_field,
                _lyric_preview(song, span),
            )
        results.append(match.to_dict())
    return results
