import json
import chromadb

with open("/backup/agent_knowledge.json", "r", encoding="utf-8") as f:
    data = json.load(f)

ids = data["ids"]
documents = data["documents"]
metadatas = data["metadatas"]
embeddings = data["embeddings"]

assert len(ids) == 14
assert len(set(ids)) == 14
assert len(documents) == len(metadatas) == len(embeddings) == 14
assert all(len(e) == 768 for e in embeddings)
assert all(
    m.get("root") == "input" and m.get("source") == "sample.pdf"
    for m in metadatas
)

client = chromadb.HttpClient(host="chromadb-test", port=8000)

knowledge = client.get_collection("agent_knowledge")
state = client.get_collection("agent_file_state")

assert knowledge.count() == 0, "Test knowledge collection is not empty"
assert state.count() == 0, "Test file-state collection is not empty"

knowledge.upsert(
    ids=ids,
    documents=documents,
    metadatas=metadatas,
    embeddings=embeddings
)

stored = knowledge.get(include=["documents", "metadatas"])

assert knowledge.count() == 14
assert set(stored["ids"]) == set(ids)
assert state.count() == 0

stored_documents = dict(zip(stored["ids"], stored["documents"]))
assert all(stored_documents[i] == d for i, d in zip(ids, documents))

print("Legacy import: SUCCESS")
print("Knowledge chunks:", knowledge.count())
print("File state records:", state.count())
print("Source: input/sample.pdf")