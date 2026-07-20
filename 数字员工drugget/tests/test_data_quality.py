from pathlib import Path

from price_specialist.data_quality import STORE_FILE, load_store_records


def test_responsibility_workbook_dimension_is_recalculated_from_actual_cells() -> None:
    project = Path(__file__).resolve().parents[1]
    records = load_store_records(project / "data/raw" / STORE_FILE)
    assert len(records) == 10507
    assert records[0]["店铺ID"] == "W00001"
    assert records[0]["平台"] == "天猫"
