"""Spec library: the source of truth the agent tests a product against.

The navigator's whole claim to credibility is `expected_source` — a finding
without a traceable expectation is an observation, not a bug. Until now the
only sources available were the charter, WCAG and HTTP semantics. This gives
the agent the product's own written spec.

Specs are NOT pasted into the prompt. A requirements document is tens of
thousands of tokens and this agent already runs long single invocations; an
inlined spec would either blow the window or be silently truncated by
context_guard mid-run, leaving the agent testing against a document it can no
longer see. Instead the model works the way a human tester does:

    list_specs()                  what documents exist
    search_specs("password")      which lines mention the thing I am testing
    read_spec("auth.md", 400)     read just that part

Extracted text is cached next to the source so extraction happens once.

Layout (override the root with QA_SPEC_DIR):

    ~/.react_qa_specs/
        index.json          name -> {source, type, chars, added}
        text/<name>.txt     extracted plain text, what the tools read
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

SPEC_DIR = Path(os.environ.get("QA_SPEC_DIR", Path.home() / ".react_qa_specs"))
TEXT_DIR = SPEC_DIR / "text"
INDEX = SPEC_DIR / "index.json"

# Formats needing a dedicated extractor.
BINARY_SUFFIXES = {".pdf", ".docx"}

# Named only so the common ones get a fast, clear rejection. Everything else
# is decided by sniffing the bytes, NOT by a suffix allowlist: specs arrive as
# .feature (Gherkin), .story, .spec, .toml, .proto, .graphql and a dozen other
# extensions nobody can enumerate up front, and silently refusing a real spec
# because of its file extension is the worst failure this module can have.
MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".webm", ".ogg",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".deb", ".rpm", ".dmg",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".iso", ".img", ".class", ".jar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pyc", ".whl", ".sqlite", ".db",
    ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".key", ".pem", ".p12",
}

# Kept for callers/tests that want the well-known text kinds by name.
TEXT_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html", ".htm", ".rst",
    ".adoc", ".feature", ".story", ".spec", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".graphql", ".gql", ".proto", ".sql", ".tsv", ".log", ".text",
}
SUPPORTED = TEXT_SUFFIXES | BINARY_SUFFIXES

PROBE_BYTES = 16384


def looks_like_text(path: Path) -> bool:
    """Decide by content, not by extension.

    A spec is readable if its opening bytes decode as UTF-8 and contain no NUL
    and few control characters. This lets any plain-text spec format through,
    including ones that did not exist when this was written.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(PROBE_BYTES)
    except Exception:
        return False
    if not raw:
        return False
    if b"\x00" in raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character straddling the probe boundary is not a failure.
        try:
            text = raw[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return False
    printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    return printable / max(len(text), 1) > 0.85


def is_supported(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in BINARY_SUFFIXES:
        return True
    if suffix in MEDIA_SUFFIXES:
        return False
    return looks_like_text(path)

MAX_FILE_BYTES = 25 * 1024 * 1024
READ_CHUNK_DEFAULT = 6000
READ_CHUNK_MAX = 20000


# ---------------------------------------------------------------- extraction

def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            text = f"[page {i}: extraction failed: {exc}]"
        out.append(f"\n--- page {i} ---\n{text}")
    return "\n".join(out)


def _extract_docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    out = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name if para.style is not None else "") or ""
        # Keep heading structure: it is what makes a spec citable by section.
        out.append(f"\n## {text}" if style.startswith("Heading") else text)
    for t_i, table in enumerate(doc.tables, 1):
        out.append(f"\n[table {t_i}]")
        for row in table.rows:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out)


def _extract_html(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = re.sub(r"&nbsp;?", " ", raw)
    return re.sub(r"[ \t]{2,}", " ", raw)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix in (".html", ".htm"):
        return _extract_html(path)
    return path.read_text(encoding="utf-8", errors="ignore")


# -------------------------------------------------------------------- store

def _load_index() -> dict:
    if not INDEX.is_file():
        return {}
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_index(index: dict) -> None:
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")


def _safe_name(name: str) -> str:
    """A citable, filesystem-safe id. Collisions get a numeric suffix later."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "spec"


def add_spec(source: Path, display_name: str = "") -> dict:
    """Extract one file into the library. Returns a record or {'error': ...}."""
    source = Path(source)
    if not source.is_file():
        return {"error": f"not a file: {source}"}
    if not is_supported(source):
        kind = source.suffix.lower()
        why = ("binary/media file" if kind in MEDIA_SUFFIXES
               else f"not readable as text ({kind or 'no extension'})")
        return {"error": f"{why}: {source.name}"}
    if source.stat().st_size > MAX_FILE_BYTES:
        return {"error": f"too large ({source.stat().st_size // 1024 // 1024} MB): {source.name}"}

    try:
        text = extract_text(source)
    except Exception as exc:
        return {"error": f"{source.name}: {type(exc).__name__}: {exc}"}

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return {"error": f"no extractable text (scanned image or empty?): {source.name}"}

    index = _load_index()
    name = _safe_name(display_name or source.name)
    if name in index and index[name].get("source") != str(source.resolve()):
        stem, dot, ext = name.rpartition(".")
        n = 2
        while f"{stem}-{n}{dot}{ext}" in index:
            n += 1
        name = f"{stem}-{n}{dot}{ext}"

    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    (TEXT_DIR / (name + ".txt")).write_text(text, encoding="utf-8")

    record = {
        "name": name,
        "source": str(source.resolve()),
        "type": source.suffix.lower().lstrip("."),
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    index[name] = record
    _save_index(index)
    return record


def import_folder(folder: str, recursive: bool = True) -> dict:
    """Index every supported document under a folder. Reports skips explicitly."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return {"error": f"not a folder: {root}", "added": [], "skipped": []}
    paths = sorted(root.rglob("*") if recursive else root.glob("*"))
    added, skipped = [], []
    for p in paths:
        if not p.is_file() or p.name.startswith("."):
            continue
        if not is_supported(p):
            kind = p.suffix.lower()
            skipped.append({"file": p.name, "reason": (
                "binary/media file" if kind in MEDIA_SUFFIXES
                else f"not readable as text ({kind or 'no extension'})")})
            continue
        rec = add_spec(p)
        (skipped if "error" in rec else added).append(
            {"file": p.name, "reason": rec["error"]} if "error" in rec else rec
        )
    return {"root": str(root), "added": added, "skipped": skipped}


def remove_spec(name: str) -> bool:
    index = _load_index()
    if name not in index:
        return False
    index.pop(name)
    _save_index(index)
    (TEXT_DIR / (name + ".txt")).unlink(missing_ok=True)
    return True


def all_specs() -> list:
    return sorted(_load_index().values(), key=lambda r: r.get("name", ""))


def _text_of(name: str) -> str | None:
    path = TEXT_DIR / (name + ".txt")
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="ignore")


# ------------------------------------------------------------- agent tools

def list_specs() -> str:
    """List the specification documents available for this product.

    Call this FIRST when the user says the product has a spec. Returns each
    document's name, size and opening lines so you can decide what to read.
    Names returned here are what you pass to read_spec and cite in
    expected_source.
    """
    specs = all_specs()
    if not specs:
        return ("No specification documents have been added. Test against the "
                "charter, HTTP semantics and WCAG as usual.")
    out = [f"{len(specs)} specification document(s) available:"]
    for rec in specs:
        name = rec["name"]
        text = _text_of(name) or ""
        head = " / ".join(line.strip() for line in text.split("\n")[:6] if line.strip())[:200]
        out.append(
            f"\n- {name}  ({rec['type']}, {rec['chars']} chars, {rec.get('lines', '?')} lines)"
            f"\n    opens with: {head}"
        )
    out.append("\nUse search_specs(query) to find a rule, then read_spec(name, offset) "
               "to read that part. Cite findings as expected_source='spec:<name>:<line>'.")
    return "\n".join(out)


def search_specs(query: str, max_hits: int = 12) -> str:
    """Search every specification document for a term and return matching lines.

    This is the fastest way to find the rule that governs whatever you are
    testing (e.g. 'password', 'timeout', 'required', 'admin'). Each hit shows
    the document name and line number so you can cite it directly in
    expected_source, and read_spec() around it for the full context.

    Args:
        query: Word or phrase to look for. Case-insensitive.
        max_hits: Maximum number of matching lines to return.
    """
    if not query or not query.strip():
        return "ERROR: query is empty."
    specs = all_specs()
    if not specs:
        return "No specification documents have been added."
    try:
        pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    except re.error as exc:
        return f"ERROR: bad query: {exc}"

    hits, truncated = [], False
    for rec in specs:
        name = rec["name"]
        text = _text_of(name)
        if text is None:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if pattern.search(line):
                if len(hits) >= max_hits:
                    truncated = True
                    break
                hits.append(f"{name}:{i}: {line.strip()[:300]}")
        if truncated:
            break
    if not hits:
        return (f"No spec line matches '{query}'. Try a broader term, or "
                "list_specs() to see what the documents cover.")
    tail = "\n(more matches exist; narrow the query)" if truncated else ""
    return f"{len(hits)} match(es) for '{query}':\n" + "\n".join(hits) + tail


def read_spec(name: str, offset: int = 0, max_chars: int = READ_CHUNK_DEFAULT) -> str:
    """Read part of one specification document.

    Read only what you need: specs are long and the session has a context
    budget. Use search_specs() to locate the rule, then read around it.

    Args:
        name: Document name exactly as returned by list_specs().
        offset: Character offset to start from (0 = beginning).
        max_chars: How much to read, capped at 20000.
    """
    text = _text_of(_safe_name(name))
    if text is None:
        available = ", ".join(r["name"] for r in all_specs()) or "(none)"
        return f"ERROR: no spec named '{name}'. Available: {available}"
    max_chars = max(500, min(int(max_chars or READ_CHUNK_DEFAULT), READ_CHUNK_MAX))
    offset = max(0, int(offset or 0))
    chunk = text[offset:offset + max_chars]
    if not chunk:
        return f"(end of {name}; it is {len(text)} chars)"
    start_line = text.count("\n", 0, offset) + 1
    more = offset + len(chunk)
    # Number every line. Without this the model has to count lines itself to
    # cite expected_source, and it miscounts - which silently corrupts the
    # traceability that is the whole point of citing a spec.
    numbered = "\n".join(
        f"{start_line + i:>4} | {line}" for i, line in enumerate(chunk.split("\n"))
    )
    footer = (f"\n\n[{name}: chars {offset}-{more} of {len(text)}. "
              f"Continue with read_spec('{name}', {more}).]"
              if more < len(text) else f"\n\n[end of {name}]")
    return (f"[{name}, lines {start_line}+; cite as spec:{name}:<line number "
            f"shown at the left>]\n{numbered}{footer}")
