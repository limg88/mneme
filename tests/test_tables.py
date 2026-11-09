from work_memory.models import Observation
from work_memory.store import Store
from work_memory.tables import TABLE_EXTRACTOR, table_facts
from work_memory.worker import derive_tables


def test_horizontal_values_keep_columns_units_and_literal_evidence():
    text = (
        "## Sheet: Test\nProject\t\tTOTALE COSTO\tTOTALE RICAVO\tMARGINE\n"
        "\t\t100,00\t200,00 €\t50%\n"
    )
    facts = table_facts(text, "Test")
    assert {(f.predicate, f.value) for f in facts} == {
        ("reported_total_cost", "100,00"),
        ("reported_total_revenue", "200,00 €"),
        ("reported_margin_percent", "50%"),
    }
    assert all(f.quote in text and "TOTALE COSTO" in f.quote for f in facts)
    assert all(f.valid_from is None and f.valid_to is None for f in facts)


def test_blank_cells_formulas_and_unaligned_values_are_never_reinterpreted():
    text = (
        "TOTALE COSTO\tTOTALE RICAVO\tMARGINE\n\t=SUM(A1:A5)\t\n\n"
        "COSTI\t\t999\nFormulas:\nC2: =100+200"
    )
    assert table_facts(text, "Sheet") == []
    # No recognised horizontal header pair, so an adjacent row is not guessed.
    assert table_facts("COSTI\t\n999\t", "Sheet") == []


def test_legacy_targets_stay_separate_and_header_values_are_not_commitments():
    text = "TARGET GIORNATE - A\t273,00\n[LEGACY ] TARGET GIORNATE - A\t87,00\n"
    facts = table_facts(text, "## Sheet: PI7")
    assert len(facts) == 2 and len({f.subject for f in facts}) == 2
    assert {f.value for f in facts} == {"273,00", "87,00"}
    assert all(f.predicate.startswith("reported_") and f.quote in text for f in facts)


def test_table_derivation_is_local_idempotent_and_preserves_provenance(tmp_path):
    store = Store(tmp_path)
    text = "Workbook: 1 worksheet.\n## Sheet: Budget\nCOSTI\t123,45 €\n"
    oid = store.ingest(
        Observation(
            source="fixture",
            source_id="1",
            project="test",
            source_url="fixture://1",
            title="Budget",
            content=text,
        )
    )["id"]
    assert derive_tables(store, oid) == 1
    assert derive_tables(Store(tmp_path), oid) == 0
    item = store.facts()["items"][0]
    assert item["extractor"] == TABLE_EXTRACTOR
    assert item["extraction_provenance"]["engine"] == "deterministic"
    assert text[item["start_offset"] : item["end_offset"]] == item["quote"]
    store.withdraw("fixture", "1", "test")
    assert store.facts()["total"] == 0 and derive_tables(store) == 0
