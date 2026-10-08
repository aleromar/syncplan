import pytest

from syncplan import cli


def test_setup_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("syncplan ")


def test_setup_no_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: syncplan" in capsys.readouterr().out
