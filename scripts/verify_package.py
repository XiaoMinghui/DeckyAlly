"""Verify the distributable, including importing its backend outside the source tree."""

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile


def verify():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    artifact = root / "artifacts" / f"DeckyAlly-{version}.zip"
    digest = artifact.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "Duplicate ZIP entries"
        for name in names:
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and path.parts[0] == "DeckyAlly"
            assert not any(x in path.parts for x in ("node_modules", "__pycache__", ".git"))
            if name.endswith(".py"):
                ast.parse(archive.read(name), filename=name)
        manifest = json.loads(archive.read("DeckyAlly/plugin.json"))
        assert manifest["name"] == "DeckyAlly" and manifest["flags"] == ["root"] and manifest["api_version"] == 1
        assert "DeckyAlly/dist/index.js" in names
        assert len(archive.read("DeckyAlly/dist/index.js")) > 100
        assert archive.testzip() is None
        archive.extractall(tmp)
        code = (
            "import ast, importlib.util, sys, types; "
            "from pathlib import Path; "
            "sys.modules['decky'] = types.ModuleType('decky'); "
            "spec = importlib.util.spec_from_file_location('plugin', 'main.py'); "
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "from decky_ally.recovery import DEFAULTS; "
            "assert DEFAULTS == {'enabled': True, 'mode': 'conditional', 'delay_seconds': 8}; "
            "assert hasattr(module.Plugin, '_main') and hasattr(module.Plugin, '_unload'); "
            "assert not hasattr(module.Plugin, 'restart'); "
            "print('Packaged backend imports successfully')"
        )
        subprocess.run([sys.executable, "-c", code], cwd=Path(tmp) / "DeckyAlly", check=True)
    print(f"Verified {artifact.name}: {len(names)} files, checksum, paths, Python syntax and packaged imports")


if __name__ == "__main__":
    verify()
