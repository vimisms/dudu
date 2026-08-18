"""Splits knowledge-base files into retrieval-sized chunks with metadata
(source path, ICM number, doc type, section title) attached, so search
results are traceable back to a specific document and section -- not just an
anonymous blob of text.

Chunking strategy: split on Markdown headers first (this repo's docs are
consistently structured -- "## 1. Plain-English problem statement", "##
Mitigation", etc. -- so header-aligned chunks stay topically coherent), then
further split any section that's still too large for a good embedding into
overlapping sub-chunks. Non-Markdown / headerless files fall back straight to
size-based chunking.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

HEADER_RE = re.compile(r"^(#{1,4})\s+(.*)$")
# Your repo uses two naming conventions -- ICM21000001115144_... and
# ICM-21000001136627-... . The original pattern required the digits to follow
# "ICM" immediately, so every dash-separated file silently lost its icm_number
# metadata: searchable by text, but never citable by ICM number and invisible to
# an ICM-number filter. Allow a dash/underscore/space separator.
ICM_RE = re.compile(r"ICM[-_\s]?0*([0-9]{6,})", re.IGNORECASE)

MAX_CHUNK_CHARS = 2000
CHUNK_OVERLAP = 200
SEARCHABLE_EXTENSIONS = {".md", ".txt", ".sql", ".kql"}


@dataclass
class Chunk:
    id: str
    embed_text: str      # what gets embedded -- includes a small context header
    display_text: str    # the raw section text, for showing back to the agent
    metadata: dict = field(default_factory=dict)


def classify_doc_type(rel_path: Path) -> str:
    parts = rel_path.parts
    if parts[0] == "docs" and len(parts) > 1:
        return parts[1]  # icm-investigations, tsgs, sql-reference, kusto, ...
    if parts[0] == "icm-health":
        return "icm-health"
    if parts[0] == "_private":
        return "private-index"
    return "other"


def _extract_icm_number(rel_path: Path, head_text: str) -> str | None:
    m = ICM_RE.search(rel_path.name) or ICM_RE.search(head_text[:2000])
    return m.group(1) if m else None


def _extract_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            return m.group(2).strip()
    return fallback


def _split_by_headers(text: str) -> list[tuple[str, str]]:
    """Returns [(section_header_title, section_text), ...]. Content before
    the first header (if any) is returned with an empty header title."""
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    header = ""
    buf: list[str] = []
    for line in lines:
        m = HEADER_RE.match(line)
        if m:
            if buf:
                sections.append((header, "\n".join(buf).strip()))
            header = m.group(2).strip()
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append((header, "\n".join(buf).strip()))
    return [(h, c) for h, c in sections if c.strip()]


def _split_large(text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        parts.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return parts


def chunk_file(path: Path, kb_root: Path) -> list[Chunk]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if not text.strip():
        return []

    rel_path = path.relative_to(kb_root)
    rel_str = str(rel_path).replace("\\", "/")
    doc_type = classify_doc_type(rel_path)
    icm_number = _extract_icm_number(rel_path, text)
    doc_title = _extract_title(text, fallback=path.stem)

    sections = _split_by_headers(text) if path.suffix.lower() in {".md"} else [("", text)]
    if not sections:
        sections = [("", text)]

    chunks: list[Chunk] = []
    for sec_idx, (section_title, section_text) in enumerate(sections):
        for sub_idx, piece in enumerate(_split_large(section_text)):
            chunk_id = hashlib.sha1(f"{rel_str}::{sec_idx}::{sub_idx}".encode()).hexdigest()[:20]
            context_header = f"Document: {doc_title}\n"
            if icm_number:
                context_header += f"ICM: {icm_number}\n"
            context_header += f"Type: {doc_type}\n"
            if section_title:
                context_header += f"Section: {section_title}\n"
            embed_text = f"{context_header}\n{piece}".strip()

            chunks.append(
                Chunk(
                    id=chunk_id,
                    embed_text=embed_text,
                    display_text=piece,
                    metadata={
                        "source": rel_str,
                        "doc_title": doc_title,
                        "doc_type": doc_type,
                        "icm_number": icm_number or "",
                        "section_title": section_title,
                        "chunk_index": f"{sec_idx}.{sub_idx}",
                    },
                )
            )
    return chunks


def iter_searchable_files(kb_root: Path, skip_dirs: set[str], max_bytes: int):
    import os

    for root, dirs, files in os.walk(kb_root):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() in SEARCHABLE_EXTENSIONS:
                try:
                    if p.stat().st_size <= max_bytes:
                        yield p
                except OSError:
                    continue
