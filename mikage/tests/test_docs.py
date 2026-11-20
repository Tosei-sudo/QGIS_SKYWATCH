"""
The explanations (README.md, docs/, models/README.md): every link to a file
of the repository finds it, and every `#anchor` finds a heading there
(GitHub's anchors). README.md was split into docs/features/ and docs/admin/
on 2026-11-20 (docs/architecture.md); the settings file maker links into
them through its DOC_FILES.
"""
import glob
import os
import re
import unicodedata

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: Anchors the settings file maker linked to before the split that name no heading (left as they were).
KNOWN_BROKEN = {"サーバー障害時のオフライン作業", "先読み"}


def slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    return "".join(c for c in text if c in "-_ " or unicodedata.category(c)[0] in "LMN").replace(" ", "-")


def anchors(path: str) -> set:
    out, fence = set(), False
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("```"):
                fence = not fence
            elif not fence:
                m = re.match(r"^#+ (.+)$", line.rstrip("\n"))
                if m:
                    out.add(slug(m.group(1)))
    return out


def documents() -> list:
    found = [os.path.join(REPO, "README.md"), os.path.join(REPO, "models", "README.md")]
    found += glob.glob(os.path.join(REPO, "docs", "**", "*.md"), recursive=True)
    return sorted(found)


def test_every_link_finds_its_file_and_heading():
    problems = []
    for doc in documents():
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        for url in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"^(https?:|mailto:)", url):
                continue
            path, _, frag = url.partition("#")
            target = os.path.normpath(os.path.join(os.path.dirname(doc), path)) if path else doc
            if not os.path.exists(target):
                problems.append(f"{os.path.relpath(doc, REPO)}: {url}（ファイルなし）")
                continue
            if frag and target.endswith(".md") and frag not in anchors(target):
                problems.append(f"{os.path.relpath(doc, REPO)}: {url}（見出しなし）")
    assert problems == []


def test_the_readme_lists_every_split_file():
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    for path in glob.glob(os.path.join(REPO, "docs", "features", "*.md")) + glob.glob(os.path.join(REPO, "docs", "admin", "*.md")):
        assert f"]({os.path.relpath(path, REPO)})" in readme, path


def test_the_settings_file_maker_links_into_the_split_files():
    with open(os.path.join(REPO, "tools", "config_builder.html"), encoding="utf-8") as f:
        html = f.read()
    files = dict(re.findall(r'^  "([^"]+)": "([^"]+)",?$', html.split("const DOC_FILES = {", 1)[1].split("};", 1)[0], re.MULTILINE))
    docs = set(re.findall(r'doc: "([^"]+)"', html))
    assert docs <= set(files)
    for anchor in docs:
        path = os.path.join(REPO, files[anchor])
        assert os.path.exists(path), files[anchor]
        assert anchor in anchors(path) or anchor in KNOWN_BROKEN, f"{anchor} が {files[anchor]} にない"


def test_the_help_page_is_built_from_its_parts():
    """mikage/help/index.html = mikage/help/src/*.html joined (scripts/build_help.py; edit the parts)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_help", os.path.join(REPO, "scripts", "build_help.py"))
    build_help = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_help)
    with open(build_help.PAGE, encoding="utf-8", newline="") as f:
        page = f.read()
    assert page == build_help.build(), "help/index.html が src と違う: python3 scripts/build_help.py"
    # One part per section (h2), in the page's order.
    names = build_help.parts()
    assert names[0] == "000-head.html" and names[-1] == "999-tail.html"
    for name in names[1:-1]:
        with open(os.path.join(build_help.SRC, name), encoding="utf-8") as f:
            assert f.read().startswith(f'<h2 id="{name.split("-", 1)[1][:-5]}"'), name
