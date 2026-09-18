"""Structural + business-rule validator for the Dashanan handoff schemas.

Runs jsonschema.validate() for structural checks, then runs the two checks
that JSON Schema cannot express: FR-coverage completeness and circular
dependency detection, both scoped to srs-to-backlog.schema.json (the only
one of the 3 handoff payloads that carries enough information in itself to
check either rule -- see docs/orchestration/schemas/*.schema.json
descriptions for what remains intentionally out of scope for schema 1/2).

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


def check_fr_coverage(payload):
    """Every input.srs_fr_list entry must be traced to by at least one story."""
    srs_fr_list = payload.get("input", {}).get("srs_fr_list", [])
    stories = payload.get("output", {}).get("stories", {})
    traced_frs = {story.get("traces_to_fr") for story in stories.values()}
    uncovered = [fr for fr in srs_fr_list if fr not in traced_frs]
    if uncovered:
        raise HandoffValidationError(
            f"FR coverage incomplete -- uncovered FR(s): {uncovered}"
        )


def check_no_cycles(payload):
    """Kahn's algorithm over the story_id -> depends_on graph; reports any cycle found."""
    stories = payload.get("output", {}).get("stories", {})
    graph = {story_id: list(story.get("depends_on", [])) for story_id, story in stories.items()}

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


def validate(schema_path, payload_path):
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    with open(payload_path, encoding="utf-8") as f:
        payload = json.load(f)

    jsonschema.validate(instance=payload, schema=schema)
    print(f"PASS (structural): {payload_path} validates against {schema_path}")

    if schema.get("title", "").startswith("Phase 5 (SRS.md + UML)"):
        check_fr_coverage(payload)
        print("PASS (FR coverage): every srs_fr_list entry is traced to by a story")
        check_no_cycles(payload)
        print("PASS (no cycles): depends_on graph is a valid DAG")


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
