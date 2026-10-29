from pathlib import Path

import pytest

from codapact import __version__
from codapact.__main__ import main


def test_generate_entrypoint(tmp_path, capsys):
    source = Path(__file__).resolve().parents[1] / "examples/pets.yaml"
    assert (
        main(
            [
                "generate",
                str(source),
                "--output",
                str(tmp_path / "client"),
                "--package",
                "example_pets",
            ]
        )
        == 0
    )
    assert "Generated example_pets" in capsys.readouterr().out
    assert (
        main(["generate", str(source), "--output", str(tmp_path / "bad"), "--package", "../bad"])
        == 2
    )
    assert "safe lowercase" in capsys.readouterr().err
    assert (
        main(
            [
                "generate",
                str(source),
                "--output",
                str(tmp_path / "remote"),
                "--package",
                "pets",
                "--cli-definition",
                "https://example.com/cli",
            ]
        )
        == 2
    )


def test_generator_identity_and_version_are_discoverable(capsys):
    with pytest.raises(SystemExit) as version:
        main(["--version"])
    assert version.value.code == 0
    output = capsys.readouterr().out
    assert "codapact" in output.casefold() and __version__ in output
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    output = capsys.readouterr().out.casefold()
    assert "contracts into callable tools" in output and "generate" in output
