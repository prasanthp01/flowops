"""Focused hygiene checks; not a substitute for independent secret scanning."""

import re
import subprocess
from pathlib import Path

paths = (
    subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    .decode()
    .split("\0")
)
patterns = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    re.compile(r"/Users/[a-zA-Z][^\s/]+/"),
]
findings = []
for name in paths:
    path = Path(name)
    if not name or not path.is_file() or path.suffix in (".png", ".webp", ".jpg", ".zip"):
        continue
    content = path.read_text(errors="replace")
    if any(pattern.search(content) for pattern in patterns):
        findings.append(name)
print("Focused source hygiene: " + ("PASS" if not findings else "FAIL"))
for finding in findings:
    print(finding)
raise SystemExit(bool(findings))
