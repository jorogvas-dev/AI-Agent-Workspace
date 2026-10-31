import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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

    def get(self, ids=None, where=None, include=None, limit=None, offset=0):
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

        records = records[offset:]
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


class TrackingLock:
    def __init__(self):
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, *args):
        self.depth -= 1


class ScopedCollection(InMemoryCollection):
    """Apply filters before ranked top-k, and reject oversized n_results."""
    def __init__(self, lock):
        super().__init__()
        self.lock = lock
        self.get_calls = []
        self.query_calls = []
        self.count_calls = 0

    def count(self):
        assert self.lock.depth > 0, "count must hold INDEX_LOCK"
        self.count_calls += 1
        return super().count()

    def get(self, **kwargs):
        assert self.lock.depth > 0, "availability lookup must hold INDEX_LOCK"
        self.get_calls.append(kwargs)
        return super().get(**kwargs)

    def query(self, **kwargs):
        assert self.lock.depth > 0, "query must hold INDEX_LOCK"
        self.query_calls.append(kwargs)
        records = sorted(
            ((key, value) for key, value in self.records.items()
             if self._matches(value["metadata"], kwargs.get("where"))),
            key=lambda item: item[1]["embedding"][0],
        )
        assert kwargs["n_results"] <= len(records)
        records = records[:kwargs["n_results"]]
        return {
            "ids": [[key for key, _ in records]],
            "documents": [[value["document"] for _, value in records]],
            "metadatas": [[value["metadata"] for _, value in records]],
            "distances": [[value["embedding"][0] for _, value in records]],
        }


class DocumentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.lock = TrackingLock()
        self.state = ScopedCollection(self.lock)
        self.chunks = ScopedCollection(self.lock)
        for name, value in (
            ("INDEX_LOCK", self.lock), ("file_state_collection", self.state),
            ("knowledge_collection", self.chunks), ("SERVICE_TOKEN", "unit-test-token"),
        ):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.embed = Mock(side_effect=self.embedding)
        patcher = patch.object(server, "embed_texts", self.embed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def embedding(self, texts):
        self.assertEqual(self.lock.depth, 0, "remote embeddings must not hold commit lock")
        return [[0.0] * server.EMBEDDING_DIMENSIONS]

    def seed(self, folder, source, chunk_id, rank=1.0):
        self.state.upsert(
            ids=[server.file_identity(folder, source)],
            documents=["must-not-be-returned"],
            metadatas=[{"root": folder, "source": source, "sha256": "must-not-be-returned"}],
            embeddings=[[0.0]],
        )
        self.chunks.upsert(
            ids=[chunk_id], documents=[f"content for {chunk_id}"],
            metadatas=[{"root": folder, "source": source, "location": "Section 2"}],
            embeddings=[[rank]],
        )

    def http(self, method, path, payload=None, authenticated=True):
        handler = FakeHandler(path, json.dumps(payload or {}).encode())
        if not authenticated:
            handler.headers["Authorization"] = "Bearer wrong-unit-test-token"
        captured = []
        with patch.object(server, "json_response", side_effect=lambda h, s, p: captured.append((s, p))):
            getattr(server.KnowledgeRequestHandler, method)(handler)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_catalog_pages_only_metadata_and_existing_identities(self):
        self.seed("input", "reports/entry.pdf", "a")
        self.seed("knowledge", "reports/entry.pdf", "b")
        self.seed("input", "other.md", "c")
        with patch.object(Path, "open", side_effect=AssertionError("no source reads")):
            status, first = self.http("do_GET", "/documents?limit=2&offset=0")
            _, last = self.http("do_GET", "/documents?limit=2&offset=2")
        self.assertEqual(status, 200)
        self.assertEqual(first["pagination"], {
            "limit": 2, "offset": 0, "total": 3, "has_more": True, "next_offset": 2,
        })
        self.assertEqual(first["result_count"], 2)
        self.assertEqual({item["folder"] for item in first["documents"]}, {"input", "knowledge"})
        self.assertEqual(len({item["document_id"] for item in first["documents"]}), 2)
        self.assertEqual(last["result_count"], 1)
        self.assertFalse(last["pagination"]["has_more"])
        self.assertIsNone(last["pagination"]["next_offset"])
        for item in first["documents"] + last["documents"]:
            self.assertEqual(set(item), {"document_id", "folder", "source"})
            self.assertEqual(item["document_id"], server.file_identity(item["folder"], item["source"]))
        self.assertEqual(self.state.get_calls, [
            {"limit": 2, "offset": 0, "include": ["metadatas"]},
            {"limit": 2, "offset": 2, "include": ["metadatas"]},
        ])
        self.assertNotIn("must-not-be-returned", json.dumps(first))
        self.embed.assert_not_called()

    def test_catalog_defaults_empty_and_offset_boundary(self):
        status, result = self.http("do_GET", "/documents")
        self.assertEqual(status, 200)
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["pagination"]["limit"], 50)
        status, result = self.http("do_GET", "/documents?limit=1&offset=10000")
        self.assertEqual(status, 200)
        self.assertEqual(result["result_count"], 0)
        self.assertEqual(self.state.get_calls[-1]["offset"], 10000)

    def test_catalog_authentication_precedes_database_access(self):
        status, result = self.http("do_GET", "/documents?limit=1", authenticated=False)
        self.assertEqual((status, result), (401, {"error": "Unauthorized."}))
        self.assertEqual(self.state.count_calls, 0)
        self.assertEqual(self.state.get_calls, [])

    def test_catalog_rejects_invalid_pagination_before_database_access(self):
        for query in (
            "limit=0", "limit=51", "limit=true", "limit=1.0", "limit=", "limit=-1",
            "offset=-1", "offset=10001", "offset=1.5", "offset=+1", "offset=abc",
            "limit=1&limit=2", "path=secret", "limit", "limit=1&offset=0&extra=1",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.http("do_GET", "/documents?" + query)[0], 400)
        self.assertEqual(self.state.get_calls, [])

    def test_catalog_invalid_stored_identity_and_database_failure_are_generic(self):
        self.seed("input", "../../private.txt", "bad")
        with patch.object(server.logger, "exception"):
            status, result = self.http("do_GET", "/documents")
            self.assertEqual(status, 500)
            self.assertEqual(result, {"error": "Document catalog unavailable."})
            with patch.object(self.state, "get", side_effect=RuntimeError("private database detail")):
                self.assertEqual(self.http("do_GET", "/documents")[1], result)

    def test_scoped_query_filters_before_top_k_in_both_folders(self):
        self.seed("knowledge", "reports/entry.pdf", "competing-folder", 0.0)
        self.seed("input", "unrelated.pdf", "competing-source", 0.1)
        self.seed("input", "reports/entry.pdf", "target", 9.0)
        for folder, expected in (("input", "target"), ("knowledge", "competing-folder")):
            with self.subTest(folder=folder):
                result = server.search_knowledge("field question", limit=1, document={"folder": folder, "source": "reports/entry.pdf"})
                self.assertEqual(result["result_count"], 1)
                self.assertEqual(result["results"][0]["chunk_id"], expected)
                self.assertEqual(result["results"][0]["folder"], folder)
                where = {"$and": [{"root": folder}, {"source": "reports/entry.pdf"}]}
                self.assertEqual(self.chunks.query_calls[-1]["where"], where)
                self.assertEqual(self.chunks.get_calls[-1], {"where": where, "limit": 1, "include": ["metadatas"]})
        self.assertEqual(self.chunks.count_calls, 0)

    def test_fewer_matching_chunks_caps_query_without_unbounded_count(self):
        self.seed("input", "nested/document.md", "one", 1.0)
        self.seed("input", "nested/document.md", "two", 2.0)
        self.seed("knowledge", "other.pdf", "competitor", 0.0)
        status, result = self.http("do_POST", "/search", {
            "query": "question", "limit": 8,
            "document": {"folder": "input", "source": "nested/document.md"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(self.chunks.query_calls[-1]["n_results"], 2)
        self.assertEqual(self.chunks.get_calls[-1]["limit"], 8)

    def test_zero_scoped_matches_never_fall_back_to_global_query(self):
        self.seed("knowledge", "existing.md", "competitor")
        for records in (self.chunks.records.copy(), {}):
            self.chunks.records = records
            result = server.search_knowledge("question", document={"folder": "input", "source": "missing.md"})
            self.assertEqual(result, {"status": "success", "query": "question", "result_count": 0, "results": []})
        self.assertEqual(self.chunks.query_calls, [])

    def test_malformed_selectors_rejected_before_embedding(self):
        invalid = [None, [], "input/document.pdf", {}, {"folder": "input"},
                   {"folder": "input", "source": "a.pdf", "where": {}},
                   {"folder": [], "source": "a.pdf"}, {"folder": "output", "source": "a.pdf"}]
        invalid += [{"folder": "input", "source": source} for source in (
            None, 1, "", "../a.pdf", "a/../../b.pdf", "/a.pdf", "C:/a.pdf",
            "https://example.invalid/a.pdf", "a\\b.pdf", "a//b.pdf", "a/./b.pdf",
            "a.pdf\x00", "a.pdf\n", "a.pdf\x7f", "a.pdf\u0085", " a.pdf", "x" * 513,
        )]
        for document in invalid:
            with self.subTest(document=document):
                status, _ = self.http("do_POST", "/search", {"query": "question", "document": document})
                self.assertEqual(status, 400)
        self.embed.assert_not_called()
        self.assertEqual(self.chunks.get_calls, [])

    def test_scoped_query_utf8_limit_and_legacy_search_are_preserved(self):
        self.seed("input", "example.md", "one")
        selector = {"folder": "input", "source": "example.md"}
        server.search_knowledge("é" * 2000, document=selector)
        self.embed.reset_mock()
        with self.assertRaises(ValueError):
            server.search_knowledge("é" * 2001, document=selector)
        self.embed.assert_not_called()
        result = server.search_knowledge("legacy", limit=99)
        self.assertEqual(result["result_count"], 1)
        self.assertNotIn("where", self.chunks.query_calls[-1])
        self.chunks.records.clear()
        self.assertEqual(server.search_knowledge("legacy"), {"status": "empty", "query": "legacy", "results": []})

    def test_scoped_search_requires_authentication(self):
        status, _ = self.http("do_POST", "/search", {
            "query": "question", "document": {"folder": "input", "source": "document.md"}
        }, authenticated=False)
        self.assertEqual(status, 401)
        self.embed.assert_not_called()


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
