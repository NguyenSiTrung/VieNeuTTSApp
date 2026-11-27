"""Packaging and catalog integrity gate for the English UI localization.

Frozen builds load ``vienetts_en.qm`` relative to ``vienetts_app/ui/i18n/__init__.py``
(``QM_PATH = Path(__file__).parent / "vienetts_en.qm"``). PyInstaller's
``collect_submodules`` only collects Python modules, so the spec must list
``vienetts_en.qm`` explicitly in ``datas``, and the release workflow must verify
the compiled catalog actually lands in the built ``dist/`` bundle on every OS leg.

These tests also guard catalog quality (FR-8.1, AC-10, closes ``VieNeuTTSApp-73r3``):

* ``packaging/vienetts-app.spec`` bundles ``vienetts_en.qm`` under ``vienetts_app/ui/i18n``
  (and does not ship the source ``.ts`` XML).
* ``.github/workflows/release.yml`` asserts ``vienetts_app/ui/i18n/vienetts_en.qm``
  exists in ``dist/`` after PyInstaller runs on Windows, macOS, and Linux.
* ``src/vienetts_app/ui/i18n/vienetts_en.ts`` parses as valid XML with zero
  ``unfinished``, ``obsolete``, or ``vanished`` entries.
* ``src/vienetts_app/ui/i18n/vienetts_en.qm`` exists, is non-empty, loads cleanly
  via ``QTranslator``, and matches every entry in ``vienetts_en.ts`` (catching
  unrecompiled ``.qm`` drift without relying on git checkout mtimes).
"""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from vienetts_app.ui.i18n import QM_PATH, TS_PATH, resolve_language, translator_for

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "packaging" / "vienetts-app.spec"
RELEASE_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
FORBIDDEN_TRANSLATION_TYPES = frozenset({"unfinished", "obsolete", "vanished"})


def _spec_datas_tuples() -> list[tuple[str, str]]:
    """Extract literal ``(source_expr, dest)`` pairs from ``datas = [...]``."""
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "datas" for target in node.targets
            )
            and isinstance(node.value, ast.List)
        ):
            pairs: list[tuple[str, str]] = []
            for element in node.value.elts:
                if (
                    isinstance(element, ast.Tuple)
                    and len(element.elts) == 2
                    and isinstance(element.elts[1], ast.Constant)
                    and isinstance(element.elts[1].value, str)
                ):
                    pairs.append((ast.unparse(element.elts[0]), element.elts[1].value))
            return pairs
    raise AssertionError("packaging/vienetts-app.spec has no literal `datas = [...]` block")


class TestPackagingSpecI18n:
    def test_spec_bundles_english_qm_under_i18n_package_dir(self) -> None:
        datas = _spec_datas_tuples()
        matched = [
            (src, dest)
            for src, dest in datas
            if "src/vienetts_app/ui/i18n/vienetts_en.qm" in src and dest == "vienetts_app/ui/i18n"
        ]
        assert matched, (
            "packaging/vienetts-app.spec must include "
            '(str(REPO / "src/vienetts_app/ui/i18n/vienetts_en.qm"), "vienetts_app/ui/i18n") '
            "in datas"
        )
        assert QM_PATH.is_file(), f"bundled source file is missing: {QM_PATH}"
        assert not any("vienetts_en.ts" in src for src, _ in datas), (
            "source XML vienetts_en.ts should not be shipped in the frozen bundle"
        )


class TestReleaseWorkflowI18n:
    def test_release_workflow_asserts_english_qm_in_dist_for_all_platforms(self) -> None:
        workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
        build_job = workflow["jobs"]["build"]
        platforms = {entry["platform"] for entry in build_job["strategy"]["matrix"]["include"]}
        assert {"windows-x64", "macos-arm64", "linux-x64"} <= platforms

        steps = build_job["steps"]
        build_idx = next(
            (i for i, step in enumerate(steps) if "vienetts-app.spec" in str(step.get("run", ""))),
            None,
        )
        assert build_idx is not None, "release.yml build job has no PyInstaller spec step"

        package_idx = next(
            (
                i
                for i, step in enumerate(steps)
                if i > build_idx and "Package artifact" in str(step.get("name", ""))
            ),
            len(steps),
        )

        qm_assert_steps = [
            step
            for step in steps[build_idx + 1 : package_idx]
            if "vienetts_en.qm" in str(step.get("run", ""))
        ]
        assert qm_assert_steps, (
            "release.yml must assert vienetts_en.qm exists in dist/ after the PyInstaller build"
        )

        # Must run across every OS matrix leg (Linux, macOS, Windows), verify
        # the packaged dist/ tree at the in-package i18n path, and fail non-zero.
        step = qm_assert_steps[0]
        assert step.get("if") is None, "vienetts_en.qm assertion must run on all OS matrix legs"
        script = str(step["run"])
        assert "dist/" in script or "dist " in script
        assert "vienetts_app/ui/i18n/vienetts_en.qm" in script
        assert "exit 1" in script


class TestEnglishCatalogIntegrity:
    def test_ts_catalog_is_valid_xml_with_no_unfinished_obsolete_or_vanished(self) -> None:
        assert TS_PATH.is_file(), f"English source catalog missing: {TS_PATH}"
        root = ET.parse(TS_PATH).getroot()
        assert root.tag == "TS"

        total_messages = 0
        bad_entries: list[tuple[str, str, str]] = []
        empty_entries: list[tuple[str, str]] = []

        for ctx in root.findall("context"):
            ctx_name = ctx.findtext("name", "")
            for msg in ctx.findall("message"):
                total_messages += 1
                source = msg.findtext("source", "")
                tr_el = msg.find("translation")
                if tr_el is None:
                    bad_entries.append((ctx_name, source, "<missing>"))
                    continue
                tr_type = tr_el.attrib.get("type")
                if tr_type in FORBIDDEN_TRANSLATION_TYPES:
                    bad_entries.append((ctx_name, source, tr_type))
                    continue
                if msg.attrib.get("numerus") == "yes":
                    forms = ["".join(el.itertext()) for el in tr_el.findall("numerusform")]
                    if len(forms) < 2 or any(not form for form in forms):
                        empty_entries.append((ctx_name, source))
                elif not "".join(tr_el.itertext()):
                    empty_entries.append((ctx_name, source))

        assert total_messages > 0, "vienetts_en.ts has no translation messages"
        assert not bad_entries, (
            f"vienetts_en.ts has {len(bad_entries)} unfinished/obsolete/vanished/missing "
            f"entries (first 10): {bad_entries[:10]}"
        )
        assert not empty_entries, (
            f"vienetts_en.ts has {len(empty_entries)} empty translations "
            f"(first 10): {empty_entries[:10]}"
        )

    def test_qm_catalog_loads_and_matches_ts_translations(self, qcoreapp) -> None:
        from PySide6.QtCore import QTranslator

        assert QM_PATH.is_file(), f"Compiled English catalog missing: {QM_PATH}"
        assert QM_PATH.stat().st_size > 0, f"Compiled English catalog is empty: {QM_PATH}"

        translator = QTranslator()
        assert translator.load(str(QM_PATH)), f"QTranslator failed to load {QM_PATH}"
        assert not translator.isEmpty(), f"QTranslator loaded empty catalog from {QM_PATH}"
        assert translator.translate("AppController", "Runtime Qwen") == "Qwen runtime"

        # Runtime helper contract: English resolves a live QTranslator, Vietnamese needs none.
        en_translator = translator_for("en")
        assert en_translator is not None
        assert en_translator.translate("AppController", "Runtime Qwen") == "Qwen runtime"
        assert translator_for("vi") is None
        assert resolve_language("en", "vi_VN") == "en"
        assert resolve_language("vi", "en_US") == "vi"
        assert resolve_language("system", "en_US") == "en"
        assert resolve_language("system", "vi_VN") == "vi"

        # Every entry in vienetts_en.ts must resolve identically from vienetts_en.qm.
        # This catches stale .qm files without relying on fragile git checkout mtimes.
        root = ET.parse(TS_PATH).getroot()
        mismatches: list[tuple[str, str, str, str]] = []
        for ctx in root.findall("context"):
            ctx_name = ctx.findtext("name", "")
            for msg in ctx.findall("message"):
                source = msg.findtext("source", "")
                comment = msg.findtext("comment")
                tr_el = msg.find("translation")
                assert tr_el is not None
                if msg.attrib.get("numerus") == "yes":
                    forms = ["".join(el.itertext()) for el in tr_el.findall("numerusform")]
                    for count, expected in ((1, forms[0]), (2, forms[1])):
                        actual = translator.translate(ctx_name, source, comment, count)
                        if actual != expected:
                            mismatches.append((ctx_name, f"{source} [n={count}]", expected, actual))
                else:
                    expected = "".join(tr_el.itertext())
                    actual = translator.translate(ctx_name, source, comment)
                    if actual != expected:
                        mismatches.append((ctx_name, source, expected, actual))

        assert not mismatches, (
            f"vienetts_en.qm is out of sync with vienetts_en.ts ({len(mismatches)} mismatches; "
            f"run scripts/update_i18n.sh; first 10): {mismatches[:10]}"
        )
