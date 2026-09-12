"""What ``bin/local-env`` writes, refuses, and — above all — never says out loud.

**The honest limit on this file**, stated here rather than discovered later: the labelled
secrets and the service account do not exist yet (tadasant-internal#2804), so nothing here
has run against real Secret Manager and nothing here can. What is pinned is everything on
this side of that seam — the key file read, the label filter, the rendering, the override
block, the refusal to clobber, the mode, and the guard that matters most, which is that no
secret value ever reaches stdout or stderr.

:class:`FakeSecrets` is the seam's fake, in the same sense as every other fake in this
repo: it implements :class:`~tools.local_env.SecretReader` honestly and records what it was
asked for, so a test can assert the *filter* was applied by checking that nothing unlabelled
was ever requested.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

import tools.local_env
from tools.local_env import (
    LABEL_FILTER,
    LOCAL_OVERRIDES,
    OVERRIDE_NAMES,
    RESERVED_NAMES,
    UNSET_NAMES,
    LocalEnvError,
    Secret,
    _render_value,
    collect,
    main,
    project_id_from_key,
    render,
    run,
    write,
)

#: Values shaped like the things this actually carries — an API key, a KMS key path, an
#: OAuth client secret, a DSN. Every one of them is asserted absent from the terminal.
SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-v1-000000000000000000000000000000",
    "CARTESIA_API_KEY": "sk_car_111111111111111111111111",
    "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-2222222222222222222",
    "GLITCHTIP_DSN": "https://33333333@glitchtip.example/4",
    "MOTET_ALLOWED_EMAILS": "someone@example.test",
}


class FakeSecrets:
    """A :class:`~tools.local_env.SecretReader` over a dict, recording what it was asked."""

    def __init__(self, values: dict[str, str], *, unlabelled: dict[str, str] | None = None) -> None:
        self._values = dict(values)
        self._unlabelled = dict(unlabelled or {})
        self.accessed: list[str] = []
        self.projects: list[str] = []

    def labelled(self, project_id: str) -> Sequence[str]:
        self.projects.append(project_id)
        # Deliberately reversed: `collect` must not rely on the API's ordering.
        return sorted(self._values, reverse=True)

    def value(self, project_id: str, name: str) -> str:
        self.accessed.append(name)
        if name in self._values:
            return self._values[name]
        if name in self._unlabelled:
            raise AssertionError(f"asked for {name}, which does not carry the label")
        raise AssertionError(f"asked for {name}, which does not exist")


def _key_file(tmp_path: Path, **fields: object) -> Path:
    payload: dict[str, object] = {
        "type": "service_account",
        "project_id": "a-project-id",
        "client_email": "svc@example.iam.gserviceaccount.com",
    }
    payload.update(fields)
    path = tmp_path / "local-dev.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _key_file(tmp_path)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    return path


class TestTheProjectComesFromTheKey:
    """Nothing in this repo names the project, so the key file has to."""

    def test_read_from_the_key_files_own_field(self, key: Path) -> None:
        assert project_id_from_key() == "a-project-id"

    def test_a_tilde_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        (home / ".config" / "motet").mkdir(parents=True)
        (home / ".config" / "motet" / "local-dev.json").write_text(
            json.dumps({"project_id": "from-home"}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "~/.config/motet/local-dev.json")
        assert project_id_from_key() == "from-home"

    def test_unset_says_so_and_says_a_human_mints_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        with pytest.raises(LocalEnvError, match="GOOGLE_APPLICATION_CREDENTIALS is not set"):
            project_id_from_key()

    def test_empty_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "   ")
        with pytest.raises(LocalEnvError, match="is not set"):
            project_id_from_key()

    def test_a_missing_file_names_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "nope.json"))
        with pytest.raises(LocalEnvError, match="cannot read the key file"):
            project_id_from_key()

    def test_not_json_is_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "local-dev.json"
        path.write_text("not json at all", encoding="utf-8")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
        with pytest.raises(LocalEnvError, match="is not JSON"):
            project_id_from_key()

    def test_json_that_is_not_an_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "local-dev.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
        with pytest.raises(LocalEnvError, match="not a JSON object"):
            project_id_from_key()

    def test_a_credential_with_no_project_id_says_which_kind_has_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "local-dev.json"
        path.write_text(json.dumps({"type": "authorized_user"}), encoding="utf-8")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
        with pytest.raises(LocalEnvError, match="has no project_id"):
            project_id_from_key()


class TestTheLabelIsTheRoster:
    """The script discovers which secrets to read; it never restates the list."""

    def test_only_labelled_secrets_are_accessed(self) -> None:
        reader = FakeSecrets(SECRETS, unlabelled={"SOMETHING_ELSE": "no"})
        secrets, dropped = collect(reader, "a-project-id")
        assert [s.name for s in secrets] == sorted(SECRETS)
        assert sorted(reader.accessed) == sorted(SECRETS)
        assert dropped == []
        assert reader.projects == ["a-project-id"]

    def test_the_real_reader_asks_secret_manager_for_that_label(self) -> None:
        assert LABEL_FILTER == "labels.motet-local=true"

    def test_an_empty_roster_points_at_the_private_half(self) -> None:
        with pytest.raises(LocalEnvError, match="motet-local=true"):
            collect(FakeSecrets({}), "a-project-id")

    def test_a_secret_that_cannot_be_an_env_var_name_is_named_not_written(self) -> None:
        with pytest.raises(LocalEnvError, match="not a usable"):
            collect(FakeSecrets({"not-an-env-name": "x"}), "a-project-id")

    def test_a_labelled_secret_that_a_local_override_owns_is_dropped(self) -> None:
        reader = FakeSecrets({**SECRETS, "MOTET_API_TOKEN": "staging-bearer"})
        secrets, dropped = collect(reader, "a-project-id")
        assert dropped == ["MOTET_API_TOKEN"]
        assert "MOTET_API_TOKEN" not in {s.name for s in secrets}
        # Never even read: a value that cannot be written should not be fetched.
        assert "MOTET_API_TOKEN" not in reader.accessed

    def test_nothing_left_after_dropping_is_an_error_rather_than_an_empty_file(self) -> None:
        with pytest.raises(LocalEnvError, match="every labelled secret collides"):
            collect(FakeSecrets({"MOTET_INFERENCE_MODE": "real"}), "a-project-id")

    def test_a_trailing_newline_from_a_pasted_secret_is_trimmed(self) -> None:
        secrets, _ = collect(FakeSecrets({"OPENROUTER_API_KEY": "sk-or-v1-abc\n"}), "p")
        assert secrets[0].value == "sk-or-v1-abc"


class TestRendering:
    def test_name_equals_value_lines_in_a_stable_order(self) -> None:
        text = render([Secret(name, SECRETS[name]) for name in sorted(SECRETS)])
        lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        assert lines[: len(SECRETS)] == [f"{name}={SECRETS[name]}" for name in sorted(SECRETS)]

    def test_a_value_needing_quotes_survives_a_round_trip(self) -> None:
        tricky = 'a b"c\\d$E\nsecond'
        rendered = _render_value(tricky)
        assert rendered == '"a b\\"c\\\\d\\$E\\nsecond"'

    def test_an_ordinary_value_is_written_bare(self) -> None:
        assert _render_value("sk-or-v1-abc/DEF+_=") == "sk-or-v1-abc/DEF+_="

    def test_the_override_block_is_appended_verbatim(self) -> None:
        text = render([Secret("OPENROUTER_API_KEY", "x")])
        assert text.endswith(LOCAL_OVERRIDES)

    def test_every_override_the_block_sets_is_in_override_names(self) -> None:
        """The block and the reserved list cannot drift, because this reads both."""
        assigned = [
            line.split("=", 1)[0]
            for line in LOCAL_OVERRIDES.splitlines()
            if line and not line.startswith("#") and "=" in line
        ]
        assert assigned == list(OVERRIDE_NAMES)

    def test_the_block_holds_the_values_the_issue_specifies(self) -> None:
        assigned = dict(
            line.split("=", 1)
            for line in LOCAL_OVERRIDES.splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        assert assigned == {
            "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/motet_dev",
            "MOTET_INFERENCE_MODE": "real",
            "MOTET_VAULT_BACKEND": "kms",
            "MOTET_STORAGE_BACKEND": "local",
            "MOTET_STORAGE_DIR": ".motet-storage",
            "MOTET_PUBLIC_BASE_URL": "http://localhost:8000",
            "OTEL_SERVICE_NAME": "motet-local",
            "OTEL_RESOURCE_ATTRIBUTES": "service.version=local",
            "MOTET_VOICE_API_BASE_URL": "http://localhost:8000",
        }

    def test_the_three_unset_variables_are_named_and_left_unset(self) -> None:
        """Unset means unset: mentioned in a comment, assigned nowhere, and reserved."""
        for name in UNSET_NAMES:
            assert f"{name}=" not in LOCAL_OVERRIDES
            assert name in LOCAL_OVERRIDES
        assert RESERVED_NAMES == frozenset(OVERRIDE_NAMES) | frozenset(UNSET_NAMES)


class TestWriting:
    def test_it_refuses_to_overwrite_without_force(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=me\n", encoding="utf-8")
        with pytest.raises(LocalEnvError, match="--force"):
            write(path, "NEW=value\n", force=False)
        assert path.read_text(encoding="utf-8") == "KEEP=me\n"

    def test_force_replaces_it(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=me\n", encoding="utf-8")
        write(path, "NEW=value\n", force=True)
        assert path.read_text(encoding="utf-8") == "NEW=value\n"

    def test_the_file_is_0600_whatever_the_umask(self, tmp_path: Path) -> None:
        previous = os.umask(0)
        try:
            path = tmp_path / ".env"
            write(path, "NEW=value\n", force=False)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            os.umask(previous)

    def test_force_also_leaves_it_0600(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEEP=me\n", encoding="utf-8")
        path.chmod(0o644)
        write(path, "NEW=value\n", force=True)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestTheWholeScript:
    def test_it_writes_the_file_and_says_what_it_wrote(
        self, key: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / ".env"
        assert run(["--output", str(out)], FakeSecrets(SECRETS)) == 0
        written = out.read_text(encoding="utf-8")
        for name, value in SECRETS.items():
            assert f"{name}={value}" in written
        assert written.endswith(LOCAL_OVERRIDES)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

        captured = capsys.readouterr()
        assert f"Wrote {out}" in captured.out
        assert "OPENROUTER_API_KEY" in captured.out

    def test_no_secret_value_ever_reaches_a_terminal(
        self, key: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The guard that matters most, asserted over stdout AND stderr, twice.

        Once on the success path, and once on the failure path where a naive
        implementation would put the value in an exception message.
        """
        out = tmp_path / ".env"
        assert run(["--output", str(out)], FakeSecrets(SECRETS)) == 0
        first = capsys.readouterr()

        assert main(["--output", str(out)]) == 2  # refuses, because it now exists
        second = capsys.readouterr()

        for stream in (first.out, first.err, second.out, second.err):
            for value in SECRETS.values():
                assert value not in stream

    def test_a_second_run_refuses_and_explains(
        self, key: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / ".env"
        assert run(["--output", str(out)], FakeSecrets(SECRETS)) == 0
        capsys.readouterr()
        with pytest.raises(LocalEnvError, match="--force"):
            run(["--output", str(out)], FakeSecrets(SECRETS))

    def test_force_rewrites_it(self, key: Path, tmp_path: Path) -> None:
        out = tmp_path / ".env"
        run(["--output", str(out)], FakeSecrets(SECRETS))
        rotated = {**SECRETS, "OPENROUTER_API_KEY": "sk-or-v1-rotated"}
        assert run(["--output", str(out), "--force"], FakeSecrets(rotated)) == 0
        assert "OPENROUTER_API_KEY=sk-or-v1-rotated" in out.read_text(encoding="utf-8")

    def test_a_dropped_secret_is_reported_by_name(
        self, key: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / ".env"
        reader = FakeSecrets({**SECRETS, "MOTET_DRAIN_TRIGGER": "true"})
        assert run(["--output", str(out)], reader) == 0
        assert "MOTET_DRAIN_TRIGGER" in capsys.readouterr().out
        assert "MOTET_DRAIN_TRIGGER=" not in out.read_text(encoding="utf-8")

    def test_a_missing_key_exits_2_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        out = tmp_path / ".env"
        assert main(["--output", str(out)]) == 2
        assert not out.exists()
        assert "GOOGLE_APPLICATION_CREDENTIALS" in capsys.readouterr().err

    def test_nothing_names_the_project_but_the_key(self, key: Path, tmp_path: Path) -> None:
        """The project id is topology: it goes into no file this repo writes, and no report."""
        out = tmp_path / ".env"
        run(["--output", str(out)], FakeSecrets(SECRETS))
        assert "a-project-id" not in out.read_text(encoding="utf-8")
        source = Path(tools.local_env.__file__).read_text(encoding="utf-8")
        assert "a-project-id" not in source
