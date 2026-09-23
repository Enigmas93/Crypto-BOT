from pathlib import Path

from aegis.macro.series_config import load_macro_series

REPO_YAML = Path(__file__).resolve().parents[1] / "macro_series.yaml"


def test_load_macro_series_parses_a_minimal_file(tmp_path):
    yaml_path = tmp_path / "series.yaml"
    yaml_path.write_text(
        """
series:
  - series_id: FEDFUNDS
    name: "Federal Funds Effective Rate"
    description: "test"
    frequency: monthly
    source: fred
    importance: high
    transformation: level
""",
        encoding="utf-8",
    )

    configs = load_macro_series(yaml_path)

    assert len(configs) == 1
    assert configs[0].series_id == "FEDFUNDS"
    assert configs[0].frequency == "monthly"
    assert configs[0].dataset is None  # BEA-only fields default to None for other sources


def test_load_macro_series_parses_bea_specific_fields(tmp_path):
    yaml_path = tmp_path / "bea.yaml"
    yaml_path.write_text(
        """
series:
  - series_id: A191RL
    name: "Real GDP"
    description: "test"
    frequency: quarterly
    source: bea
    importance: high
    transformation: level
    dataset: NIPA
    table: T10101
    bea_frequency: Q
""",
        encoding="utf-8",
    )

    configs = load_macro_series(yaml_path)

    assert configs[0].dataset == "NIPA"
    assert configs[0].table == "T10101"
    assert configs[0].bea_frequency == "Q"


def test_load_macro_series_empty_file_returns_empty_list(tmp_path):
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("series: []\n", encoding="utf-8")
    assert load_macro_series(yaml_path) == []


def test_the_real_macro_series_yaml_in_the_repo_loads_and_has_unique_ids():
    configs = load_macro_series(REPO_YAML)
    assert len(configs) >= 8
    ids = [c.series_id for c in configs]
    assert len(ids) == len(set(ids))
    for c in configs:
        assert c.frequency in {"daily", "weekly", "monthly", "quarterly", "annual"}
        assert c.importance in {"high", "medium", "low"}
        assert c.source in {"fred", "bls", "bea"}


def test_the_real_macro_series_yaml_bea_entry_has_dataset_and_table_set():
    configs = load_macro_series(REPO_YAML)
    bea_configs = [c for c in configs if c.source == "bea"]
    assert bea_configs, "expected at least one BEA series in macro_series.yaml"
    for c in bea_configs:
        assert c.dataset and c.table and c.bea_frequency
