"""The wheel ships what the library needs at run time (the bundled .proto files)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import schema_validator

ROOT = Path(schema_validator.__file__).resolve().parent.parent

_BUILD = """
import sys
from setuptools import build_meta
print(build_meta.build_wheel(sys.argv[1]))
"""


def test_wheel_contains_the_package_and_its_proto_files(tmp_path: Path) -> None:
    # Built from a copy, so no build/ or egg-info directory is left in the project.
    source = tmp_path / "src"
    source.mkdir()
    shutil.copy(ROOT / "pyproject.toml", source)
    shutil.copytree(ROOT / "schema_validator", source / "schema_validator",
                    ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "dist"
    out.mkdir()
    done = subprocess.run([sys.executable, "-c", _BUILD, str(out)], cwd=source, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]

    wheels = list(out.glob("schema_validator-*.whl"))
    assert len(wheels) == 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())

    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "schema_validator").rglob("*")
               if p.suffix in (".py", ".proto")}
    assert on_disk <= names, sorted(on_disk - names)
    protos = sorted(n for n in names if n.endswith(".proto"))
    assert len(protos) == 18
    assert "schema_validator/_proto/confluent/meta.proto" in protos
    assert "schema_validator/_proto/google/type/date.proto" in protos
    # Tests and the integration stack are not shipped.
    assert not any(n.startswith(("tests/", "integration/")) for n in names)
