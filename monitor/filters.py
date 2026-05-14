"""Keyword filtering for Monitor documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


MATCH_MODES = ("literal", "word", "regex")


@dataclass
class Topic:
    name: str
    keywords: list[str]
    match_mode: str = "word"
    id: int | None = None
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.match_mode not in MATCH_MODES:
            raise ValueError(
                f"Invalid match_mode {self.match_mode!r}; expected one of "
                f"{MATCH_MODES}"
            )
        self._compiled = [self._compile(k) for k in self.keywords]

    def _compile(self, keyword: str) -> re.Pattern:
        if self.match_mode == "regex":
            return re.compile(keyword, re.IGNORECASE | re.DOTALL)
        if self.match_mode == "word":
            return re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
        return re.compile(re.escape(keyword), re.IGNORECASE)

    def match(self, text: str) -> list[str]:
        if not text:
            return []
        hits = []
        for kw, pattern in zip(self.keywords, self._compiled):
            if pattern.search(text):
                hits.append(kw)
        return hits


@dataclass
class MatchResult:
    topic_id: int | None
    topic_name: str
    keywords: list[str]


def match_topics(text: str, topics: Iterable[Topic]) -> list[MatchResult]:
    results = []
    for topic in topics:
        hits = topic.match(text)
        if hits:
            results.append(MatchResult(
                topic_id=topic.id,
                topic_name=topic.name,
                keywords=hits,
            ))
    return results
