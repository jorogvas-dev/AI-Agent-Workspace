from pathlib import Path

message = "Python execution through the local agent is working."

Path("/workspace/output/executor-test.txt").write_text(
    message,
    encoding="utf-8"
)

print(message)
