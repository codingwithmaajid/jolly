"""Retrieval tables: cited chunks with their embedding vectors.

One Chunk per cited unit so every retrieved passage maps straight back to a
citation an advocate can check in the file:
- order sheets: one chunk per dated proceeding entry (or per [Pn] paragraph
  when a sheet holds a single date), with `hearing_date` set;
- pleadings, affidavits, notes: one chunk per paragraph.

`header` is a deterministic context line ("C.C. No. 1042 of 2025 · Order
sheet · 12 Sep 2026 · P2") used as the embedding title and in keyword search.
`char_start`/`char_end` locate the chunk in `Document.text`, so the console
can highlight the exact span a citation points to.

Vectors live in the database as float32 bytes and are searched with NumPy;
at demo scale (hundreds of chunks per case) this is milliseconds. The
Postgres path stores the same vector in a pgvector column (docs/ARCHITECTURE_V2.md).
"""

from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Chunk(SQLModel, table=True):
    id: str = Field(primary_key=True, description="'<doc_id>#<para>', e.g. '1042-OS-2#P2'.")
    doc_id: str = Field(foreign_key="document.id", index=True)
    case_id: str = Field(foreign_key="case.id", index=True)
    para: str | None = None
    seq: int = Field(default=0, description="Position of the chunk inside its document (0-based).")
    doc_kind: str | None = Field(default=None, index=True)
    hearing_date: date | None = Field(default=None, index=True)
    header: str = ""
    char_start: int | None = None
    char_end: int | None = None
    text: str
    trusted: bool = False
    # Text that reads like an instruction to an AI (prompt injection). Kept for
    # the record, never passed to the model or spoken.
    suspicious: bool = False
    embed_model: str
    embed_dim: int = 0
    embedding: bytes
    created_at: datetime = Field(default_factory=_now)
