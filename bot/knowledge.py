"""The owner's knowledge base: a folder of notes and documents the AI can search.

Everything is plain text (.md and .txt) in one folder on the server. The owner
adds a note from Feishu ("记住：..."), or files are copied into the folder.
Searching is simple keyword matching: English words, and pairs of neighbouring
Chinese characters. That is enough for a few hundred pages and needs no extra
service.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

NOTES_FILE = "notes.md"
SUFFIXES = (".md", ".txt")
CHUNK_CHARS = 600
RESULT_CHARS = 3200
_WORD = re.compile(r"[a-z0-9][a-z0-9.%+-]*")
_CJK = re.compile(r"[一-鿿]+")
_NOTE_HEADING = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    source: str  # file name
    heading: str  # the nearest heading above the text, may be empty
    text: str


def tokens(text: str) -> list[str]:
    """English words in lower case, plus every pair of neighbouring Chinese characters."""
    lowered = text.lower()
    result = [word for word in _WORD.findall(lowered) if len(word) > 1]
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            result.append(run)
        result.extend(run[index : index + 2] for index in range(len(run) - 1))
    return result


def split_chunks(source: str, text: str) -> list[Chunk]:
    """Cuts a document into pieces of a few hundred characters, keeping paragraphs whole when possible."""
    chunks: list[Chunk] = []
    heading = ""
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        body = "\n".join(buffer).strip()
        if body:
            chunks.append(Chunk(source, heading, body))
        buffer, size = [], 0

    for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        first_line = paragraph.split("\n", 1)[0]
        if first_line.startswith("#"):
            flush()
            heading = first_line.lstrip("# ").strip()
            paragraph = paragraph[len(first_line) :].strip()
            if not paragraph:
                continue
        while len(paragraph) > CHUNK_CHARS:  # one very long paragraph: cut at sentence ends
            cut = max(paragraph.rfind(mark, 0, CHUNK_CHARS) for mark in "。！？.!?\n")
            cut = cut + 1 if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            flush()
            buffer, size = [paragraph[:cut]], cut
            flush()
            paragraph = paragraph[cut:].strip()
        if size + len(paragraph) > CHUNK_CHARS:
            flush()
        if paragraph:
            buffer.append(paragraph)
            size += len(paragraph)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, folder: str | Path):
        self._folder = Path(folder)
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple[float, int], list[Chunk]]] = {}

    # ----------------------------------------------------------------- files

    def _files(self) -> list[Path]:
        if not self._folder.is_dir():
            return []
        return sorted(
            path
            for path in self._folder.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUFFIXES
            and path.name.lower() != "readme.md"
            and not path.name.startswith(("_", "."))
        )

    def _read(self, path: Path) -> str:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        log.warning("could not read %s as text; skipping it", path.name)
        return ""

    def _chunks(self) -> list[Chunk]:
        result: list[Chunk] = []
        with self._lock:
            alive = set()
            for path in self._files():
                alive.add(path)
                stat = path.stat()
                stamp = (stat.st_mtime, stat.st_size)
                cached = self._cache.get(path)
                if cached is None or cached[0] != stamp:
                    name = path.relative_to(self._folder).as_posix()
                    cached = (stamp, split_chunks(name, self._read(path)))
                    self._cache[path] = cached
                result.extend(cached[1])
            for path in set(self._cache) - alive:
                del self._cache[path]
        return result

    def summary(self) -> list[tuple[str, int]]:
        """(file name, number of characters) for every document."""
        return [(path.relative_to(self._folder).as_posix(), len(self._read(path))) for path in self._files()]

    def note_count(self) -> int:
        path = self._folder / NOTES_FILE
        return len(_NOTE_HEADING.findall(self._read(path))) if path.exists() else 0

    # ----------------------------------------------------------------- notes

    def add_note(self, text: str, when: datetime) -> int:
        """Appends one note. Returns how many notes there are now."""
        text = text.strip()
        if not text:
            raise ValueError("empty note")
        with self._lock:
            self._folder.mkdir(parents=True, exist_ok=True)
            path = self._folder / NOTES_FILE
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"## {when.strftime('%Y-%m-%d %H:%M')} 笔记\n\n{text}\n\n")
        return self.note_count()

    def remove_last_note(self) -> str | None:
        """Deletes the newest note and returns its text, or None when there is none."""
        with self._lock:
            path = self._folder / NOTES_FILE
            if not path.exists():
                return None
            content = self._read(path)
            starts = [match.start() for match in _NOTE_HEADING.finditer(content)]
            if not starts:
                return None
            removed = content[starts[-1] :]
            path.write_text(content[: starts[-1]], encoding="utf-8")
        return removed.split("\n", 1)[1].strip() if "\n" in removed else ""

    # ---------------------------------------------------------------- search

    def search(self, query: str, limit: int = 5) -> list[Chunk]:
        chunks = self._chunks()
        wanted = set(tokens(query))
        if not chunks or not wanted:
            return []
        counted = []
        document_frequency: dict[str, int] = {}
        for chunk in chunks:
            counts: dict[str, int] = {}
            for token in tokens(f"{chunk.heading} {chunk.text}"):
                if token in wanted:
                    counts[token] = counts.get(token, 0) + 1
            counted.append(counts)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        scored = []
        for index, counts in enumerate(counted):
            score = sum(
                math.log(1 + len(chunks) / document_frequency[token]) * count / (count + 1.2) for token, count in counts.items()
            )
            if score > 0:
                scored.append((score, -index))
        scored.sort(reverse=True)
        return [chunks[-negative_index] for _, negative_index in scored[:limit]]

    def search_text(self, query: str) -> str:
        """What the AI gets back from the search tool."""
        files = [name for name, _ in self.summary()]
        if not files:
            return "资料库现在是空的，交易员还没有放进任何资料。"
        found = self.search(query)
        if not found:
            return f"资料库里没有找到和“{query}”相关的内容。现有的资料文件：{'、'.join(files)}。"
        blocks, used = [], 0
        for chunk in found:
            label = chunk.source + (f" · {chunk.heading}" if chunk.heading else "")
            block = f"【出处：{label}】\n{chunk.text}"
            if blocks and used + len(block) > RESULT_CHARS:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n".join(blocks)
