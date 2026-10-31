import os
import json
import secrets
import csv
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from pypdf import PdfReader
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

ROOTS = [
    Path("/workspace/input").resolve(),
    Path("/workspace/knowledge").resolve()
]

TOKEN = os.environ["DOCUMENT_TOKEN"]
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_OUTPUT = 100000
MAX_PDF_PAGES = 100

SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt", ".md"}


def resolve_document(relative_path):
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("A relative document path is required.")

    candidate_path = Path(relative_path)

    if candidate_path.is_absolute():
        raise ValueError("Absolute paths are not allowed.")

    for root in ROOTS:
        candidate = (root / candidate_path).resolve()

        if root in candidate.parents and candidate.is_file():
            return candidate

    raise FileNotFoundError("Document not found in permitted folders.")


def extract_document(path):
    extension = path.suffix.lower()

    if extension not in SUPPORTED:
        raise ValueError(f"Unsupported format: {extension}")

    if path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("File exceeds the 20 MB limit.")

    sections = []

    if extension == ".pdf":
        reader = PdfReader(str(path))

        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF exceeds the 100-page processing limit.")

        for number, page in enumerate(reader.pages, start=1):
            sections.append({
                "section": f"Page {number}",
                "text": page.extract_text() or ""
            })

    elif extension == ".docx":
        document = Document(str(path))

        paragraphs = [
            p.text for p in document.paragraphs if p.text.strip()
        ]

        for index, paragraph in enumerate(paragraphs, start=1):
            sections.append({
                "section": f"Paragraph {index}",
                "text": paragraph
            })

        for table_index, table in enumerate(document.tables, start=1):
            rows = []
            for row in table.rows:
                rows.append(" | ".join(cell.text for cell in row.cells))

            sections.append({
                "section": f"Table {table_index}",
                "text": "\n".join(rows)
            })

    elif extension == ".xlsx":
        workbook = load_workbook(
            filename=path,
            read_only=True,
            data_only=True
        )

        try:
            for sheet in workbook.worksheets:
                rows = []

                for row in sheet.iter_rows(values_only=True):
                    values = [str(v) if v is not None else "" for v in row]

                    if any(values):
                        rows.append(" | ".join(values))

                    if len(rows) >= 5000:
                        rows.append("[Sheet row limit reached]")
                        break

                sections.append({
                    "section": f"Sheet: {sheet.title}",
                    "text": "\n".join(rows)
                })
        finally:
            workbook.close()

    elif extension == ".pptx":
        presentation = Presentation(str(path))

        for number, slide in enumerate(presentation.slides, start=1):
            texts = []

            for shape in slide.shapes:
                if shape.has_text_frame and shape.text.strip():
                    texts.append(shape.text)

                if shape.has_table:
                    for row in shape.table.rows:
                        texts.append(
                            " | ".join(cell.text for cell in row.cells)
                        )

            sections.append({
                "section": f"Slide {number}",
                "text": "\n".join(texts)
            })

    elif extension == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file)

            for index, row in enumerate(reader, start=1):
                sections.append({
                    "section": f"Row {index}",
                    "text": " | ".join(row)
                })

                if index >= 10000:
                    sections.append({
                        "section": "Notice",
                        "text": "CSV row limit reached."
                    })
                    break

    else:
        text = path.read_text(encoding="utf-8-sig")
        sections.append({
            "section": "Document text",
            "text": text
        })

    total_chars = sum(len(s["text"]) for s in sections)
    truncated = False

    if total_chars > MAX_OUTPUT:
        remaining = MAX_OUTPUT
        limited = []

        for section in sections:
            if remaining <= 0:
                break

            content = section["text"][:remaining]
            limited.append({
                "section": section["section"],
                "text": content
            })
            remaining -= len(content)

        sections = limited
        truncated = True

    return {
        "filename": path.name,
        "format": extension,
        "size_bytes": path.stat().st_size,
        "sections": sections,
        "truncated": truncated
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/extract":
            return self.send_json(404, {"error": "Endpoint not found."})

        supplied = self.headers.get("Authorization", "")

        if not secrets.compare_digest(supplied, "Bearer " + TOKEN):
            return self.send_json(401, {"error": "Unauthorized."})

        try:
            length = int(self.headers.get("Content-Length", "0"))

            if length <= 0 or length > 4096:
                return self.send_json(400, {"error": "Invalid request size."})

            data = json.loads(self.rfile.read(length))
            path = resolve_document(data.get("path"))
            result = extract_document(path)

            return self.send_json(200, result)

        except FileNotFoundError as e:
            return self.send_json(404, {"error": str(e)})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:
            return self.send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        print("Document service:", format % args, flush=True)


print("Document service listening on port 8001", flush=True)
HTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
