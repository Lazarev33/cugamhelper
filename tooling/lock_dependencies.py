"""Генерирует lock из проверенного pip --report и публичных метаданных PyPI."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from urllib.request import urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def package(name, version, marker):
    with urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
        data = json.load(response)
    metadata = data["info"]
    wheels = [file for file in data["urls"] if file["packagetype"] == "bdist_wheel" and not file["yanked"]
              and (file["filename"].endswith("-any.whl") or
                   "manylinux" in file["filename"] and "x86_64" in file["filename"])]
    if not wheels:
        raise ValueError(f"{name}=={version}: отсутствуют Linux wheels")
    hashes = sorted({file["digests"]["sha256"] for file in wheels})
    for python in ("3.10", "3.12"):
        if not marker or python in marker:
            if not SpecifierSet(metadata.get("requires_python") or "").contains(Version(python)):
                raise ValueError(f"{name}=={version}: конфликт Python {python}")
    pin = f"{name}=={version}" + (f" ; {marker}" if marker else "")
    line = pin + " " + " ".join("--hash=sha256:" + digest for digest in hashes)
    classifiers = [value.removeprefix("License :: ") for value in metadata["classifiers"] if value.startswith("License :: ")]
    return line, {"name": name, "version": version, "scope": marker or "all",
                  "license": metadata.get("license_expression") or classifiers or "см. LICENSE внутри wheel",
                  "declared_license_files": metadata.get("license_files") or [], "source": metadata["package_url"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    items = [(re.sub(r"[-_.]+", "-", row["metadata"]["name"]).lower(), row["metadata"]["version"], "")
             for row in json.loads(args.report.read_text())["install"]]
    special = {"pillow": ("12.3.0", "13.0.0"), "starlette": ("1.6.0", "1.10.0"),
               "annotated-doc": ("0.0.4", "0.0.7"), "multidict": ("7.1.0", "7.2.1")}
    items = [row for row in items if row[0] not in special]
    for name, versions in special.items():
        items.extend((name, version, f'python_version == "{python}"') for version, python in zip(versions, ("3.10", "3.12")))
    items += [("async-timeout", "5.0.1", 'python_version == "3.10"'),
              ("exceptiongroup", "1.3.1", 'python_version == "3.10"')]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda row: package(*row), sorted(items)))
    Path("requirements.lock").write_text(
        "# Точный набор wheels для Linux x86_64 / CPython 3.10 и 3.12.\n" +
        "\n".join(line for line, _ in results) + "\n")
    Path("docs/sbom.json").write_text(json.dumps(
        {"format": "cugam-python-components-v1", "components": [info for _, info in results]},
        ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
