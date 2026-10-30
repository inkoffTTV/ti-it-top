from __future__ import annotations

import ast
from io import BytesIO
from pathlib import Path

from PIL import Image


def _load_prepare_avatar():
    source = Path(__file__).resolve().parents[1] / "miniapp" / "api.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {"_prepare_avatar", "_local_avatar_path"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = {"Path": Path, "BytesIO": BytesIO, "TEAM_AVATAR_MAX_SIDE": 512}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    return ns["_prepare_avatar"]


def test_avatar_is_normalized_to_jpeg():
    src = BytesIO()
    Image.new("RGBA", (1600, 900), (120, 70, 220, 255)).save(src, format="PNG")
    prepare = _load_prepare_avatar()
    out = prepare(src.getvalue())
    with Image.open(BytesIO(out)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 512
        assert image.mode == "RGB"
