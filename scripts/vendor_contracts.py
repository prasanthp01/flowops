"""Import only the verified, immutable local Contracts 1.0.0 release."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "b55d1a9dc5d2bab23817b984b9801ccff7304f04"
SOURCE = "02dacbc396189eb72809cfa228efc548d7e48925"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()

    def git(*argv):
        return subprocess.check_output(["git", "-C", str(args.source), *argv])

    assert git("rev-parse", "v1.0.0").decode().strip() == RELEASE
    manifest = json.loads(git("show", f"{RELEASE}:contracts-manifest.json"))
    assert manifest["release_source_commit"] == SOURCE
    for section in ("files", "test_reports"):
        for name, expected in manifest[section].items():
            assert hashlib.sha256(git("show", f"{RELEASE}:{name}")).hexdigest() == expected
    base = ROOT / "flowops/_vendor/ecosystem_contracts"
    fixtures = ROOT / "tests/fixtures/contracts-v1"
    files = {}
    names = git("ls-tree", "-r", "--name-only", RELEASE).decode().splitlines()
    for name in names:
        if name.startswith("src/ecosystem_contracts/") and name.endswith(".py"):
            target = base / name.removeprefix("src/ecosystem_contracts/")
        elif name.startswith("schemas/v1/") or name == "LICENSE":
            target = base / name
        elif name.startswith(("examples/valid/", "examples/invalid/", "examples/errors/")):
            target = fixtures / name.removeprefix("examples/")
        elif name in {"openapi/ecosystem-v1.json", "docs/PROTOCOL.md", "contracts-manifest.json"}:
            target = ROOT / "contracts-v1" / name
        else:
            continue
        data = git("show", f"{RELEASE}:{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[str(target.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    (ROOT / "contracts.lock.json").write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "wire_version": "1.0",
                "release_commit": RELEASE,
                "tested_source_commit": SOURCE,
                "local_tag": "v1.0.0",
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Verified and pinned {len(files)} Contracts files; canonical repository unchanged.")


if __name__ == "__main__":
    main()
