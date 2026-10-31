import io
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("KNOWLEDGE_TOKEN", "unit-test-token")


chromadb_stub = types.ModuleType("chromadb")
chromadb_stub.HttpClient = object
sys.modules.setdefault("chromadb", chromadb_stub)

pypdf_stub = types.ModuleType("pypdf")
pypdf_stub.PdfReader = object
sys.modules.setdefault("pypdf", pypdf_stub)

docx_stub = types.ModuleType("docx")
docx_stub.Document = object
sys.modules.setdefault("docx", docx_stub)

openpyxl_stub = types.ModuleType("openpyxl")
openpyxl_stub.load_workbook = object
sys.modules.setdefault("openpyxl", openpyxl_stub)

pptx_stub = types.ModuleType("pptx")
pptx_stub.Presentation = object
sys.modules.setdefault("pptx", pptx_stub)

sys.path.insert(0, str(Path(__file__).parent))
import server


class EmbeddingResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self.data}


class FakeCollection:
    def __init__(self, count=0):
        self._count = count
        self.query_calls = 0

    def count(self):
        return self._count

    def query(self, **kwargs):
        self.query_calls += 1
        return {
            "documents": [["result"]],
            "metadatas": [[{"source": "input/a.txt"}]],
            "distances": [[0.1]],
            "ids": [["chunk"]],
        }


class InMemoryCollection:
    def __init__(self):
        self.records = {}

    def count(self):
        return len(self.records)

    def _matches(self, metadata, where):
        if not where:
            return True

        if "$and" in where:
            return all(self._matches(metadata, item) for item in where["$and"])

        return all(metadata.get(key) == value for key, value in where.items())

    def get(self, ids=None, where=None, include=None, limit=None):
        records = [
            (record_id, record)
            for record_id, record in self.records.items()
            if ids is None or record_id in ids
        ]

        records = [
            (record_id, record)
            for record_id, record in records
            if self._matches(record["metadata"], where)
        ]

        if limit is not None:
            records = records[:limit]

        return {
            "ids": [record_id for record_id, _ in records],
            "metadatas": [record["metadata"] for _, record in records],
        }

    def upsert(self, ids, documents, metadatas, embeddings):
        for record_id, document, metadata, embedding in zip(
            ids,
            documents,
            metadatas,
            embeddings,
        ):
            self.records[record_id] = {
                "document": document,
                "metadata": metadata,
                "embedding": embedding,
            }

    def delete(self, ids=None, where=None):
        if ids is None:
            ids = [
                record_id
                for record_id, record in self.records.items()
                if self._matches(record["metadata"], where)
            ]

        for record_id in ids:
            self.records.pop(record_id, None)


class FakeConnection:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeHandler:
    def __init__(self, path, body=b"{}"):
        self.path = path
        self.headers = {
            "Authorization": "Bearer unit-test-token",
            "Content-Length": str(len(body)),
        }
        self.rfile = io.BytesIO(body)
        self.connection = FakeConnection()
        self.responses = []


class KnowledgeServiceTests(unittest.TestCase):
    def _seed_index(self, state_collection, knowledge_collection, source):
        state_collection.upsert(
            ids=[server.file_identity("input", source)],
            documents=[f"input:{source}"],
            metadatas=[{
                "root": "input",
                "source": source,
                "sha256": "old-hash",
            }],
            embeddings=[[0.0] * server.EMBEDDING_DIMENSIONS],
        )
        knowledge_collection.upsert(
            ids=["old-chunk"],
            documents=["old content"],
            metadatas=[{"root": "input", "source": source}],
            embeddings=[[0.0] * server.EMBEDDING_DIMENSIONS],
        )

    def _cleanup_with_collections(
        self,
        roots,
        state_collection,
        knowledge_collection,
        discovered_files=None,
        check=None,
    ):
        if discovered_files is None:
            discovered_files = set()

        patches = [
            patch.object(server, "get_roots", return_value=roots),
            patch.object(server, "file_state_collection", state_collection),
            patch.object(server, "knowledge_collection", knowledge_collection),
        ]

        if check is not None:
            patches.append(patch.object(server, "check_document_filesystem", side_effect=check))

        with patches[0], patches[1], patches[2]:
            if len(patches) == 4:
                with patches[3]:
                    return server.cleanup_missing_documents(
                        discovered_files,
                        {"input"},
                    )

            return server.cleanup_missing_documents(
                discovered_files,
                {"input"},
            )

    def test_unprefixed_path_is_resolved_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            knowledge_root = root / "knowledge"
            input_root.mkdir()
            knowledge_root.mkdir()
            (input_root / "document.txt").write_text("content")

            with patch.object(
                server,
                "get_roots",
                return_value={
                    "input": input_root,
                    "knowledge": knowledge_root,
                },
            ):
                name, selected_root, resolved = server.resolve_document(
                    "document.txt"
                )

            self.assertEqual(name, "input")
            self.assertEqual(selected_root, input_root)
            self.assertEqual(resolved, input_root / "document.txt")

    def test_file_created_and_indexed_after_discovery_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory) / "input"
            input_root.mkdir()
            document = input_root / "new.txt"
            document.write_text("new content")

            state = InMemoryCollection()
            knowledge = InMemoryCollection()
            self._seed_index(state, knowledge, "new.txt")

            result = self._cleanup_with_collections(
                {"input": input_root},
                state,
                knowledge,
            )

            self.assertEqual(result["removed_documents"], 0)
            self.assertEqual(result["removed_chunks"], 0)
            self.assertEqual(state.count(), 1)
            self.assertEqual(knowledge.count(), 1)

    def test_genuinely_deleted_document_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory) / "input"
            input_root.mkdir()
            state = InMemoryCollection()
            knowledge = InMemoryCollection()
            self._seed_index(state, knowledge, "deleted.txt")

            result = self._cleanup_with_collections(
                {"input": input_root},
                state,
                knowledge,
            )

            self.assertEqual(result["removed_documents"], 1)
            self.assertEqual(result["removed_chunks"], 1)
            self.assertEqual(state.count(), 0)
            self.assertEqual(knowledge.count(), 0)

    def test_filesystem_inspection_error_preserves_records(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory) / "input"
            input_root.mkdir()
            state = InMemoryCollection()
            knowledge = InMemoryCollection()
            self._seed_index(state, knowledge, "unknown.txt")

            result = self._cleanup_with_collections(
                {"input": input_root},
                state,
                knowledge,
                check=lambda root, source: None,
            )

            self.assertEqual(result["removed_documents"], 0)
            self.assertEqual(result["removed_chunks"], 0)
            self.assertEqual(state.count(), 1)
            self.assertEqual(knowledge.count(), 1)

    def test_cleanup_and_index_state_updates_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory) / "input"
            input_root.mkdir()
            document = input_root / "concurrent.txt"
            document.write_text("content")

            state = InMemoryCollection()
            knowledge = InMemoryCollection()
            self._seed_index(state, knowledge, "concurrent.txt")

            status_entered = threading.Event()
            release_status = threading.Event()
            writer_started = threading.Event()
            writer_acquired = threading.Event()

            def blocked_check(root, source):
                status_entered.set()
                release_status.wait(timeout=2)
                return True

            def writer():
                writer_started.set()
                with server.INDEX_LOCK:
                    state.upsert(
                        ids=[server.file_identity("input", "concurrent.txt")],
                        documents=["input:concurrent.txt"],
                        metadatas=[{
                            "root": "input",
                            "source": "concurrent.txt",
                            "sha256": "new-hash",
                        }],
                        embeddings=[[0.0] * server.EMBEDDING_DIMENSIONS],
                    )
                    writer_acquired.set()

            cleanup_result = []

            def cleanup():
                cleanup_result.append(self._cleanup_with_collections(
                    {"input": input_root},
                    state,
                    knowledge,
                    check=blocked_check,
                ))

            cleanup_thread = threading.Thread(target=cleanup)
            cleanup_thread.start()
            self.assertTrue(status_entered.wait(timeout=2))

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            self.assertTrue(writer_started.wait(timeout=2))
            self.assertFalse(writer_acquired.is_set())

            release_status.set()
            cleanup_thread.join(timeout=2)
            writer_thread.join(timeout=2)

            self.assertFalse(cleanup_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertTrue(writer_acquired.is_set())
            self.assertEqual(cleanup_result[0]["removed_documents"], 0)
            self.assertEqual(
                state.records[server.file_identity("input", "concurrent.txt")]["metadata"]["sha256"],
                "new-hash",
            )

    def test_indexing_still_skips_unchanged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory) / "input"
            knowledge_root = Path(directory) / "knowledge"
            input_root.mkdir()
            knowledge_root.mkdir()
            document = input_root / "indexed.txt"
            document.write_text("indexed content")

            state = InMemoryCollection()
            knowledge = InMemoryCollection()
            vector = [0.0] * server.EMBEDDING_DIMENSIONS

            with patch.object(
                server,
                "get_roots",
                return_value={"input": input_root, "knowledge": knowledge_root},
            ), patch.object(server, "file_state_collection", state), patch.object(
                server, "knowledge_collection", knowledge
            ), patch.object(
                server,
                "extract_document",
                return_value=[{"location": "Text content", "text": "indexed content"}],
            ), patch.object(server, "embed_texts", return_value=[vector]):
                first = server.index_document("input/indexed.txt")
                second = server.index_document("input/indexed.txt")

            self.assertEqual(first["status"], "indexed")
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(knowledge.count(), 1)

    def test_embedding_indices_must_cover_batch(self):
        embedding = [0.0] * server.EMBEDDING_DIMENSIONS
        malformed = [
            {"index": 0, "embedding": embedding},
            {"index": 0, "embedding": embedding},
        ]

        with patch.object(server.requests, "post", return_value=EmbeddingResponse(malformed)):
            with self.assertRaisesRegex(RuntimeError, "invalid embedding indices"):
                server.embed_texts(["one", "two"])

    def test_embedding_indices_are_sorted_into_input_order(self):
        first = [1.0] * server.EMBEDDING_DIMENSIONS
        second = [2.0] * server.EMBEDDING_DIMENSIONS
        response = [
            {"index": 1, "embedding": second},
            {"index": 0, "embedding": first},
        ]

        with patch.object(server.requests, "post", return_value=EmbeddingResponse(response)):
            result = server.embed_texts(["one", "two"])

        self.assertEqual(result, [first, second])

    def test_format_specific_pdf_limit(self):
        class OversizedPdf:
            pages = [object()] * (server.MAX_PDF_PAGES + 1)

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "large.pdf"
            document.write_bytes(b"not parsed by the stub")

            with patch.object(server, "PdfReader", return_value=OversizedPdf()):
                with self.assertRaisesRegex(ValueError, "page processing limit"):
                    server.extract_document(document)

    def test_format_specific_xlsx_limit(self):
        class OversizedWorkbook:
            worksheets = [object()] * (server.MAX_XLSX_SHEETS + 1)

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "large.xlsx"
            document.write_bytes(b"not parsed by the stub")

            with patch.object(
                server,
                "load_workbook",
                return_value=OversizedWorkbook(),
            ):
                with self.assertRaisesRegex(ValueError, "worksheet processing limit"):
                    server.extract_document(document)

    def test_format_specific_pptx_limit(self):
        class OversizedPresentation:
            slides = [object()] * (server.MAX_PPTX_SLIDES + 1)

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "large.pptx"
            document.write_bytes(b"not parsed by the stub")

            with patch.object(
                server,
                "Presentation",
                return_value=OversizedPresentation(),
            ):
                with self.assertRaisesRegex(ValueError, "slide processing limit"):
                    server.extract_document(document)

    def test_query_length_is_rejected_before_embedding(self):
        with patch.object(server, "embed_texts") as embed:
            with self.assertRaisesRegex(ValueError, "query exceeds"):
                server.search_knowledge("x" * (server.MAX_QUERY_CHARS + 1))

        embed.assert_not_called()

    def test_search_uses_lock_for_collection_query(self):
        collection = FakeCollection(count=1)
        with patch.object(server, "knowledge_collection", collection), patch.object(
            server, "embed_texts", return_value=[[0.0] * server.EMBEDDING_DIMENSIONS]
        ):
            result = server.search_knowledge("hello")

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(collection.query_calls, 1)

    def test_unexpected_http_errors_are_generic(self):
        handler = FakeHandler("/index", b'{"path":"input/a.txt"}')
        captured = []

        with patch.object(server, "json_response", side_effect=lambda h, s, p: captured.append((s, p))), patch.object(
            server, "index_document", side_effect=RuntimeError("secret internal detail")
        ):
            server.KnowledgeRequestHandler.do_POST(handler)

        self.assertEqual(captured[-1], (500, {"error": "Document indexing failed."}))
        self.assertNotIn("secret internal detail", repr(captured))
        self.assertEqual(handler.connection.timeouts, [server.REQUEST_BODY_TIMEOUT_SECONDS, None])

    def test_body_size_limit_uses_configured_constant(self):
        handler = FakeHandler("/search", b"{}")
        handler.headers["Content-Length"] = str(server.MAX_REQUEST_BODY + 1)
        captured = []

        with patch.object(server, "json_response", side_effect=lambda h, s, p: captured.append((s, p))):
            server.KnowledgeRequestHandler.do_POST(handler)

        self.assertEqual(captured, [(400, {"error": "Invalid request body size."})])

    def test_request_server_rejects_excess_threads(self):
        request_server = server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            server.KnowledgeRequestHandler,
        )
        requests = [object() for _ in range(server.MAX_CONCURRENT_REQUESTS)]
        for request in requests:
            request_server.request_semaphore.acquire()

        rejected = types.SimpleNamespace(closed=False)
        rejected.close = lambda: setattr(rejected, "closed", True)

        try:
            request_server.process_request(rejected, ("127.0.0.1", 1))
            self.assertTrue(rejected.closed)
        finally:
            for _ in requests:
                request_server.request_semaphore.release()
            request_server.server_close()


if __name__ == "__main__":
    unittest.main()
