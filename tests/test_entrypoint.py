from pathlib import Path

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
