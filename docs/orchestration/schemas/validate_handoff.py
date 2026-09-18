"""Structural + business-rule validator for the Dashanan backlog-related schemas.

Runs jsonschema.validate() for structural checks, then runs the two checks
that JSON Schema cannot express: FR-coverage completeness and circular
dependency detection. Supports both backlog payload shapes in this repo:

  - srs-to-backlog.schema.json: the abstract handoff-contract payload
    (output.stories keyed by story_id, input.srs_fr_list gives the master
    FR list to check coverage against).
  - backlog-draft.schema.json: the real on-disk backlog_draft.json artifact
    (sprint_1_stories is a plain array of story objects; the payload itself
    carries no master FR list, so FR-coverage completeness is skipped for
    this shape and only cycle detection runs -- documented, not silent).

Usage:
    python validate_handoff.py <schema.json> <payload.json>

Exits 0 and prints PASS lines on success; exits 1 and prints a description
of the first failure found otherwise.
"""

import json
import sys

import jsonschema


class HandoffValidationError(Exception):
    pass


def check_fr_coverage(fr_list, traced_frs):
    """Every fr_list entry must appear in traced_frs (the set of FRs stories trace to)."""
    uncovered = [fr for fr in fr_list if fr not in traced_frs]
    if uncovered:
        raise HandoffValidationError(
            f"FR coverage incomplete -- uncovered FR(s): {uncovered}"
        )


def check_no_cycles(graph):
    """Kahn's algorithm over a {story_id: [depends_on story_ids]} graph; reports any cycle found."""
    in_degree = {node: 0 for node in graph}
    dependents = {node: [] for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            in_degree[node] += 1
            dependents.setdefault(dep, []).append(node)

    queue = [node for node, deg in in_degree.items() if deg == 0]
    visited_count = 0

    while queue:
        current = queue.pop()
        visited_count += 1
        for dependent in dependents.get(current, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if visited_count != len(graph):
        remaining = [node for node, deg in in_degree.items() if deg > 0]
        raise HandoffValidationError(
            f"Circular dependency detected in depends_on graph -- story(ies) still "
            f"blocked after topological sort: {remaining}"
        )


def _handoff_contract_checks(payload):
    stories = payload.get("output", {}).get("stories", {})
    graph = {story_id: list(story.get("depends_on", [])) for story_id, story in stories.items()}
    traced_frs = {story.get("traces_to_fr") for story in stories.values()}
    fr_list = payload.get("input", {}).get("srs_fr_list", [])

    check_fr_coverage(fr_list, traced_frs)
    print("PASS (FR coverage): every srs_fr_list entry is traced to by a story")
    check_no_cycles(graph)
    print("PASS (no cycles): depends_on graph is a valid DAG")


def _backlog_draft_checks(payload):
    stories = payload.get("sprint_1_stories", [])
    graph = {story["story_id"]: list(story.get("depends_on", [])) for story in stories}

    check_no_cycles(graph)
    print("PASS (no cycles): sprint_1_stories depends_on graph is a valid DAG")
    print(
        "SKIP (FR coverage): backlog_draft.json carries no master FR list of its own "
        "to check coverage against -- see backlog-draft.schema.json's description."
    )


def validate(schema_path, payload_path):
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    jsonschema.validate(instance=payload, schema=schema)
    print(f"PASS (structural): {payload_path} validates against {schema_path}")

    title = schema.get("title", "")
    if title.startswith("Phase 5 (SRS.md + UML)"):
        _handoff_contract_checks(payload)
    elif title.startswith("docs/phase-6-sprint-planning/backlog_draft.json"):
        _backlog_draft_checks(payload)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} <schema.json> <payload.json>", file=sys.stderr)
        sys.exit(2)

    schema_path, payload_path = sys.argv[1], sys.argv[2]
    try:
        validate(schema_path, payload_path)
    except (jsonschema.ValidationError, HandoffValidationError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
