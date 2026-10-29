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


def test_missing_generator_command_has_normal_usage_exit(capsys):
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    assert "generate" in capsys.readouterr().err


def test_generation_os_failures_have_safe_diagnostics(monkeypatch, capsys):
    def denied(**kwargs):
        raise OSError("sensitive filesystem detail")

    monkeypatch.setattr("codapact.__main__.generate", denied)
    assert main(["generate", "api.yaml"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "generated project" in output.err.casefold()
    assert "sensitive" not in output.err
