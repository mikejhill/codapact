"""Reference amplification must remain bounded before serialization or code generation."""

import pytest

from codapact.errors import CodaError
from codapact.loading import resolve


@pytest.mark.parametrize("kind", ["value", "key"])
@pytest.mark.parametrize("references", [6, 9])
def test_reference_text_budget_counts_repeated_values_and_keys(kind, references):
    text = "x" * 1_000_000
    leaf = {"value": text} if kind == "value" else {text: 1}
    source = {"leaf": leaf, "uses": [{"$ref": "#/leaf"} for _ in range(references)]}
    if references == 6:
        result = resolve(source)
        assert len(result["uses"]) == references
        assert all(value == leaf for value in result["uses"])
    else:
        with pytest.raises(CodaError, match="(?i)expansion text limit"):
            resolve(source)
