"""Сборка ZIP из выбранной ревизии и подпись манифеста внешним ключом."""
import argparse
from pathlib import Path
import json
import subprocess
from sys import path
from time import time
from zipfile import ZipFile, ZipInfo

path.insert(0, str(Path(__file__).resolve().parents[1]))
from secure_release import ASSET, MANIFEST, SIGNATURE, key_id, sha256, version_number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--days", type=int, default=30, choices=range(1, 91), metavar="1..90")
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "--verify", args.revision+"^{commit}"], text=True).strip()
    version = subprocess.check_output(["git", "show", revision + ":VERSION"], text=True).strip()
    version_number(version)
    dest = args.output
    dest.mkdir(exist_ok=True)
    archive = dest / ASSET
    subprocess.run(["git", "archive", "--format=zip", "--output", archive, revision], check=True)
    with ZipFile(archive, "a") as bundle:
        bundle.writestr(ZipInfo("REVISION"), revision + "\n")
    issued = int(time())
    manifest = {
        "format":1, "product":"Cugam Helper", "version":version, "revision":revision, "asset":ASSET,
        "sha256":sha256(archive.read_bytes()).hexdigest(), "size":archive.stat().st_size, "schema":2,
        "issued":issued, "expires":issued + args.days*86400, "key_id":key_id(args.public_key),
        "python":["3.10", "3.12"], "platform":"linux-x86_64", "channel":"stable",
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n")
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", args.private_key, "-rawin",
                    "-in", dest / MANIFEST, "-out", dest / SIGNATURE], check=True)


if __name__ == "__main__":
    main()
