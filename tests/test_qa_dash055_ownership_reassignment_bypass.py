"""Regression test for the DASH-STORY-005 (DSHN-55) P1 re-review findings, attempt 2.

QA remediation-verification sub-task. `test_regression_dash055_p1_append_only_privilege_bypass.py`
already proves a table-owner-class role cannot directly UPDATE/DELETE against
`provenance_records` / `episodic_entries` (the attempt-1 trigger). This suite proves the
NARROWER, attempt-2-specific gap the re-review found and the Fix sub-task's ownership
reassignment (control 3: `ALTER TABLE ... OWNER TO dashanan_schema_owner` +
`ALTER FUNCTION ... OWNER TO dashanan_schema_owner`) is meant to close: that an
append-only BEFORE UPDATE/DELETE trigger, by itself, does nothing to stop the table
OWNER from disabling or replacing the trigger and then mutating freely.

Exploit reproduced, verbatim from the P1 re-review (both CRITICAL on Zone 7 / HIGH on
Zone 2, since the identical pattern exists in both schema files):

  1. `ALTER TABLE ... DISABLE TRIGGER <name>;` requires only the ALTER privilege on the
     table, which PostgreSQL grants IMPLICITLY and UNREVOKABLY to the table OWNER. Before
     attempt 2's ownership reassignment, the role that ran `CREATE TABLE` (the default
     connecting credential in any deployment that never separately provisions a distinct
     service role) remained the owner forever, so it could always disable the append-only
     trigger and then freely UPDATE/DELETE rows -- defeating the attempt-1 trigger
     entirely and falsifying must-not-deviate item 1 ("No UPDATE grant exists on the
     Zone 7 table for ANY service role") for that role class.

  2. `CREATE OR REPLACE FUNCTION <trigger_function>() ...` (turning the trigger function
     into a no-op) requires only ownership of the FUNCTION, which is independent of table
     ownership in PostgreSQL. This is a STRICTLY EASIER bypass than (1) -- it never
     touches the table's DDL at all -- and the re-review noted attempt 1 left the
     function owned by the migration role too, leaving this second bypass open even if
     someone had hardened (1) alone.

CRITICAL METHODOLOGY NOTE -- why this suite does NOT connect as `postgres`: PostgreSQL
superusers bypass every ownership and privilege check, including `ALTER TABLE ...
DISABLE TRIGGER` and `CREATE OR REPLACE FUNCTION` on an object they do not own. Running
this suite's exploit attempts as `postgres` (the Docker image's default superuser) would
therefore ALWAYS succeed regardless of whether control 3's ownership reassignment ran
correctly, making the test worthless as a regression check (this was confirmed
empirically while authoring this suite: an earlier draft that ran the exploit as
`postgres` reported both bypasses as still open even immediately after a successful
`OWNER TO dashanan_schema_owner` reassignment, purely because superuser status makes
ownership irrelevant -- not because the fix was broken). Instead, mirroring the P1
re-review's own adversarial setup (a "non-superuser, CREATEROLE-holding, database-owning
role" -- and a realistic shape for a cloud-managed-Postgres "master user" that many
deployments connect their application through directly), this suite creates a dedicated
non-superuser role (`dash_migration_owner`) that:
  - is granted CREATEROLE and CREATEDB (so it can run this repo's migration files,
    which themselves create `dashanan_app_role` / `dashanan_schema_owner` and grant
    itself brief membership in the latter),
  - owns the target database,
and runs BOTH the schema-apply migration AND every exploit attempt as that same role --
the exact role class both bypasses require and the exact role class control 3's ownership
reassignment (which runs as whatever role executes the migration file) is designed to
strip ownership away from.

This suite connects as `dash_migration_owner` for both schema application and every
exploit attempt, and proves BOTH exploit attempts are now refused, because control 3's
`OWNER TO dashanan_schema_owner` reassignment (on both the table and the trigger
function) has already run by the time this suite's schema-apply fixture returns, leaving
the connecting role a NON-owner of both objects.

Why this is a DISTINCT regression test from `test_regression_dash055_p1_append_only_
privilege_bypass.py` rather than a duplicate: that suite proves the trigger itself
rejects a direct UPDATE/DELETE attempt (attempt 1's control), connecting as the Docker
image's superuser -- which is a valid way to test the trigger (BEFORE triggers fire
unconditionally, including for superusers, since they are not privilege-gated) but is
NOT a valid way to test ownership-gated DDL like DISABLE TRIGGER or CREATE OR REPLACE
FUNCTION, per the methodology note above. This suite proves the trigger cannot be
disabled or defanged out from under a row in the first place (attempt 2's control) using
the non-superuser role class that check actually requires -- a materially different
attack surface and connection identity that a green run of the first suite alone does
NOT exercise.

Isolation: every test in this module runs against a throwaway, ephemeral Postgres 16
container created fresh for this module's run and destroyed unconditionally in the
module-scoped fixture's teardown (`docker rm -f`), regardless of test outcome. No shared,
long-lived infrastructure is read, written, or in any way touched.

Skip conditions (this suite degrades to a documented skip, never a false pass, when its
prerequisite is unavailable):
  - `docker` is not on PATH, or the Docker daemon is not reachable.
  - The `pgvector/pgvector:pg16` image is not available locally and cannot be pulled.
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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_SCHEMA_SQL = (
    REPO_ROOT / "src" / "dashanan" / "infrastructure" / "provenance_schema.sql"
)
EPISODIC_SCHEMA_SQL = (
    REPO_ROOT / "src" / "dashanan" / "infrastructure" / "episodic_schema.sql"
)

PG_IMAGE = "pgvector/pgvector:pg16"
PG_SUPERUSER = "postgres"
PG_SUPERUSER_PASSWORD = "postgres"
PG_DB = "dashanan_ownership_regression_test"

# The adversarial, non-superuser role every exploit attempt in this suite connects as --
# see the module docstring's methodology note for why `postgres` (superuser) would make
# this suite worthless.
MIGRATION_ROLE = "dash_migration_owner"
MIGRATION_ROLE_PASSWORD = "dash_migration_owner_pw"

OWNERSHIP_ERROR_FRAGMENT = "must be owner of"


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


def _psql_as(container: str, role: str, password: str, sql: str) -> subprocess.CompletedProcess:
    """Execute `sql` inside `container`, connecting as `role` (authenticated via
    `PGPASSWORD`, not the trust-authenticated superuser socket), against `PG_DB`.
    """
    return _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-h",
            "localhost",
            "-U",
            role,
            "-d",
            PG_DB,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
    )


def _psql_file_as(
    container: str, role: str, password: str, container_path: str
) -> subprocess.CompletedProcess:
    """Execute the SQL script at `container_path` inside `container` via `psql -f`,
    connecting as `role`.
    """
    return _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-h",
            "localhost",
            "-U",
            role,
            "-d",
            PG_DB,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            container_path,
        ]
    )


def _psql(container: str, sql: str) -> subprocess.CompletedProcess:
    """Execute `sql` as `MIGRATION_ROLE` -- the sole connection identity every
    exploit-attempt test in this suite uses (see module docstring's methodology note).
    """
    return _psql_as(container, MIGRATION_ROLE, MIGRATION_ROLE_PASSWORD, sql)


def _psql_baseline_insert(container: str, sql: str) -> subprocess.CompletedProcess:
    """Execute a baseline row `INSERT` as the Postgres superuser.

    Only used to seed a row BEFORE an exploit attempt runs -- never for the exploit
    attempt itself (which always connects as `MIGRATION_ROLE`, per the module
    docstring's methodology note). `MIGRATION_ROLE` has neither table ownership nor an
    explicit grant after control 2/3 run (it is deliberately left with no privileges
    on these tables at all, which is stronger than the pre-fix state, not a test
    artifact), so seeding must use a role that still has INSERT. Using the superuser
    here is safe because seeding a row is not part of either exploit under test --
    only the subsequent ALTER TABLE / CREATE OR REPLACE FUNCTION attempt, and the
    UPDATE that follows it, run as the non-superuser role.
    """
    return _psql_as(container, PG_SUPERUSER, PG_SUPERUSER_PASSWORD, sql)


@pytest.fixture(scope="module")
def pg_container():
    """Start a throwaway PostgreSQL 16 container, create the adversarial non-superuser
    `MIGRATION_ROLE`, and apply both remediated schemas AS THAT ROLE (not as the
    superuser) so control 3's ownership reassignment runs against the exact role class
    the P1 re-review's exploit requires.

    Yields the container name. Teardown (`docker rm -f`) runs unconditionally, even if
    schema application or an in-test assertion fails, so this suite never leaks a
    container on this machine regardless of outcome.
    """
    container_name = f"dash055-ownership-regression-{uuid.uuid4().hex[:12]}"

    run_result = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_PASSWORD={PG_SUPERUSER_PASSWORD}",
            "-e",
            f"POSTGRES_DB={PG_DB}",
            PG_IMAGE,
        ]
    )
    if run_result.returncode != 0:
        pytest.skip(
            f"Could not start ephemeral Postgres container for the DASH-055 "
            f"ownership-reassignment regression suite: {run_result.stderr.strip()}"
        )

    try:
        _wait_for_postgres_ready(container_name)

        # Create the adversarial role as the superuser (bootstrap only -- the superuser
        # is never used again after this block). CREATEROLE + CREATEDB mirrors the P1
        # re-review's own adversarial setup and a realistic cloud-managed-Postgres
        # "master user" shape; it is what lets this role run the migration files
        # (which create dashanan_app_role / dashanan_schema_owner and briefly grant
        # itself membership in the latter).
        create_role_sql = (
            f"CREATE ROLE {MIGRATION_ROLE} LOGIN CREATEROLE CREATEDB "
            f"PASSWORD '{MIGRATION_ROLE_PASSWORD}';"
        )
        create_role_result = _run(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={PG_SUPERUSER_PASSWORD}",
                container_name,
                "psql",
                "-h",
                "localhost",
                "-U",
                PG_SUPERUSER,
                "-d",
                PG_DB,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                create_role_sql,
            ]
        )
        assert create_role_result.returncode == 0, (
            f"Failed to create the adversarial non-superuser role {MIGRATION_ROLE}: "
            f"{create_role_result.stderr}"
        )

        make_db_owner_result = _run(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={PG_SUPERUSER_PASSWORD}",
                container_name,
                "psql",
                "-h",
                "localhost",
                "-U",
                PG_SUPERUSER,
                "-d",
                PG_DB,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"ALTER DATABASE {PG_DB} OWNER TO {MIGRATION_ROLE};",
            ]
        )
        assert make_db_owner_result.returncode == 0, (
            f"Failed to make {MIGRATION_ROLE} the owner of {PG_DB}: "
            f"{make_db_owner_result.stderr}"
        )

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
            # Apply the schema AS THE ADVERSARIAL ROLE, not the superuser -- this is
            # what makes control 3's ownership reassignment (ALTER TABLE/FUNCTION ...
            # OWNER TO dashanan_schema_owner) actually run against MIGRATION_ROLE as
            # the role losing ownership, matching the P1 re-review's exploit precisely.
            apply_result = _psql_file_as(
                container_name, MIGRATION_ROLE, MIGRATION_ROLE_PASSWORD, container_path
            )
            assert apply_result.returncode == 0, (
                f"Applying {local_path.name} to a real PostgreSQL 16 instance as the "
                f"adversarial non-superuser role failed -- the remediated schema "
                f"itself is broken, independent of this suite's exploit reproduction: "
                f"{apply_result.stderr}"
            )

        yield container_name
    finally:
        _run(["docker", "rm", "-f", container_name], timeout=30)


def _wait_for_postgres_ready(container: str, timeout_seconds: float = 60.0) -> None:
    """Poll until PostgreSQL in `container` accepts real connections and stays up.

    Requires TWO CONSECUTIVE successful real queries half a second apart before
    declaring readiness, to avoid racing the official image's internal restart cycle
    during `initdb`.
    """
    deadline = time.monotonic() + timeout_seconds
    consecutive_successes = 0
    while time.monotonic() < deadline:
        result = _run(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={PG_SUPERUSER_PASSWORD}",
                container,
                "psql",
                "-h",
                "localhost",
                "-U",
                PG_SUPERUSER,
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
        f"{timeout_seconds}s -- cannot run the DASH-055 ownership-reassignment "
        f"regression suite."
    )


class TestOwnershipReassignmentClosesDisableTriggerBypass:
    """Reproduces the re-review's CRITICAL (Zone 7) / HIGH (Zone 2) finding: the
    migration role, if left as table owner, can disable the append-only trigger via
    `ALTER TABLE ... DISABLE TRIGGER` (a privilege table-level REVOKE cannot touch) and
    then mutate rows freely, completely defeating the attempt-1 trigger.
    """

    def test_migration_role_is_not_provenance_records_owner(self, pg_container: str):
        """Preconditions check: control 3 must have actually reassigned ownership away
        from `MIGRATION_ROLE` -- everything below depends on this having happened, so
        this test fails loudly and specifically if it did not.
        """
        result = _psql(
            pg_container,
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE relname = 'provenance_records';",
        )
        assert result.returncode == 0
        assert MIGRATION_ROLE not in result.stdout, (
            f"provenance_records is still owned by the migration role "
            f"'{MIGRATION_ROLE}' -- control 3's ALTER TABLE ... OWNER TO "
            f"dashanan_schema_owner did not take effect (see the schema file's own "
            f"RAISE WARNING branch for why it can legitimately skip: the migration "
            f"role must own schema \"public\" or hold CREATE ... WITH GRANT OPTION "
            f"on it). Every exploit-attempt test below depends on this reassignment "
            f"having succeeded."
        )
        assert "dashanan_schema_owner" in result.stdout, (
            f"Expected provenance_records to be owned by dashanan_schema_owner, "
            f"got: {result.stdout.strip()}"
        )

    def test_migration_role_cannot_disable_provenance_append_only_trigger(
        self, pg_container: str
    ):
        """The CRITICAL exploit itself: `ALTER TABLE ... DISABLE TRIGGER` as the
        non-superuser migration role must now be refused with an ownership error, not
        silently succeed.
        """
        result = _psql(
            pg_container,
            "ALTER TABLE provenance_records "
            "DISABLE TRIGGER trg_provenance_records_append_only;",
        )
        assert result.returncode != 0, (
            "ALTER TABLE ... DISABLE TRIGGER succeeded as the migration role -- "
            "control 3's ownership reassignment did NOT close the re-review's "
            "reproduced CRITICAL exploit. An attacker holding this role's credentials "
            "could disable the append-only trigger and mutate rows freely."
        )
        assert OWNERSHIP_ERROR_FRAGMENT in result.stderr, (
            f"ALTER TABLE ... DISABLE TRIGGER was rejected, but not with the expected "
            f"ownership error. stderr was: {result.stderr}"
        )

    def test_migration_role_cannot_disable_episodic_append_only_trigger(
        self, pg_container: str
    ):
        """The HIGH-severity Zone 2 mirror of the CRITICAL exploit above."""
        result = _psql(
            pg_container,
            "ALTER TABLE episodic_entries "
            "DISABLE TRIGGER trg_episodic_entries_append_only;",
        )
        assert result.returncode != 0, (
            "ALTER TABLE ... DISABLE TRIGGER succeeded against episodic_entries as "
            "the migration role -- the identical CRITICAL-class gap the re-review "
            "found in provenance_schema.sql is still open in episodic_schema.sql."
        )
        assert OWNERSHIP_ERROR_FRAGMENT in result.stderr

    def test_disabled_trigger_attempt_leaves_mutation_still_blocked(
        self, pg_container: str
    ):
        """End-to-end proof: even attempting (and failing at) DISABLE TRIGGER first, a
        subsequent UPDATE is still rejected -- the disable attempt did not partially
        succeed or leave the trigger in a weakened state.
        """
        provenance_id = uuid.uuid4().hex
        record_hash = (provenance_id * 2)[:64]
        retrieval_context_hash = "b2" * 32
        insert_sql = f"""
            INSERT INTO provenance_records (
                tenant_id, provenance_id, item_id, source_zone, source_type,
                source_refs, write_timestamp, update_history, retrieval_context_hash,
                conflict_status, invalidation_flag, confidence, prev_hash, record_hash
            ) VALUES (
                'tenant-regression', '{provenance_id}', 'item-regression-1', 'episodic',
                'user_stated', '{{}}', '2026-01-01T00:00:00Z', '[]',
                '{retrieval_context_hash}', 'none', FALSE, 0.9, NULL,
                '{record_hash}'
            );
        """
        insert_result = _psql_baseline_insert(pg_container, insert_sql)
        assert insert_result.returncode == 0, f"Baseline INSERT failed: {insert_result.stderr}"

        disable_result = _psql(
            pg_container,
            "ALTER TABLE provenance_records "
            "DISABLE TRIGGER trg_provenance_records_append_only;",
        )
        assert disable_result.returncode != 0

        update_result = _psql(
            pg_container,
            f"UPDATE provenance_records SET confidence = 0.01 "
            f"WHERE provenance_id = '{provenance_id}';",
        )
        assert update_result.returncode != 0, (
            "UPDATE succeeded after a failed DISABLE TRIGGER attempt -- the failed "
            "ALTER statement must not have left the trigger in any weakened state."
        )
        # MIGRATION_ROLE holds no explicit GRANT on this table after control 2/3 run
        # (it is not the table owner, and was never GRANTed membership in
        # dashanan_app_role), so the UPDATE is refused at the privilege-check stage
        # ("permission denied") before the trigger even fires -- a STRONGER rejection
        # than attempt 1's append-only trigger message, not a weaker one. Either
        # rejection reason proves the mutation did not go through; a deployment that
        # DID grant this role UPDATE (which it should never do) would still be caught
        # by the append-only trigger underneath.
        assert (
            "append-only" in update_result.stderr
            or "permission denied" in update_result.stderr
        ), (
            f"UPDATE was rejected, but for neither the expected append-only-trigger "
            f"reason nor a privilege-denial reason. stderr was: {update_result.stderr}"
        )


class TestOwnershipReassignmentClosesFunctionReplaceBypass:
    """Reproduces the re-review's second, strictly-easier bypass: `CREATE OR REPLACE
    FUNCTION` on the trigger function itself, which never touches the table's DDL at
    all and therefore is not covered by locking down ALTER TABLE alone -- the function
    must be independently reassigned to `dashanan_schema_owner` too.
    """

    def test_migration_role_is_not_provenance_trigger_function_owner(
        self, pg_container: str
    ):
        """Preconditions check mirroring the table-ownership one above, but for the
        FUNCTION -- table and function ownership are independent in PostgreSQL, so this
        must be verified separately.
        """
        result = _psql(
            pg_container,
            "SELECT pg_get_userbyid(proowner) FROM pg_proc "
            "WHERE proname = 'reject_provenance_records_mutation';",
        )
        assert result.returncode == 0
        assert MIGRATION_ROLE not in result.stdout, (
            "reject_provenance_records_mutation() is still owned by the migration "
            "role -- control 3's ALTER FUNCTION ... OWNER TO dashanan_schema_owner "
            "did not take effect. Every exploit-attempt test in this class depends on "
            "this reassignment having succeeded."
        )
        assert "dashanan_schema_owner" in result.stdout

    def test_migration_role_cannot_neuter_provenance_trigger_function(
        self, pg_container: str
    ):
        """The re-review's second exploit: replacing the trigger function with a no-op
        via `CREATE OR REPLACE FUNCTION`, run by a role that is not the function's
        owner, must be refused.
        """
        noop_replace_sql = """
            CREATE OR REPLACE FUNCTION reject_provenance_records_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """
        result = _psql(pg_container, noop_replace_sql)
        assert result.returncode != 0, (
            "CREATE OR REPLACE FUNCTION succeeded against the trigger function as the "
            "migration role -- the re-review's second, strictly-easier bypass (never "
            "touching the table's DDL at all) is still open, even though the table "
            "itself is correctly locked down."
        )
        assert OWNERSHIP_ERROR_FRAGMENT in result.stderr, (
            f"CREATE OR REPLACE FUNCTION was rejected, but not with the expected "
            f"ownership error. stderr was: {result.stderr}"
        )

    def test_migration_role_cannot_neuter_episodic_trigger_function(
        self, pg_container: str
    ):
        """The HIGH-severity Zone 2 mirror of the function-replace exploit above."""
        noop_replace_sql = """
            CREATE OR REPLACE FUNCTION reject_episodic_entries_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """
        result = _psql(pg_container, noop_replace_sql)
        assert result.returncode != 0, (
            "CREATE OR REPLACE FUNCTION succeeded against episodic_entries' trigger "
            "function as the migration role -- the identical bypass the re-review "
            "found is still open in episodic_schema.sql."
        )
        assert OWNERSHIP_ERROR_FRAGMENT in result.stderr

    def test_failed_function_replace_leaves_mutation_still_blocked(
        self, pg_container: str
    ):
        """End-to-end proof mirroring the DISABLE TRIGGER class's equivalent test: a
        failed CREATE OR REPLACE FUNCTION attempt must not leave the trigger function
        partially replaced or otherwise weakened -- a subsequent UPDATE is still
        rejected by the ORIGINAL, unmodified function.
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
        insert_result = _psql_baseline_insert(pg_container, insert_sql)
        assert insert_result.returncode == 0, f"Baseline INSERT failed: {insert_result.stderr}"

        noop_replace_sql = """
            CREATE OR REPLACE FUNCTION reject_episodic_entries_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """
        replace_result = _psql(pg_container, noop_replace_sql)
        assert replace_result.returncode != 0

        update_result = _psql(
            pg_container,
            f"UPDATE episodic_entries SET payload = 'tampered-payload' "
            f"WHERE episode_id = '{episode_id}';",
        )
        assert update_result.returncode != 0, (
            "UPDATE succeeded after a failed CREATE OR REPLACE FUNCTION attempt -- "
            "the failed replace must not have left the trigger function in any "
            "weakened or partially-applied state."
        )
        # See the DISABLE TRIGGER class's equivalent test for why either message
        # proves the mutation was blocked (MIGRATION_ROLE holds no explicit GRANT on
        # this table, so a privilege-denial precedes the trigger's own check).
        assert (
            "append-only" in update_result.stderr
            or "permission denied" in update_result.stderr
        ), (
            f"UPDATE was rejected, but for neither the expected append-only-trigger "
            f"reason nor a privilege-denial reason. stderr was: {update_result.stderr}"
        )

        select_result = _psql_baseline_insert(
            pg_container,
            f"SELECT payload FROM episodic_entries WHERE episode_id = '{episode_id}';",
        )
        assert "tampered-payload" not in select_result.stdout
