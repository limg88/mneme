import shutil
import zipfile
from pathlib import Path

import pytest

from work_memory.local_files import (
    LocalFormatError,
    collect_folder,
    local_observation,
    read_document,
)
from work_memory.models import LocalScope
from work_memory.store import Store


def docx(path, body):
    xml = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def test_docx_body_and_tables_preserve_text_without_executing_fields(tmp_path):
    path = tmp_path / "work.docx"
    docx(
        path,
        "<w:p><w:r><w:t>DEMO </w:t></w:r><w:r><w:t>plan</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cost</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>100 EUR</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    text, metadata = read_document(path)
    assert text == "DEMO plan\nCost\t100 EUR"
    assert metadata["format"] == "docx"
    docx(path, "<w:p><w:ins><w:r><w:t>Unaccepted revision</w:t></w:r></w:ins></w:p>")
    with pytest.raises(LocalFormatError, match="tracked changes"):
        read_document(path)


def test_docx_entities_and_oversized_xml_are_rejected(tmp_path):
    path = tmp_path / "bad.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "secret">]><x>&a;</x>')
    with pytest.raises(LocalFormatError, match="XML"):
        read_document(path)


def test_folder_limits_resume_and_never_follow_symlinks_or_hidden_files(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE")
    (root / "a.md").write_text("Alpha")
    (root / "b.txt").write_text("Beta")
    (root / "leak.md").symlink_to(outside)
    (root / ".hidden.md").write_text("PRIVATE")
    store = Store(tmp_path / "memory")
    scope = LocalScope(root=str(root), project="DEMO", max_files_per_sync=1)
    first = collect_folder(store, scope)
    assert first["new"] == 1 and first["state"] == "partial"
    second = collect_folder(store, scope)
    assert second["new"] == 1 and second["state"] == "complete"
    assert store.search("PRIVATE")["total"] == 0
    assert collect_folder(store, scope)["observed"] == 0
    (root / "a.md").write_text("Gamma changed")
    assert collect_folder(store, scope)["new"] == 1
    assert store.search("Alpha")["total"] == 1 and store.search("Gamma")["total"] == 1


def test_confirmed_missing_local_file_blocks_all_project_copies(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    path = root / "a.md"
    path.write_text("Alpha")
    store = Store(tmp_path / "memory")
    scope = LocalScope(root=str(root), project="DEMO")
    collect_folder(store, scope)
    store.ingest(local_observation(path, "other"))
    path.unlink()
    result = collect_folder(store, scope)
    assert result["revoked"] == 2 and store.search("Alpha")["total"] == 0
    assert store.is_withdrawn("local", str(path), "future project")


def test_missing_root_is_not_inferred_as_mass_deletion(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "a.md").write_text("Alpha")
    store = Store(tmp_path / "memory")
    scope = LocalScope(root=str(root), project="DEMO")
    collect_folder(store, scope)
    root.rename(tmp_path / "renamed")
    assert collect_folder(store, scope)["state"] == "incomplete"
    assert store.search("Alpha")["total"] == 1


def test_permission_check_runs_even_for_unchanged_cached_files(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    path = root / "a.md"
    path.write_text("Alpha")
    store = Store(tmp_path / "memory")
    scope = LocalScope(root=str(root), project="DEMO")
    collect_folder(store, scope)
    import os

    original = os.access
    monkeypatch.setattr(
        "work_memory.local_files.os.access",
        lambda p, m: False if Path(p) == path else original(p, m),
    )
    result = collect_folder(store, scope)
    assert result["revoked"] == 1 and store.search("Alpha")["total"] == 0


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler not installed")
def test_real_pdf_text_main_path(tmp_path):
    path = tmp_path / "work.pdf"
    stream = b"BT /F1 12 Tf 72 700 Td (DEMO local document) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{p:010d} 00000 n \n".encode() for p in offsets[1:])
    data += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    path.write_bytes(data)
    text, metadata = read_document(path)
    assert "DEMO local document" in text and metadata["format"] == "pdf"
