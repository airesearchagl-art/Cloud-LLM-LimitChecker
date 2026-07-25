from pathlib import Path


FORBIDDEN_PATTERNS = [
    "responses.create",
    "chat.completions.create",
    "images.generate",
    "audio.",
    "realtime",
    "generateContent",
    "streamGenerateContent",
    "sendMessage",
    "messages.create",
    "messages.stream",
    "embeddings.create",
    "moderations.create",
]


def test_no_paid_model_call_markers_in_app_or_static() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = [root / "app", root / "static"]
    offenders: list[str] = []
    for target in targets:
        for path in target.rglob("*"):
            if path.suffix not in {".py", ".js"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(root)} contains {pattern}")
    assert offenders == []
