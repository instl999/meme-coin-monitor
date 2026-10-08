"""Build dist/holder-watch-<version>.tar.gz (+ .sha256) for deployment. Run: python deploy/package.py

Leaves out secrets and machine-local data (.env, state.json, logs, virtualenvs, caches), converts
text files to LF line endings, marks the scripts executable, and refuses to build if any secret
value from the local .env appears in a packaged file.
"""

import hashlib
import io
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watcher import __version__  # noqa: E402
from watcher.config import load_dotenv  # noqa: E402

INCLUDE = ["holder_watch.py", "watcher", "tests", "deploy", "config.json", "presets.json", "requirements.txt",
           "requirements-dev.txt", "README.md", "README.en.md", "README.zh-CN.md", "DEPLOY.md", "CODEX_DEPLOY.md", ".env.example", ".gitignore"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}
EXECUTABLE = {"deploy/install.sh", "holder_watch.py"}
SECRET_NAMES = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "TELEGRAM_BOT_TOKEN", "ANKR_API_KEY", "ALCHEMY_API_KEY",
                "ETHEREUM_RPC_URL", "BASE_RPC_URL", "BSC_RPC_URL", "ARBITRUM_RPC_URL")


def package_files():
    for item in INCLUDE:
        path = ROOT / item
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                rel = child.relative_to(ROOT)
                if child.is_file() and not SKIP_PARTS & set(rel.parts) and child.suffix not in SKIP_SUFFIXES:
                    yield child
        else:
            raise SystemExit(f"missing {item}")


def main() -> None:
    env = {}
    load_dotenv(ROOT / ".env", env)
    secrets = [env[name].encode() for name in SECRET_NAMES if len(env.get(name, "")) >= 8]

    name = f"holder-watch-{__version__}"
    out = ROOT / "dist" / f"{name}.tar.gz"
    out.parent.mkdir(exist_ok=True)
    mtime = int(time.time())
    count = 0
    with tarfile.open(out, "w:gz") as tar:
        for path in package_files():
            rel = path.relative_to(ROOT).as_posix()
            data = path.read_bytes().replace(b"\r\n", b"\n")
            leaked = [s for s in secrets if s in data]
            if leaked:
                out.unlink(missing_ok=True)
                raise SystemExit(f"refusing to package: {rel} contains a secret value from .env")
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size, info.mtime = len(data), mtime
            info.mode = 0o755 if rel in EXECUTABLE else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
            count += 1
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8", newline="\n")
    print(f"{out.relative_to(ROOT)}: {count} files, {out.stat().st_size / 1024:.0f} KB")
    print(f"sha256 {digest}")
    print(f"checked {len(secrets)} secret value(s) from .env: none packaged")


if __name__ == "__main__":
    main()
