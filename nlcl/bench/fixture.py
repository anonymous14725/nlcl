"""Builds the small project folder used by the terminal tests and the benchmark."""
from pathlib import Path

FILES = {
    "README.md": "# demo\nA small project.\n",
    "notes.txt": "banana\napple\nCherry\napple pie\n",
    "data.csv": "id,name\n1,ann\n2,bob\n3,cy\n",
    "src/main.py": "import util\nprint('hello')\n# TODO: refactor\n",
    "src/util.py": "def add(a, b):\n    return a + b\n# TODO: tests\n",
    "src/app.js": "console.log('hi');\n",
    "src/old.tmp": "scratch\n",
    "logs/run1.log": "ok\nwarning: disk\nok\n",
    "logs/run2.log": "error: crash\n",
    "cache.tmp": "x\n",
    "docs/guide.md": "Guide\nTODO: write\n",
    "big.bin": "0" * 5000 + "\n",
}


def build(root: Path) -> Path:
    root = Path(root)
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    (root / "empty_dir").mkdir(exist_ok=True)
    return root
