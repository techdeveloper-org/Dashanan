"""Regression test for the DASH-STORY-005 (DSHN-55) P1 security review findings.

QA remediation-verification sub-task, attempt 1 of 2. This suite reproduces the exact
exploit the P1 review demonstrated against a REAL PostgreSQL 16 instance (spun up in an
ephemeral, disposable Docker container -- not mocked, not parsed as text) and proves that
exploit is now blocked by the Fix sub-task's two-layer remediation
(`trg_provenance_records_append_only` / `trg_episodic_entries_append_only` triggers plus
the new, per-zone `dashanan_provenance_role` / `dashanan_episodic_role`), applied to both:

  src/dashanan/infrastructure/provenance_schema.sql   (Zone 7, CRITICAL finding)
  src/dashanan/infrastructure/episodic_schema.sql     (Zone 2, HIGH finding)

Exploit reproduced, verbatim from the P1 review:
  "REVOKE UPDATE, DELETE ... FROM PUBLIC" does not bind a table's OWNER -- PostgreSQL
  grants the owner implicit ALL PRIVILEGES regardless of any REVOKE (only REASSIGN OWNED
  or ALTER TABLE ... OWNER TO changes that). Before the fix, the role that ran the
  CREATE TABLE (the "owner") could freely UPDATE or DELETE rows despite the bare REVOKE,
  because REVOKE ... FROM PUBLIC never touches the owner's implicit privileges. This test
  connects AS THE OWNER (the exact role class the REVOKE-only defense failed to bind) and
  proves the mutation is refused anyway -- proving the fix closes the gap at its root
  (a BEFORE UPDATE/DELETE trigger that rejects unconditionally, independent of who is
  privileged to run the statement) rather than merely adding a grant that a
  differently-privileged attacker could still route around.

HIGH-severity consequence reproduced: an attacker holding the application's ordinary DB
credentials could tamper a provenance/episodic row and recompute a self-consistent
`record_hash`, defeating `verify_chain`'s hash-chain tamper detection (see
`dashanan.domain.provenance_record.verify_chain`). This suite reproduces that exact
tamper-and-recompute UPDATE attempt and asserts it is rejected before the row can change,
independent of whether the recomputed hash would have been self-consistent.

Why Docker, not a text/AST parse of the .sql files: the Fix sub-task's own dev report
already confirmed (and this suite independently re-confirms via `test_smoke.py`'s
existing architecture-fitness sweep) that nothing in this repository parses .sql files'
text. A test that only greps for "CREATE TRIGGER" would pass even if the trigger function
had a typo that let mutations through, or if PostgreSQL rejected the DDL outright. Only
executing the real DDL against a real PostgreSQL engine and attempting the real exploit
proves the fix; this suite does exactly that and nothing less.

Isolation: every test in this module runs against a throwaway, ephemeral Postgres 16
container created fresh for this module's run and destroyed unconditionally in the
module-scoped fixture's teardown (`docker rm -f`), regardless of test outcome. No shared,
long-lived infrastructure (such as the `gst-copilot-postgres` container already running on
this machine for an unrelated project) is read, written, or in any way touched.

Skip conditions (this suite degrades to a documented skip, never a false pass, when its
prerequisite is unavailable):
  - `docker` is not on PATH, or the Docker daemon is not reachable.
  - The `pgvector/pgvector:pg16` image (a plain PostgreSQL 16 superset already present in
    this machine's local Docker image cache per this session's own `docker ps` check) is
    not available locally and cannot be pulled.
A skip here is explicitly NOT equivalent to the exploit being unverified in CI -- the
module docstring and skip reason both point back to this rationale so a reader never
mistakes "skipped" for "passed."
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_SCHEMA_SQL = (
    REPO_ROOT / "src" / "dashanan" / "infrastructure" / "provenance_schema.sql"
)
EPISODIC_SCHEMA_SQL = (
    REPO_ROOT / "src" / "dashanan" / "infrastructure" / "episodic_schema.sql"
)

PG_IMAGE = "pgvector/pgvector:pg16"
PG_DB = "dashanan_regression_test"
PG_USER = "postgres"

APPEND_ONLY_MESSAGE_FRAGMENT = "append-only"

# Fixed 64-hex-character values satisfying each table's `~ '^[0-9a-f]{64}$'` CHECK
# constraint (provenance_records.retrieval_context_hash is fixed; .record_hash is
# derived per-row in the `provenance_row` fixture since it must also be globally unique).
RETRIEVAL_CONTEXT_HASH = "b2" * 32
TAMPERED_RECORD_HASH = "c3" * 32


def _docker_available() -> bool:
    """Return whether the `docker` CLI is present and its daemon is reachable.

    Both conditions are required: a `docker` binary on PATH with a stopped or
    unreachable daemon still fails every subsequent `docker run`/`docker exec` call in
    this module, so checking `docker info` (not just `shutil.which`) is what actually
    determines whether the fixture can proceed.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _image_available(image: str) -> bool:
    """Return whether `image` already exists in the local Docker image cache."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


DOCKER_PREREQUISITES_MET = _docker_available() and _image_available(PG_IMAGE)

pytestmark = pytest.mark.skipif(
    not DOCKER_PREREQUISITES_MET,
    reason=(
        "Docker daemon or the pgvector/pgvector:pg16 image is unavailable on this "
        "machine -- see this module's docstring for why a real-Postgres exploit "
        "reproduction is required instead of a text/AST parse of the .sql files, and "
        "why this skip must never be read as the exploit having been verified closed."
    ),
)


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess command with a bounded timeout, capturing output as text."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 30), **kwargs
    )


def _psql(container: str, sql: str) -> subprocess.CompletedProcess:
    """Execute `sql` inside `container` as the PostgreSQL 16 table-owner role (`postgres`).

    The owner role is deliberately used everywhere in this suite -- it is the exact role
    class the pre-fix `REVOKE ... FROM PUBLIC` defense failed to bind (PostgreSQL grants
    the owner implicit ALL PRIVILEGES regardless of any REVOKE), so every mutation
    attempt below is the strongest form of the original exploit, not a weaker one.
    """
    return _run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            PG_USER,
            "-d",
            PG_DB,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
    )


def _psql_file(container: str, container_path: str) -> subprocess.CompletedProcess:
    """Execute the SQL script at `container_path` inside `container` via `psql -f`."""
    return _run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            PG_USER,
            "-d",
            PG_DB,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            container_path,
        ]
    )


@pytest.fixture(scope="module")
def pg_container():
    """Start a throwaway PostgreSQL 16 container, apply both remediated schemas to it.

    Yields the container name. Teardown (`docker rm -f`) runs unconditionally, even if
    schema application or an in-test assertion fails, so this suite never leaks a
    container on this machine regardless of outcome.
    """
    container_name = f"dash055-regression-{uuid.uuid4().hex[:12]}"

    run_result = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_PASSWORD={PG_USER}",
            "-e",
            f"POSTGRES_DB={PG_DB}",
            PG_IMAGE,
        ]
    )
    if run_result.returncode != 0:
        pytest.skip(
            f"Could not start ephemeral Postgres container for the DASH-055 "
            f"regression suite: {run_result.stderr.strip()}"
        )

    try:
        _wait_for_postgres_ready(container_name)

        for local_path, container_path in (
            (PROVENANCE_SCHEMA_SQL, "/tmp/provenance_schema.sql"),
            (EPISODIC_SCHEMA_SQL, "/tmp/episodic_schema.sql"),
        ):
            copy_result = _run(
                ["docker", "cp", str(local_path), f"{container_name}:{container_path}"]
            )
            assert copy_result.returncode == 0, (
                f"Failed to copy {local_path} into the regression container: "
                f"{copy_result.stderr}"
            )
            apply_result = _psql_file(container_name, container_path)
            assert apply_result.returncode == 0, (
                f"Applying {local_path.name} to a real PostgreSQL 16 instance failed -- "
                f"the remediated schema itself is broken, independent of this suite's "
                f"exploit reproduction: {apply_result.stderr}"
            )

        yield container_name
    finally:
        _run(["docker", "rm", "-f", container_name], timeout=30)


def _wait_for_postgres_ready(container: str, timeout_seconds: float = 60.0) -> None:
    """Poll until PostgreSQL in `container` accepts real connections and stays up.

    The official `postgres` (and `pgvector/pgvector`) image runs `initdb`, starts a
    throwaway internal server, stops it, then starts the real server -- `pg_isready`
    can report success during that first throwaway window, immediately followed by
    "the database system is shutting down". To avoid racing that restart cycle, this
    requires TWO CONSECUTIVE successful real queries (`SELECT 1`, not just
    `pg_isready`'s socket check) half a second apart before declaring readiness.
    """
    deadline = time.monotonic() + timeout_seconds
    consecutive_successes = 0
    while time.monotonic() < deadline:
        result = _run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                PG_USER,
                "-d",
                PG_DB,
                "-tAc",
                "SELECT 1;",
            ],
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            consecutive_successes += 1
            if consecutive_successes >= 2:
                return
        else:
            consecutive_successes = 0
        time.sleep(0.5)
    pytest.fail(
        f"PostgreSQL in container {container} did not become stably ready within "
        f"{timeout_seconds}s -- cannot run the DASH-055 regression suite."
    )


class ProvenanceRow(NamedTuple):
    """Identifiers of a baseline `provenance_records` row inserted for one test."""

    provenance_id: str
    record_hash: str


@pytest.fixture()
def provenance_row(pg_container: str) -> ProvenanceRow:
    """Insert one valid, constraint-satisfying `provenance_records` row as the owner.

    Both `provenance_id` and `record_hash` are derived fresh per test invocation (uuid4)
    so tests can run in any order without a leftover row from a prior test colliding on
    the primary key or the `uq_provenance_records_record_hash` unique constraint.
    """
    provenance_id = uuid.uuid4().hex
    # record_hash must satisfy `~ '^[0-9a-f]{64}$'` AND be globally unique
    # (uq_provenance_records_record_hash) -- derive a fresh 64-hex-char value per test
    # invocation from provenance_id itself so parallel/sequential fixture uses never
    # collide on the unique constraint.
    record_hash = (provenance_id * 2)[:64]
    insert_sql = f"""
        INSERT INTO provenance_records (
            tenant_id, provenance_id, item_id, source_zone, source_type,
            source_refs, write_timestamp, update_history, retrieval_context_hash,
            conflict_status, invalidation_flag, confidence, prev_hash, record_hash
        ) VALUES (
            'tenant-regression', '{provenance_id}', 'item-regression-1', 'episodic',
            'user_stated', '{{}}', '2026-01-01T00:00:00Z', '[]',
            '{RETRIEVAL_CONTEXT_HASH}', 'none', FALSE, 0.9, NULL,
            '{record_hash}'
        );
    """
    result = _psql(pg_container, insert_sql)
    assert result.returncode == 0, f"Baseline INSERT failed: {result.stderr}"
    return ProvenanceRow(provenance_id=provenance_id, record_hash=record_hash)


@pytest.fixture()
def episodic_row(pg_container: str) -> str:
    """Insert one valid, constraint-satisfying `episodic_entries` row as the owner.

    Returns the container name's episode_id for convenience; unique per test invocation
    for the same reason as `provenance_row`.
    """
    episode_id = uuid.uuid4().hex
    session_id = f"session-regression-{episode_id}"
    insert_sql = f"""
        INSERT INTO episodic_entries (
            tenant_id, session_id, episode_id, seq, occurred_at, written_at,
            payload, actors, token_count, state, score_terms
        ) VALUES (
            'tenant-regression', '{session_id}', '{episode_id}', 1,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z',
            'regression-test-payload', '{{}}', 10, 'active', '{{}}'
        );
    """
    result = _psql(pg_container, insert_sql)
    assert result.returncode == 0, f"Baseline INSERT failed: {result.stderr}"
    return episode_id


class TestProvenanceOwnerCannotMutateDespiteImplicitAllPrivileges:
    """Reproduces the CRITICAL finding against `provenance_records` (Zone 7).

    Every test in this class connects as the table OWNER -- the exact role class the
    original `REVOKE UPDATE, DELETE ... FROM PUBLIC` defense failed to bind, since
    PostgreSQL grants an owner implicit ALL PRIVILEGES regardless of any REVOKE. If the
    remediation had only been "add a REVOKE" (which was already present before the fix
    and already proven insufficient by the P1 review), every test below would fail
    (the UPDATE/DELETE would silently succeed as the owner). They pass only because the
    fix added a role-independent BEFORE UPDATE/DELETE trigger.
    """

    def test_owner_update_is_rejected(
        self, pg_container: str, provenance_row: ProvenanceRow
    ):
        """The CRITICAL exploit: owner-privileged UPDATE must be refused, not silently allowed."""
        update_sql = (
            f"UPDATE provenance_records SET confidence = 0.1 "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';"
        )
        result = _psql(pg_container, update_sql)

        assert result.returncode != 0, (
            "UPDATE as the table owner succeeded -- the append-only trigger did not "
            "block the mutation, so the CRITICAL finding is NOT fixed."
        )
        assert APPEND_ONLY_MESSAGE_FRAGMENT in result.stderr, (
            f"UPDATE was rejected, but not by the expected append-only trigger message. "
            f"stderr was: {result.stderr}"
        )

        select_result = _psql(
            pg_container,
            f"SELECT confidence FROM provenance_records "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';",
        )
        assert "0.9" in select_result.stdout, (
            "The row's confidence value changed despite the UPDATE being rejected -- "
            "the trigger is not actually preventing the mutation."
        )

    def test_owner_delete_is_rejected(
        self, pg_container: str, provenance_row: ProvenanceRow
    ):
        """The CRITICAL exploit's DELETE variant: owner-privileged DELETE must be refused."""
        delete_sql = (
            f"DELETE FROM provenance_records "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';"
        )
        result = _psql(pg_container, delete_sql)

        assert result.returncode != 0, (
            "DELETE as the table owner succeeded -- append-only enforcement does not "
            "hold for DELETE, so the CRITICAL finding is NOT fully fixed."
        )
        assert APPEND_ONLY_MESSAGE_FRAGMENT in result.stderr

        select_result = _psql(
            pg_container,
            f"SELECT provenance_id FROM provenance_records "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';",
        )
        assert provenance_row.provenance_id in select_result.stdout, (
            "The row is gone despite the DELETE being rejected -- the trigger is not "
            "actually preventing the mutation."
        )

    def test_tamper_and_recompute_hash_attack_is_rejected(
        self, pg_container: str, provenance_row: ProvenanceRow
    ):
        """Reproduces the HIGH-severity consequence: tamper a row + recompute record_hash.

        The original P1 review's HIGH finding was that an attacker holding the
        application's ordinary DB credentials could alter a row's content AND
        recompute a self-consistent `record_hash` for it, defeating `verify_chain`'s
        hash-chain tamper detection (`dashanan.domain.provenance_record.verify_chain`
        walks `prev_hash -> record_hash`; a self-consistent recomputed hash passes that
        walk). This test performs exactly that attack shape -- an UPDATE that changes
        both the payload-bearing column (confidence) and record_hash together, as an
        attacker recomputing a plausible replacement hash would -- and asserts it is
        rejected at the database before either column can change, which is what makes
        the hash chain's tamper detection meaningful again: the row literally cannot be
        altered by anyone connecting with ordinary (even owner-equivalent) DB
        credentials, so there is nothing left for `verify_chain` to be tricked by.
        """
        tamper_sql = (
            f"UPDATE provenance_records "
            f"SET confidence = 0.01, record_hash = '{TAMPERED_RECORD_HASH}' "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';"
        )
        result = _psql(pg_container, tamper_sql)

        assert result.returncode != 0, (
            "A tamper-and-recompute-hash UPDATE succeeded -- the hash chain's tamper "
            "detection guarantee is still defeatable by anyone with the application's "
            "DB credentials, which is precisely the HIGH finding this fix was supposed "
            "to close."
        )
        assert APPEND_ONLY_MESSAGE_FRAGMENT in result.stderr

        select_result = _psql(
            pg_container,
            f"SELECT record_hash FROM provenance_records "
            f"WHERE provenance_id = '{provenance_row.provenance_id}';",
        )
        assert provenance_row.record_hash in select_result.stdout
        assert TAMPERED_RECORD_HASH not in select_result.stdout, (
            "The tampered record_hash was persisted despite the UPDATE being rejected."
        )


class TestEpisodicOwnerCannotMutateDespiteImplicitAllPrivileges:
    """Reproduces the HIGH finding against `episodic_entries` (Zone 2) -- the review's
    statement that "the identical defect exists in episodic_schema.sql". Mirrors the
    provenance test class exactly, against the second remediated file.
    """

    def test_owner_update_is_rejected(self, pg_container: str, episodic_row: str):
        update_sql = (
            f"UPDATE episodic_entries SET payload = 'tampered-payload' "
            f"WHERE episode_id = '{episodic_row}';"
        )
        result = _psql(pg_container, update_sql)

        assert result.returncode != 0, (
            "UPDATE as the table owner succeeded against episodic_entries -- the "
            "identical CRITICAL-class gap the review found in provenance_schema.sql is "
            "still open in episodic_schema.sql."
        )
        assert APPEND_ONLY_MESSAGE_FRAGMENT in result.stderr

        select_result = _psql(
            pg_container,
            f"SELECT payload FROM episodic_entries WHERE episode_id = '{episodic_row}';",
        )
        assert "tampered-payload" not in select_result.stdout

    def test_owner_delete_is_rejected(self, pg_container: str, episodic_row: str):
        delete_sql = f"DELETE FROM episodic_entries WHERE episode_id = '{episodic_row}';"
        result = _psql(pg_container, delete_sql)

        assert result.returncode != 0, (
            "DELETE as the table owner succeeded against episodic_entries."
        )
        assert APPEND_ONLY_MESSAGE_FRAGMENT in result.stderr

        select_result = _psql(
            pg_container,
            f"SELECT episode_id FROM episodic_entries WHERE episode_id = '{episodic_row}';",
        )
        assert episodic_row in select_result.stdout


class TestDistinctLeastPrivilegedRoleNowExists:
    """Proves must-not-deviate item 1's other half: a distinct, least-privileged,
    PER-ZONE role now genuinely exists for each schema (the P1 review's repo-wide grep for
    `CREATE ROLE`/`CREATE USER`/`GRANT` found zero matches before this fix), and that each
    is granted only SELECT and INSERT -- never UPDATE or DELETE -- on its OWN zone's table
    only, so a deployment that connects as this role (rather than as the owner) has no
    privilege-level path to UPDATE/DELETE either, independent of the trigger.

    DSHN-60 P1 remediation note: attempt 1's `dashanan_app_role` was a single role shared
    across both `provenance_records` and `episodic_entries` -- a credential granted
    membership in it for one zone's write path was, by construction, already privileged to
    SELECT/INSERT the OTHER zone's table too, with no schema-level control isolating the
    two. This suite now proves each zone has its OWN role (`dashanan_provenance_role` for
    Zone 7, `dashanan_episodic_role` for Zone 2) and that neither role is granted anything
    on the other zone's table.
    """

    @pytest.mark.parametrize(
        "role_name", ["dashanan_provenance_role", "dashanan_episodic_role"]
    )
    def test_role_exists_as_nologin(self, pg_container: str, role_name: str):
        result = _psql(
            pg_container,
            f"SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = '{role_name}';",
        )
        assert result.returncode == 0
        assert role_name in result.stdout
        assert " f" in result.stdout, (
            f"{role_name} must be NOLOGIN (rolcanlogin = false); the application "
            "is expected to be granted membership in it via a separate login role, not "
            "to log in as this role directly."
        )

    @pytest.mark.parametrize(
        ("role_name", "table_name"),
        [
            ("dashanan_provenance_role", "provenance_records"),
            ("dashanan_episodic_role", "episodic_entries"),
        ],
    )
    def test_role_has_only_select_and_insert_on_its_own_zone(
        self, pg_container: str, role_name: str, table_name: str
    ):
        result = _psql(
            pg_container,
            "SELECT privilege_type FROM information_schema.role_table_grants "
            f"WHERE grantee = '{role_name}' AND table_name = '{table_name}' "
            "ORDER BY privilege_type;",
        )
        assert result.returncode == 0
        granted = {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and line.strip() not in ("privilege_type", "")
            and "---" not in line
            and "rows)" not in line.lower()
            and "row)" not in line.lower()
        }
        assert granted == {"INSERT", "SELECT"}, (
            f"{role_name}'s privileges on {table_name} were {granted}, expected "
            f"exactly {{'INSERT', 'SELECT'}} -- any UPDATE/DELETE grant here would "
            f"reopen must-not-deviate item 1 even with the trigger in place."
        )

    @pytest.mark.parametrize(
        ("role_name", "other_zone_table"),
        [
            ("dashanan_provenance_role", "episodic_entries"),
            ("dashanan_episodic_role", "provenance_records"),
        ],
    )
    def test_role_has_no_privileges_on_the_other_zones_table(
        self, pg_container: str, role_name: str, other_zone_table: str
    ):
        """DSHN-60 P1 remediation: proves the per-zone role split is real isolation,
        not merely a rename -- a role provisioned for one zone must hold ZERO grants
        on the other zone's table."""
        result = _psql(
            pg_container,
            "SELECT privilege_type FROM information_schema.role_table_grants "
            f"WHERE grantee = '{role_name}' AND table_name = '{other_zone_table}';",
        )
        assert result.returncode == 0
        granted = {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and line.strip() not in ("privilege_type", "")
            and "---" not in line
            and "rows)" not in line.lower()
            and "row)" not in line.lower()
        }
        assert granted == set(), (
            f"{role_name} unexpectedly holds {granted} on {other_zone_table} -- "
            "per-zone roles must never be granted anything on a sibling zone's table."
        )
