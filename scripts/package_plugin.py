"""Create a Decky ZIP with one plugin directory and no local settings/dependencies."""

import hashlib
import json
from pathlib import Path
import zipfile


def build():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    required = ["main.py", "plugin.json", "package.json", "dist/index.js", "dist/index.js.map",
                "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"]
    files = [root / path for path in required]
    files.extend(sorted((root / "py_modules").rglob("*.py")))
    for path in files:
        if not path.is_file():
            raise SystemExit(f"Missing required file: {path}. Run npm run build first.")
    output = root / "artifacts" / f"DeckyAlly-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = "DeckyAlly/" + path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(f"Created {output} ({output.stat().st_size} bytes)")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    build()
