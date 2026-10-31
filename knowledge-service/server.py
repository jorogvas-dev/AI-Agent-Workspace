import math
import os
import re
import json
import csv
import time
import hashlib
import logging
import threading
import secrets

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests
import chromadb

from pypdf import PdfReader
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_ROOT = Path("/workspace/input").resolve()
KNOWLEDGE_ROOT = Path("/workspace/knowledge").resolve()

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))

LM_STUDIO_URL = os.getenv(
    "LM_STUDIO_URL",
    "http://host.docker.internal:1234/v1/embeddings"
)

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-nomic-embed-text-v1.5@q8_0"
)

SERVICE_TOKEN = os.getenv("KNOWLEDGE_TOKEN", "")

WATCHER_ENABLED = os.getenv(
    "WATCHER_ENABLED", "true"
).lower() == "true"

WATCH_INTERVAL_SECONDS = max(
    15,
    int(os.getenv("WATCH_INTERVAL_SECONDS", "60"))
)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_CHARS = 500_000
MAX_QUERY_CHARS = 4_000

MAX_PDF_PAGES = 100
MAX_DOCX_PARAGRAPHS = 20_000
MAX_DOCX_TABLES = 1_000
MAX_DOCX_TABLE_ROWS = 100_000
MAX_XLSX_SHEETS = 100
MAX_XLSX_ROWS_PER_SHEET = 10_000
MAX_PPTX_SLIDES = 200
MAX_PPTX_SHAPES_PER_SLIDE = 5_000
MAX_CSV_ROWS = 100_000

MAX_REQUEST_BODY = 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10
MAX_CONCURRENT_REQUESTS = 16

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200

MAX_SEARCH_RESULTS = 10
MAX_DOCUMENT_SOURCE_BYTES = 512
MAX_DOCUMENT_PAGE = 50
MAX_DOCUMENT_OFFSET = 10_000
DOCUMENT_UNSET = object()
EMBEDDING_DIMENSIONS = 768

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".csv",
    ".txt",
    ".md"
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("knowledge-service")


# ============================================================
# VALIDATION
# ============================================================

if not SERVICE_TOKEN:
    raise RuntimeError(
        "KNOWLEDGE_TOKEN environment variable is missing."
    )


# ============================================================
# CHROMADB CONNECTION
# ============================================================

# ============================================================
# CHROMADB CONNECTION
# ============================================================

chroma_client = None
knowledge_collection = None
file_state_collection = None


def initialize_chromadb(max_attempts=12, retry_delay=5):
    """
    Initialize ChromaDB with retries.

    Retries temporary connection failures instead of
    terminating the service immediately.
    """

    global chroma_client
    global knowledge_collection
    global file_state_collection

    last_error = None

    for attempt in range(1, max_attempts + 1):

        try:
            client = chromadb.HttpClient(
                host=CHROMA_HOST,
                port=CHROMA_PORT
            )

            knowledge = client.get_or_create_collection(
                name="agent_knowledge",
                metadata={"hnsw:space": "cosine"}
            )

            file_state = client.get_or_create_collection(
                name="agent_file_state",
                metadata={"hnsw:space": "cosine"}
            )

            # Verify both collections are accessible.
            knowledge.count()
            file_state.count()

            # Publish references only after successful initialization.
            chroma_client = client
            knowledge_collection = knowledge
            file_state_collection = file_state

            logger.info(
                "Connected to ChromaDB. Attempt %s/%s.",
                attempt,
                max_attempts
            )

            return

        except Exception as error:

            last_error = error

            logger.warning(
                "ChromaDB connection attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                error
            )

            if attempt < max_attempts:
                time.sleep(retry_delay)

    raise RuntimeError(
        f"Could not connect to ChromaDB after "
        f"{max_attempts} attempts."
    ) from last_error


# This collection stores file hashes and indexing metadata.
# It is NOT used for semantic searches.
#
# Dummy vectors are used because ChromaDB requires embeddings
# when no embedding function is configured.


# ============================================================
# SHARED LOCKS
# ============================================================

# Prevent concurrent indexing, deletion and watcher operations
# from modifying ChromaDB at the same time.

INDEX_LOCK = threading.RLock()


# ============================================================
# AUTHENTICATION
# ============================================================

def authorized(headers):
    authorization = headers.get("Authorization", "")

    if not authorization.startswith("Bearer "):
        return False

    supplied_token = authorization[7:].strip()

    return secrets.compare_digest(
        supplied_token,
        SERVICE_TOKEN
    )


# ============================================================
# FILE IDENTITY AND HASHING
# ============================================================

def file_identity(root_name, relative_path):
    """
    Create a stable ID for a file based on its root and path.
    """

    identity = f"{root_name}:{relative_path}"

    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


def calculate_file_hash(path):
    """
    Calculate a SHA-256 hash without loading the entire file
    into memory.
    """

    digest = hashlib.sha256()

    with open(path, "rb") as file:
        while True:
            block = file.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


# ============================================================
# FILE STATE STORAGE
# ============================================================

def get_file_state(root_name, relative_path):
    """
    Retrieve the previously indexed hash for a file.
    """

    file_id = file_identity(root_name, relative_path)

    result = file_state_collection.get(
        ids=[file_id],
        include=["metadatas"]
    )

    if not result["ids"]:
        return None

    metadata = result["metadatas"][0]

    return metadata.get("sha256")


def save_file_state(root_name, relative_path, file_hash):
    """
    Save the latest successful indexing state.
    """

    file_id = file_identity(root_name, relative_path)

    file_state_collection.upsert(
        ids=[file_id],
        embeddings=[[0.0] * EMBEDDING_DIMENSIONS],
        documents=[f"{root_name}:{relative_path}"],
        metadatas=[{
            "root": root_name,
            "source": relative_path,
            "sha256": file_hash,
            "updated_at": int(time.time())
        }]
    )





# ============================================================
# ROOT HELPERS
# ============================================================

def get_roots():
    return {
        "input": INPUT_ROOT,
        "knowledge": KNOWLEDGE_ROOT
    }


def get_root_name(root_path):
    if root_path == INPUT_ROOT:
        return "input"

    if root_path == KNOWLEDGE_ROOT:
        return "knowledge"

    raise ValueError("Unknown document root.")

# ============================================================
# FILE RESOLUTION AND VALIDATION
# ============================================================

def resolve_document(relative_path):
    """
    Resolve a document path safely.

    Supports:
      input/example.pdf
      knowledge/example.pdf

    Also supports an unprefixed relative path when it
    exists in exactly one allowed root.
    """

    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("A valid relative file path is required.")

    relative_path = relative_path.strip().replace("\\", "/")

    root_name = None

    if relative_path.startswith("input/"):
        root_name = "input"
        relative_path = relative_path[len("input/"):]

    elif relative_path.startswith("knowledge/"):
        root_name = "knowledge"
        relative_path = relative_path[len("knowledge/"):]

    candidate_path = Path(relative_path)

    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise ValueError("Invalid document path.")

    if candidate_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("Unsupported document format.")

    roots = get_roots()

    selected_roots = (
        {root_name: roots[root_name]}
        if root_name
        else roots
    )

    matches = []

    for name, root in selected_roots.items():

        unresolved_candidate = root / candidate_path

        # Reject symlinks in any component of the requested path.
        current_path = root

        has_symlink = False

        for part in candidate_path.parts:
            current_path = current_path / part

            if current_path.is_symlink():
                has_symlink = True
                break

        if has_symlink:
            continue

        candidate = unresolved_candidate.resolve()

        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue

        if candidate.is_file():
            matches.append((name, root, candidate))

    if not matches:
        raise FileNotFoundError(
            "Document not found in the selected directory."
        )

    if len(matches) > 1:
        raise ValueError(
            "Ambiguous path. Use input/ or knowledge/ prefix."
        )

    name, root, resolved = matches[0]

    if resolved.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("File exceeds the 20 MB size limit.")

    return name, root, resolved


# ============================================================
# DOCUMENT EXTRACTION
# ============================================================

def extract_document(path):
    """
    Extract readable text from supported document formats.

    Returns a list of text sections with source locations.
    """

    extension = path.suffix.lower()

    sections = []

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    if extension == ".pdf":

        reader = PdfReader(str(path))

        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(
                f"PDF exceeds the {MAX_PDF_PAGES}-page processing limit."
            )

        for page_number, page in enumerate(reader.pages, start=1):

            text = page.extract_text() or ""

            if text.strip():
                sections.append({
                    "location": f"Page {page_number}",
                    "text": text
                })

    # --------------------------------------------------------
    # WORD
    # --------------------------------------------------------

    elif extension == ".docx":

        document = Document(str(path))

        if len(document.paragraphs) > MAX_DOCX_PARAGRAPHS:
            raise ValueError("DOCX exceeds the paragraph processing limit.")

        if len(document.tables) > MAX_DOCX_TABLES:
            raise ValueError("DOCX exceeds the table processing limit.")

        for index, paragraph in enumerate(
            document.paragraphs,
            start=1
        ):

            text = paragraph.text.strip()

            if text:
                sections.append({
                    "location": f"Paragraph {index}",
                    "text": text
                })

        for table_number, table in enumerate(
            document.tables,
            start=1
        ):

            if len(table.rows) > MAX_DOCX_TABLE_ROWS:
                raise ValueError("DOCX exceeds the table row processing limit.")

            for row_number, row in enumerate(
                table.rows,
                start=1
            ):

                values = [
                    cell.text.strip()
                    for cell in row.cells
                ]

                row_text = " | ".join(
                    value for value in values if value
                )

                if row_text:
                    sections.append({
                        "location": (
                            f"Table {table_number}, "
                            f"Row {row_number}"
                        ),
                        "text": row_text
                    })

    # --------------------------------------------------------
    # EXCEL
    # --------------------------------------------------------

    elif extension == ".xlsx":

        workbook = load_workbook(
            filename=str(path),
            read_only=True,
            data_only=True
        )

        try:
            if len(workbook.worksheets) > MAX_XLSX_SHEETS:
                raise ValueError("XLSX exceeds the worksheet processing limit.")

            for worksheet in workbook.worksheets:

                for row_number, row in enumerate(
                    worksheet.iter_rows(values_only=True),
                    start=1
                ):

                    if row_number > MAX_XLSX_ROWS_PER_SHEET:
                        raise ValueError(
                            "XLSX exceeds the row processing limit."
                        )

                    values = [
                        str(value)
                        for value in row
                        if value is not None
                    ]

                    row_text = " | ".join(values)

                    if row_text.strip():
                        sections.append({
                            "location": (
                                f"Sheet {worksheet.title}, "
                                f"Row {row_number}"
                            ),
                            "text": row_text
                        })

        finally:
            workbook.close()

    # --------------------------------------------------------
    # POWERPOINT
    # --------------------------------------------------------

    elif extension == ".pptx":

        presentation = Presentation(str(path))

        if len(presentation.slides) > MAX_PPTX_SLIDES:
            raise ValueError(
                f"PPTX exceeds the {MAX_PPTX_SLIDES}-slide processing limit."
            )

        for slide_number, slide in enumerate(
            presentation.slides,
            start=1
        ):

            slide_text = []

            if len(slide.shapes) > MAX_PPTX_SHAPES_PER_SLIDE:
                raise ValueError(
                    "PPTX exceeds the per-slide shape processing limit."
                )

            for shape in slide.shapes:

                if shape.has_text_frame:
                    text = shape.text.strip()

                    if text:
                        slide_text.append(text)

                if shape.has_table:

                    for row in shape.table.rows:

                        values = [
                            cell.text.strip()
                            for cell in row.cells
                        ]

                        row_text = " | ".join(
                            value for value in values if value
                        )

                        if row_text:
                            slide_text.append(row_text)

            combined_text = "\n".join(slide_text)

            if combined_text.strip():
                sections.append({
                    "location": f"Slide {slide_number}",
                    "text": combined_text
                })

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    elif extension == ".csv":

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            newline=""
        ) as file:

            reader = csv.reader(file)

            for row_number, row in enumerate(
                reader,
                start=1
            ):

                if row_number > MAX_CSV_ROWS:
                    raise ValueError("CSV exceeds the row processing limit.")

                row_text = " | ".join(
                    value.strip()
                    for value in row
                    if value.strip()
                )

                if row_text:
                    sections.append({
                        "location": f"Row {row_number}",
                        "text": row_text
                    })

    # --------------------------------------------------------
    # TXT AND MARKDOWN
    # --------------------------------------------------------

    elif extension in {".txt", ".md"}:

        text = path.read_text(
            encoding="utf-8-sig",
            errors="replace"
        )

        if text.strip():
            sections.append({
                "location": "Text content",
                "text": text
            })

    else:
        raise ValueError("Unsupported document format.")

    # --------------------------------------------------------
    # OUTPUT SIZE LIMIT
    # --------------------------------------------------------

    total_chars = sum(
        len(section["text"])
        for section in sections
    )

    if total_chars > MAX_CHARS:
        raise ValueError(
            f"Extracted document exceeds the "
            f"{MAX_CHARS:,} character limit."
        )

    return sections

# ============================================================
# TEXT CHUNKING
# ============================================================

def chunk_text(text):
    """
    Split text into overlapping chunks.

    Overlap helps preserve context when a relevant passage
    crosses a chunk boundary.
    """

    if not text:
        return []

    chunks = []

    start = 0
    text_length = len(text)

    while start < text_length:

        end = min(start + CHUNK_SIZE, text_length)

        # Prefer splitting at a word boundary.
        if end < text_length:
            boundary = text.rfind(" ", start, end)

            if boundary > start + CHUNK_SIZE // 2:
                end = boundary

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        start = max(0, end - CHUNK_OVERLAP)

    return chunks


# ============================================================
# DOCUMENT SECTION CHUNKING
# ============================================================

def prepare_document_chunks(sections):
    """
    Convert extracted sections into searchable chunks.

    Every chunk retains its original document location.
    """

    prepared = []

    for section in sections:

        location = section["location"]
        text = section["text"]

        for chunk_number, chunk in enumerate(
            chunk_text(text),
            start=1
        ):

            prepared.append({
                "location": location,
                "chunk_number": chunk_number,
                "text": chunk
            })

    return prepared


# ============================================================
# EMBEDDING GENERATION
# ============================================================

def embed_texts(texts):
    """
    Generate embeddings using the local LM Studio API.
    """

    if not texts:
        return []

    embeddings = []

    # Process in batches to avoid oversized requests.
    batch_size = 32

    for batch_start in range(0, len(texts), batch_size):

        batch = texts[
            batch_start:batch_start + batch_size
        ]

        response = requests.post(
            LM_STUDIO_URL,
            json={
                "model": EMBEDDING_MODEL,
                "input": batch
            },
            timeout=120
        )

        response.raise_for_status()

        result = response.json()

        data = result.get("data", [])

        if len(data) != len(batch):
            raise RuntimeError(
                "Embedding API returned an unexpected "
                "number of embeddings."
            )

        expected_indices = set(range(len(batch)))
        received_indices = [item.get("index") for item in data]

        if (
            any(
                isinstance(index, bool) or not isinstance(index, int)
                for index in received_indices
            )
            or set(received_indices) != expected_indices
            or len(set(received_indices)) != len(received_indices)
        ):
            raise RuntimeError(
                "Embedding API returned invalid embedding indices."
            )

        data.sort(key=lambda item: item["index"])

        for item in data:

            embedding = item.get("embedding")

            if not isinstance(embedding, list):
                raise RuntimeError(
                    "Embedding API returned invalid embedding data."
                )

            if len(embedding) != EMBEDDING_DIMENSIONS:
                raise RuntimeError(
                    f"Expected {EMBEDDING_DIMENSIONS} embedding "
                    f"dimensions, received {len(embedding)}."
                )

            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in embedding
            ):
                raise RuntimeError(
                    "Embedding API returned non-numeric or "
                    "non-finite embedding values."
                )

            embeddings.append(embedding)
    return embeddings


# ============================================================
# CHUNK IDENTIFIERS
# ============================================================

def create_chunk_id(
    root_name,
    relative_path,
    location,
    chunk_number,
    text
):
    """
    Generate a deterministic ID for each document chunk.
    """

    identity = (
        f"{root_name}|{relative_path}|"
        f"{location}|{chunk_number}|{text}"
    )

    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


# ============================================================
# DOCUMENT INDEXING
# ============================================================


def index_document(relative_path, force=False):
    """
    Index a document using its explicitly selected root.

    Unchanged documents are skipped.
    Existing chunks are replaced only after successful
    extraction and embedding.
    """

    root_name, root_path, file_path = resolve_document(relative_path)

    relative_source = file_path.relative_to(
        root_path
    ).as_posix()

    with INDEX_LOCK:
        initial_hash = calculate_file_hash(file_path)

        previous_hash = get_file_state(
            root_name,
            relative_source
        )

        if not force and previous_hash == initial_hash:
            return {
                "status": "unchanged",
                "source": relative_source,
                "folder": root_name,
                "chunks_indexed": 0
            }

    # Extraction and remote embedding do not need to block searches or
    # other database writers. The final hash check protects against a
    # document changing while this work is in progress.
    sections = extract_document(file_path)

    prepared_chunks = prepare_document_chunks(sections)

    if not prepared_chunks:
        raise ValueError(
            "Document contains no extractable text."
        )

    texts = [
        item["text"]
        for item in prepared_chunks
    ]

    embeddings = embed_texts(texts)

    final_hash = calculate_file_hash(file_path)

    if initial_hash != final_hash:
        raise RuntimeError(
            "Document changed during indexing."
        )

    with INDEX_LOCK:

        ids = []
        documents = []
        metadatas = []

        for item in prepared_chunks:

            chunk_id = create_chunk_id(
                root_name,
                relative_source,
                item["location"],
                item["chunk_number"],
                item["text"]
            )

            ids.append(chunk_id)
            documents.append(item["text"])

            metadatas.append({
                "root": root_name,
                "source": relative_source,
                "location": item["location"],
                "chunk_number": item["chunk_number"],
                "indexed_at": int(time.time())
            })

        # Store the new chunks first.
        knowledge_collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas
        )

        # Verify that the source file did not change
        # while the new chunks were being written.
        post_write_hash = calculate_file_hash(file_path)

        if final_hash != post_write_hash:
            raise RuntimeError(
                "Document changed during database indexing. "
                "File state was not updated; the next watcher "
                "cycle will retry."
            )

        # Find and remove outdated chunks.
        existing = knowledge_collection.get(
            where={
                "$and": [
                    {"root": root_name},
                    {"source": relative_source}
                ]
            },
            include=["metadatas"]
        )

        current_ids = set(ids)

        obsolete_ids = [
            chunk_id
            for chunk_id in existing["ids"]
            if chunk_id not in current_ids
        ]

        if obsolete_ids:
            knowledge_collection.delete(ids=obsolete_ids)

        # Save state only after the new chunks are stored
        # and obsolete chunks have been removed.
        save_file_state(
            root_name,
            relative_source,
            final_hash
        )

        logger.info(
            "Indexed %s/%s: %s chunks",
            root_name,
            relative_source,
            len(ids)
        )

        return {
            "status": "indexed",
            "source": relative_source,
            "folder": root_name,
            "chunks_indexed": len(ids)
        }


# ============================================================
# SEMANTIC KNOWLEDGE SEARCH
# ============================================================

def validate_document_selector(document):
    """Validate an opaque indexed identity, without accessing the filesystem."""
    if not isinstance(document, dict) or set(document) != {"folder", "source"}:
        raise ValueError("Document must contain exactly folder and source.")
    folder, source = document["folder"], document["source"]
    if not isinstance(folder, str) or folder not in {"input", "knowledge"}:
        raise ValueError("Document folder must be input or knowledge.")
    if (
        not isinstance(source, str) or not source or source != source.strip()
        or len(source.encode("utf-8")) > MAX_DOCUMENT_SOURCE_BYTES
        or "\\" in source or ":" in source
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in source)
        or any(part in {"", ".", ".."} for part in source.split("/"))
    ):
        raise ValueError("Document source must be a bounded relative POSIX path.")
    return {"folder": folder, "source": source}


def list_documents(limit=50, offset=0):
    """Read one bounded file-state metadata page; never open source files."""
    if type(limit) is not int or not 1 <= limit <= MAX_DOCUMENT_PAGE:
        raise ValueError("The limit parameter must be an integer from 1 to 50.")
    if type(offset) is not int or not 0 <= offset <= MAX_DOCUMENT_OFFSET:
        raise ValueError("The offset parameter must be an integer from 0 to 10000.")
    with INDEX_LOCK:
        if file_state_collection is None:
            raise RuntimeError("File state is unavailable.")
        total = file_state_collection.count()
        page = file_state_collection.get(
            limit=limit, offset=offset, include=["metadatas"]
        )
        ids = page.get("ids", [])
        metadatas = page.get("metadatas", [])
        if (
            type(total) is not int or total < 0
            or len(ids) > limit or len(ids) != len(metadatas)
        ):
            raise RuntimeError("Invalid file-state page.")
        documents = []
        for document_id, metadata in zip(ids, metadatas):
            try:
                identity = validate_document_selector({
                    "folder": metadata["root"], "source": metadata["source"]
                })
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("Invalid file-state identity.") from None
            if document_id != file_identity(identity["folder"], identity["source"]):
                raise RuntimeError("Invalid file-state identity.")
            documents.append({"document_id": document_id, **identity})
    next_offset = offset + len(documents)
    has_more = next_offset < total
    return {
        "status": "success",
        "documents": documents,
        "result_count": len(documents),
        "pagination": {
            "limit": limit, "offset": offset, "total": total,
            "has_more": has_more,
            "next_offset": next_offset if has_more and next_offset <= MAX_DOCUMENT_OFFSET else None,
        },
    }


def search_knowledge(query, limit=5, document=DOCUMENT_UNSET):
    """
    Search indexed documents using semantic similarity.

    Returns matching text chunks with source information.
    """

    scoped = document is not DOCUMENT_UNSET
    if scoped:
        document = validate_document_selector(document)

    if not isinstance(query, str) or not query.strip():
        raise ValueError("A search query is required.")

    query = query.strip()

    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(
            f"Search query exceeds the {MAX_QUERY_CHARS:,} character limit."
        )

    if scoped and len(query.encode("utf-8")) > 4000:
        raise ValueError("Scoped search query exceeds the 4000 byte limit.")

    limit = max(
        1,
        min(int(limit), MAX_SEARCH_RESULTS)
    )

    query_embeddings = embed_texts([query])

    # Keep the count and query within the same critical section as index
    # replacement and cleanup so searches do not observe intermediate state.
    with INDEX_LOCK:
        if knowledge_collection is None:
            raise RuntimeError("Knowledge collection is unavailable.")
        query_options = {}
        if scoped:
            where = {"$and": [
                {"root": document["folder"]}, {"source": document["source"]}
            ]}
            # At most top-k metadata records are needed to cap n_results.
            matches = knowledge_collection.get(
                where=where, limit=limit, include=["metadatas"]
            )
            total_chunks = len(matches["ids"])
            query_options["where"] = where
        else:
            total_chunks = knowledge_collection.count()

        if total_chunks == 0:
            if scoped:
                return {
                    "status": "success", "query": query,
                    "result_count": 0, "results": []
                }
            return {
                "status": "empty",
                "query": query,
                "results": []
            }

        result = knowledge_collection.query(
            query_embeddings=query_embeddings,
            n_results=min(limit, total_chunks),
            include=[
                "documents",
                "metadatas",
                "distances"
            ],
            **query_options
        )

    documents = result.get("documents") or [[]]
    metadatas = result.get("metadatas") or [[]]
    distances = result.get("distances") or [[]]
    ids = result.get("ids") or [[]]

    results = []

    for index, text in enumerate(documents[0]):

        metadata = (
            metadatas[0][index]
            if index < len(metadatas[0])
            else {}
        )

        if scoped and (
            metadata.get("root") != document["folder"]
            or metadata.get("source") != document["source"]
        ):
            raise RuntimeError("Scoped search returned an invalid source.")

        distance = (
            distances[0][index]
            if index < len(distances[0])
            else None
        )

        chunk_id = (
            ids[0][index]
            if index < len(ids[0])
            else None
        )

        results.append({
            "text": text,
            "source": metadata.get("source", "Unknown"),
            "folder": metadata.get("root", "Unknown"),
            "location": metadata.get("location", "Unknown"),
            "chunk_number": metadata.get("chunk_number"),
            "distance": distance,
            "chunk_id": chunk_id
        })

    return {
        "status": "success",
        "query": query,
        "result_count": len(results),
        "results": results
    }


# ============================================================
# DOCUMENT DELETION
# ============================================================

def delete_document_index(root_name, relative_source):
    """
    Remove all indexed chunks and file-state metadata
    belonging to one document.
    """

    if root_name not in {"input", "knowledge"}:
        raise ValueError("Invalid document root.")

    with INDEX_LOCK:

        # Remove semantic chunks.
        knowledge_collection.delete(
            where={
                "$and": [
                    {"root": root_name},
                    {"source": relative_source}
                ]
            }
        )

        # Remove file-state record.
        state_id = file_identity(
            root_name,
            relative_source
        )

        file_state_collection.delete(
            ids=[state_id]
        )

        logger.info(
            "Removed indexed document: %s/%s",
            root_name,
            relative_source
        )


# ============================================================
# DISCOVERED FILE CLEANUP
# ============================================================

def cleanup_missing_documents(
    discovered_files,
    complete_roots
):
    """
    Remove records for documents that no longer exist.

    discovered_files:
        Set of (root_name, relative_path) tuples.

    complete_roots:
        Set of roots that were scanned successfully.

    A root is cleaned only when its scan was complete.
    This prevents a temporary scanning error from deleting
    valid knowledge.
    """

    if not complete_roots:
        return {
            "removed_documents": 0,
            "removed_chunks": 0
        }

    discovered_files = set(discovered_files)
    complete_roots = set(complete_roots)

    removed_documents = 0
    removed_chunks = 0

    with INDEX_LOCK:

        # ----------------------------------------------------
        # Inspect file-state records.
        # ----------------------------------------------------

        state_count = file_state_collection.count()

        if state_count > 0:

            state_records = file_state_collection.get(
                limit=state_count,
                include=["metadatas"]
            )

            for state_id, metadata in zip(
                state_records["ids"],
                state_records["metadatas"]
            ):

                root_name = metadata.get("root")
                relative_source = metadata.get("source")

                if root_name not in complete_roots:
                    continue

                if (root_name, relative_source) in discovered_files:
                    continue

                filesystem_status = check_document_filesystem(
                    root_name,
                    relative_source
                )

                if filesystem_status is True:
                    logger.info(
                        "Preserving indexed document found after scan: %s/%s",
                        root_name,
                        relative_source
                    )
                    continue

                if filesystem_status is None:
                    logger.warning(
                        "Preserving indexed document because it could not be "
                        "verified: %s/%s",
                        root_name,
                        relative_source
                    )
                    continue

                # File is missing from a successfully scanned root.
                matching_chunks = knowledge_collection.get(
                    where={
                        "$and": [
                            {"root": root_name},
                            {"source": relative_source}
                        ]
                    },
                    include=["metadatas"]
                )

                chunk_ids = matching_chunks["ids"]

                if chunk_ids:
                    knowledge_collection.delete(ids=chunk_ids)
                    removed_chunks += len(chunk_ids)

                file_state_collection.delete(
                    ids=[state_id]
                )

                removed_documents += 1

                logger.info(
                    "Removed missing document: %s/%s",
                    root_name,
                    relative_source
                )

        # ----------------------------------------------------
        # Clean orphaned chunks.
        #
        # This also handles chunks left behind by older
        # indexes that do not have a corresponding state
        # record.
        # ----------------------------------------------------

        chunk_count = knowledge_collection.count()

        if chunk_count > 0:

            chunk_records = knowledge_collection.get(
                limit=chunk_count,
                include=["metadatas"]
            )

            orphaned = {}

            for chunk_id, metadata in zip(
                chunk_records["ids"],
                chunk_records["metadatas"]
            ):

                root_name = metadata.get("root")
                relative_source = metadata.get("source")

                if root_name not in complete_roots:
                    continue

                if (root_name, relative_source) in discovered_files:
                    continue

                filesystem_status = check_document_filesystem(
                    root_name,
                    relative_source
                )

                if filesystem_status is not False:
                    if filesystem_status is True:
                        logger.info(
                            "Preserving chunks for document found after scan: "
                            "%s/%s",
                            root_name,
                            relative_source
                        )
                    else:
                        logger.warning(
                            "Preserving chunks because document existence "
                            "could not be verified: %s/%s",
                            root_name,
                            relative_source
                        )
                    continue

                orphaned.setdefault(
                    (root_name, relative_source),
                    []
                ).append(chunk_id)

            for (root_name, relative_source), chunk_ids in orphaned.items():

                knowledge_collection.delete(
                    ids=chunk_ids
                )

                removed_chunks += len(chunk_ids)

                logger.info(
                    "Removed %s orphaned chunks from %s/%s",
                    len(chunk_ids),
                    root_name,
                    relative_source
                )

    return {
        "removed_documents": removed_documents,
        "removed_chunks": removed_chunks
    }


# ============================================================
# INDEXING STATISTICS
# ============================================================

def get_indexing_statistics():
    """
    Return basic information about the knowledge base.
    """

    chunk_count = knowledge_collection.count()
    file_count = file_state_collection.count()

    return {
        "chunks": chunk_count,
        "indexed_files": file_count
    }

# ============================================================
# FILE DISCOVERY
# ============================================================

def discover_documents():
    """
    Scan input and knowledge directories.

    Returns:
        discovered_files: set of (root_name, relative_path)
        complete_roots: roots scanned without filesystem errors

    Symlinks are ignored. Oversized files are still included
    in discovered_files so their existing index is not deleted
    merely because they exceed the indexing size limit.
    """

    discovered_files = set()
    complete_roots = set()

    for root_name, root_path in get_roots().items():

        scan_complete = True

        if not root_path.exists() or not root_path.is_dir():
            logger.error(
                "Knowledge root is unavailable: %s",
                root_path
            )
            continue

        def handle_walk_error(error):
            nonlocal scan_complete
            scan_complete = False

            logger.error(
                "Filesystem scan error in %s: %s",
                root_name,
                error
            )

        for current_dir, directories, filenames in os.walk(
            root_path,
            topdown=True,
            onerror=handle_walk_error,
            followlinks=False
        ):

            current_path = Path(current_dir)

            # Do not follow directory symlinks.
            directories[:] = [
                directory
                for directory in directories
                if not (
                    current_path / directory
                ).is_symlink()
            ]

            for filename in filenames:

                file_path = current_path / filename

                try:
                    if file_path.is_symlink():
                        continue

                    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue

                    relative_path = file_path.relative_to(
                        root_path
                    ).as_posix()

                    discovered_files.add(
                        (root_name, relative_path)
                    )

                except OSError as error:

                    scan_complete = False

                    logger.error(
                        "Could not inspect file %s: %s",
                        file_path,
                        error
                    )

        if scan_complete:
            complete_roots.add(root_name)

    return discovered_files, complete_roots


def check_document_filesystem(root_name, relative_source):
    """
    Check whether an indexed document currently exists safely.

    Returns:
        True: the expected regular file exists.
        False: the expected file is genuinely missing.
        None: the filesystem could not be checked safely.

    A None result is intentionally conservative: cleanup must not delete
    indexed data when a root, path component, or filesystem operation is
    unavailable or unsafe to verify.
    """

    roots = get_roots()
    root = roots.get(root_name)

    if root is None or not isinstance(relative_source, str):
        return None

    relative_path = Path(relative_source)

    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.suffix.lower() not in SUPPORTED_EXTENSIONS
    ):
        return None

    try:
        if not root.exists() or not root.is_dir():
            return None

        current_path = root

        for part in relative_path.parts:
            current_path = current_path / part

            if current_path.is_symlink():
                return None

        candidate = (root / relative_path).resolve()
        candidate.relative_to(root.resolve())

        if candidate.is_file():
            return True

        if candidate.exists():
            return None

        return False

    except (OSError, RuntimeError, ValueError) as error:
        logger.warning(
            "Could not safely verify indexed document %s/%s: %s",
            root_name,
            relative_source,
            error
        )
        return None


# ============================================================
# AUTOMATIC INDEXING CYCLE
# ============================================================


def run_indexing_cycle():
    """
    Synchronize input and knowledge folders with ChromaDB.


    Deleted documents are removed only after a complete
    scan of their respective root.
    """

    logger.info("Starting knowledge synchronization cycle.")

    discovered_files, complete_roots = discover_documents()

    indexed = 0
    unchanged = 0
    failed = 0

    # --------------------------------------------------------
    # Process discovered files.
    # --------------------------------------------------------

    for root_name, relative_path in sorted(discovered_files):

        try:

            full_path = f"{root_name}/{relative_path}"

           

# Documents without a state record must be indexed.
# index_document() safely replaces existing chunks
# only after extraction and embedding succeed.
                    

            result = index_document(full_path)

            if result["status"] == "indexed":
                indexed += 1

            elif result["status"] == "unchanged":
                unchanged += 1

        except Exception:

            failed += 1

            logger.exception(
                "Failed to process %s/%s",
                root_name,
                relative_path
            )

    # --------------------------------------------------------
    # Clean missing files only after a successful scan.
    # --------------------------------------------------------

    cleanup_result = cleanup_missing_documents(
        discovered_files,
        complete_roots
    )

    statistics = get_indexing_statistics()

    logger.info(
            "Synchronization complete: "
            "indexed=%s, unchanged=%s, failed=%s, "
            "removed_documents=%s, removed_chunks=%s, "
            "total_chunks=%s, indexed_files=%s",
            indexed,
            unchanged,
            failed,
            cleanup_result["removed_documents"],
            cleanup_result["removed_chunks"],
            statistics["chunks"],
            statistics["indexed_files"]
        )

    return {
            "indexed": indexed,
            "unchanged": unchanged,
            "failed": failed,
            **cleanup_result,
            **statistics
        }



# ============================================================
# WATCHER THREAD
# ============================================================

def knowledge_watcher():
    """
    Background process that periodically synchronizes
    the local folders with ChromaDB.
    """

    logger.info(
        "Knowledge watcher started. Interval: %s seconds.",
        WATCH_INTERVAL_SECONDS
    )

    while True:

        try:
            run_indexing_cycle()

        except Exception:

            logger.exception(
                "Unexpected error during knowledge synchronization."
            )

        time.sleep(WATCH_INTERVAL_SECONDS)


# ============================================================
# HTTP RESPONSE HELPERS
# ============================================================

def json_response(handler, status_code, payload):

    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status_code)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )

    handler.send_header(
        "Content-Length",
        str(len(body))
    )

    handler.end_headers()

    handler.wfile.write(body)


# ============================================================
# HTTP SERVER
# ============================================================

class KnowledgeRequestHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(
            "%s - %s",
            self.address_string(),
            format % args
        )

    def do_GET(self):

        if self.path == "/documents" or self.path.startswith("/documents?"):
            if not authorized(self.headers):
                json_response(self, 401, {"error": "Unauthorized."})
                return
            try:
                if len(self.path) > 1024:
                    raise ValueError("Invalid pagination parameters.")
                parameters = parse_qs(
                    urlsplit(self.path).query, keep_blank_values=True,
                    strict_parsing=True, max_num_fields=2
                )
                if set(parameters) - {"limit", "offset"}:
                    raise ValueError("Invalid pagination parameters.")
                values = {}
                for key, default in (("limit", "50"), ("offset", "0")):
                    items = parameters.get(key, [default])
                    if len(items) != 1 or not re.fullmatch(r"[0-9]{1,5}", items[0]):
                        raise ValueError("Invalid pagination parameters.")
                    values[key] = int(items[0])
                json_response(self, 200, list_documents(**values))
            except ValueError:
                json_response(self, 400, {"error": "Invalid pagination parameters."})
            except Exception:
                logger.exception("Document catalog failed.")
                json_response(self, 500, {"error": "Document catalog unavailable."})
            return

        if self.path != "/health":
            json_response(
                self,
                404,
                {"error": "Endpoint not found."}
            )
            return

        try:
            statistics = get_indexing_statistics()

            json_response(
                self,
                200,
                {
                    "status": "ok",
                    "collection": "agent_knowledge",
                    "watcher_enabled": WATCHER_ENABLED,
                    "watch_interval_seconds": (
                        WATCH_INTERVAL_SECONDS
                        if WATCHER_ENABLED
                        else None
                    ),
                    **statistics
                }
            )

        except Exception as error:

            logger.exception("Health check failed.")

            json_response(
                self,
                500,
                {
                    "status": "error",
                    "error": "Knowledge service unavailable."
                }
            )

    def do_POST(self):

        # ----------------------------------------------------
        # AUTHENTICATION
        # ----------------------------------------------------

        if not authorized(self.headers):

            json_response(
                self,
                401,
                {"error": "Unauthorized."}
            )

            return

        # ----------------------------------------------------
        # REQUEST BODY
        # ----------------------------------------------------

        try:

            content_length = int(
                self.headers.get("Content-Length", "0")
            )

            if content_length <= 0 or content_length > MAX_REQUEST_BODY:

                json_response(
                    self,
                    400,
                    {"error": "Invalid request body size."}
                )

                return

            self.connection.settimeout(REQUEST_BODY_TIMEOUT_SECONDS)

            try:
                raw_body = self.rfile.read(content_length)
            finally:
                self.connection.settimeout(None)

            payload = json.loads(
                raw_body.decode("utf-8")
            )

            if not isinstance(payload, dict):

                raise ValueError(
                    "Request body must be a JSON object."
                )

        except Exception:

            json_response(
                self,
                400,
                {"error": "Invalid JSON request."}
            )

            return

        # ----------------------------------------------------
        # INDEX DOCUMENT
        # ----------------------------------------------------

        if self.path == "/index":

            try:

                relative_path = payload.get("path")

                force = payload.get("force", False)

                if not isinstance(force, bool):
                    raise ValueError(
                        "The force parameter must be a boolean."
                    )

                result = index_document(
                    relative_path,
                    force=force
                )

                json_response(
                    self,
                    200,
                    {
                        **result,
                        "collection": "agent_knowledge"
                    }
                )

            except FileNotFoundError as error:

                json_response(
                    self,
                    404,
                    {"error": str(error)}
                )

            except ValueError as error:

                json_response(
                    self,
                    400,
                    {"error": str(error)}
                )

            except Exception:

                logger.exception("Document indexing failed.")

                json_response(
                    self,
                    500,
                    {"error": "Document indexing failed."}
                )

            return

        # ----------------------------------------------------
        # SEMANTIC SEARCH
        # ----------------------------------------------------

        if self.path == "/search":

            try:

                query = payload.get("query")

                limit = payload.get("limit", 5)

                if isinstance(limit, bool) or not isinstance(limit, int):
                    raise ValueError(
                        "The limit parameter must be an integer."
                    )

                result = search_knowledge(
                    query,
                    limit=limit,
                    **({"document": payload["document"]} if "document" in payload else {})
                )

                json_response(
                    self,
                    200,
                    result
                )

            except ValueError as error:

                json_response(
                    self,
                    400,
                    {"error": str(error)}
                )

            except Exception:

                logger.exception("Knowledge search failed.")

                json_response(
                    self,
                    500,
                    {"error": "Knowledge search failed."}
                )

            return

        # ----------------------------------------------------
        # UNKNOWN ENDPOINT
        # ----------------------------------------------------

        json_response(
            self,
            404,
            {"error": "Endpoint not found."}
        )


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Limit active request threads without changing the HTTP API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_semaphore = threading.BoundedSemaphore(
            MAX_CONCURRENT_REQUESTS
        )

    def process_request(self, request, client_address):
        if not self.request_semaphore.acquire(blocking=False):
            request.close()
            return

        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_semaphore.release()


# ============================================================
# SERVER STARTUP
# ============================================================

def main():

    logger.info("Starting local knowledge service.")

    logger.info(
        "ChromaDB: %s:%s",
        CHROMA_HOST,
        CHROMA_PORT
    )

    logger.info(
        "Embedding model: %s",
        EMBEDDING_MODEL
    )

    initialize_chromadb()
    logger.info(
        "Knowledge chunks: %s",
        knowledge_collection.count()
    )

    logger.info(
        "Indexed files: %s",
        file_state_collection.count()
    )

    # Start automatic folder monitoring.
    if WATCHER_ENABLED:

        watcher_thread = threading.Thread(
            target=knowledge_watcher,
            name="knowledge-watcher",
            daemon=True
        )

        watcher_thread.start()

    server = BoundedThreadingHTTPServer(
        ("0.0.0.0", 8002),
        KnowledgeRequestHandler
    )

    logger.info(
        "Knowledge service listening on port 8002."
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        logger.info("Knowledge service shutting down.")

    finally:
        server.server_close()


if __name__ == "__main__":
    main()
