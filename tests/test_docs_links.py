"""
Every relative link in the Markdown docs points at a file that exists.

This is here because the README linked to `LICENSE` from both the badge and the License
section, `pyproject.toml` and `CITATION.cff` both declared MIT, and the file had never
been committed — so the project's stated license was not actually granted to anyone. No
test noticed, because nothing executes a link.

Link rot is silent and cumulative: a doc renamed in one PR leaves a dead reference in
five others, and the only signal is a reader hitting a 404 on GitHub. Checking it is
deterministic and costs milliseconds, so it belongs in the suite rather than in a review
checklist.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HTML_SRC = re.compile(r'src="([^"]+)"')
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def _markdown_files():
    files = [REPO / "README.md"]
    files += sorted((REPO / "docs").glob("*.md"))
    files += [REPO / n for n in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")]
    return [f for f in files if f.exists()]


def _targets(path: Path):
    """(label, target) for every link or image that should resolve on disk."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    for label, url in MD_LINK.findall(text):
        yield label, url
    for url in HTML_SRC.findall(text):
        yield "<img>", url


@pytest.mark.parametrize("doc", _markdown_files(), ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    broken = []
    for label, url in _targets(doc):
        if url.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = url.split("#")[0].strip()
        if not target:
            continue
        # Links in docs/*.md are written relative to the file that contains them.
        if not (doc.parent / target).exists() and not (REPO / target).exists():
            broken.append(f"[{label}]({url})")
    assert not broken, f"{doc.relative_to(REPO)} links to missing files: {broken}"


def test_readme_nav_anchors_match_real_headings():
    """The header nav is the first thing a reader clicks; a dead anchor scrolls nowhere."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    slugs = {
        re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        for h in HEADING.findall(text)
    }
    nav = re.findall(r'href="#([^"]+)"', text)
    assert nav, "the README nav disappeared; this test is now checking nothing"
    assert [a for a in nav if a in slugs] == nav, \
        f"nav anchors with no matching heading: {[a for a in nav if a not in slugs]}"


def test_the_license_file_exists_and_matches_the_declared_license():
    """
    Three places declare MIT. The grant only exists if the file does — this is the
    specific bug that motivated the module, so it gets its own assertion rather than
    relying on the README happening to link to it.
    """
    license_file = REPO / "LICENSE"
    assert license_file.exists(), "LICENSE is missing; the MIT claim grants nothing"
    body = license_file.read_text(encoding="utf-8")
    assert "MIT License" in body
    assert "Copyright (c)" in body

    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject, "pyproject no longer says MIT; LICENSE now disagrees"
