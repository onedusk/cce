"""Emit never writes outside its target directory (audit 2.1).

``unit.path`` and the topic slug come from the job request. Request
validation rejects traversal now, but packages stored before it, or built
directly, reach the sink unvalidated: the sink refuses them itself.
"""

from __future__ import annotations

import pytest

from cce.output.mdx import emit_mdx, safe_child_dir
from cce.output.mdx.thnklabs import emit_thnklabs
from tests.conftest import make_content_unit, make_evidence, make_publish_package

pytestmark = pytest.mark.unit

EMITTERS = {"generic": emit_mdx, "client": emit_thnklabs}


def _package(path: str):
    ev = make_evidence(id="ev_1")
    unit = make_content_unit(path=path, content="## T\n\nA claim [ev:ev_1].")
    return make_publish_package(units=[unit], evidence=[ev])


@pytest.mark.parametrize("fmt", sorted(EMITTERS))
@pytest.mark.parametrize(
    "path", ["../../escaped", "/tmp/cce-abs-escape", "learn/../../escaped", "..", ""]
)
def test_hostile_unit_path_is_refused_and_nothing_escapes(fmt, path, tmp_path):
    target = tmp_path / "site" / "content"
    target.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside the target directory"):
        EMITTERS[fmt](_package(path), target, topic_slug="topic")

    written = {p.relative_to(tmp_path).parts[:2] for p in tmp_path.rglob("*")}
    assert written <= {("site",), ("site", "content")}


@pytest.mark.parametrize("fmt", sorted(EMITTERS))
@pytest.mark.parametrize("slug", ["../escaped", "/tmp/cce-abs-escape", "a/b", ""])
def test_hostile_topic_slug_is_refused(fmt, slug, tmp_path):
    target = tmp_path / "content"
    target.mkdir()

    with pytest.raises(ValueError, match="outside the target directory"):
        EMITTERS[fmt](_package("learn"), target, topic_slug=slug)

    assert list(tmp_path.iterdir()) == [target]
    assert list(target.iterdir()) == []


def test_symlink_out_of_the_target_is_refused(tmp_path):
    target = tmp_path / "content"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "topic").symlink_to(outside)

    with pytest.raises(ValueError, match="outside the target directory"):
        safe_child_dir(target, target, "topic")


@pytest.mark.parametrize("fmt", sorted(EMITTERS))
def test_ordinary_names_still_emit(fmt, tmp_path):
    EMITTERS[fmt](_package("learn"), tmp_path, topic_slug="sleep-hygiene")
    assert (tmp_path / "sleep-hygiene" / "learn" / "page.mdx").is_file()
