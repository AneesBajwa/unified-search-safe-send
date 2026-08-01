"""The two shapes the brief closes, asserted field for field (task 9.12).

    interface Result {                    // CLOSED — no score, no raw, no extras
      source: string; id: string; title: string; snippet: string;
      author?: string; timestamp?: string; url: string;
    }
    interface Draft {
      id: string; channel: "gmail" | "slack"; to: string;
      subject?: string; body: string; idempotency_key: string;
    }

Three assertions, each catching a different way this goes wrong:

1. **The published schema matches the brief.** A renamed or added server field
   fails here.
2. **The generated client file matches the published schema**, by regenerating
   and comparing — so a stale `schema.d.ts`, or one somebody edited by hand,
   fails rather than drifting quietly until a card renders blank.
3. **The wire agrees with both.** 🔴 The one that matters. Phase 3 shipped three
   defects past a green suite, every one of them a test asserting a *shape*
   rather than reading the value that actually crossed the boundary. A schema is
   a claim about responses; this makes a request and reads the response.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from api.main import create_app
from httpx import AsyncClient

from scripts.gen_schema import OUTPUT, render

#: The brief, transcribed. ``True`` means required.
BRIEF_RESULT: dict[str, bool] = {
    "source": True,
    "id": True,
    "title": True,
    "snippet": True,
    "author": False,
    "timestamp": False,
    "url": True,
}
BRIEF_DRAFT: dict[str, bool] = {
    "id": True,
    "channel": True,
    "to": True,
    "subject": False,
    "body": True,
    "idempotency_key": True,
}


def _schema(name: str) -> dict[str, Any]:
    spec = create_app(run_worker_inline=False).openapi()
    schemas = spec["components"]["schemas"]
    assert name in schemas, (
        f"{name} is not published in OpenAPI at all. A route annotated "
        f"`-> dict[str, Any]` publishes `{{}}`, which would make every assertion "
        f"in this file compare nothing to nothing and pass."
    )
    return dict(schemas[name])


def _assert_matches_brief(name: str, brief: dict[str, bool]) -> None:
    schema = _schema(name)
    fields = set(schema["properties"])
    required = set(schema.get("required", []))

    assert fields == set(brief), (
        f"{name} does not match the brief field for field. "
        f"extra: {sorted(fields - set(brief))}; missing: {sorted(set(brief) - fields)}"
    )
    for field, is_required in brief.items():
        assert (field in required) is is_required, (
            f"{name}.{field} is {'required' if field in required else 'optional'}; "
            f"the brief says {'required' if is_required else 'optional'}"
        )

    # CLOSED means closed: `additionalProperties: false`, so a ranking score or a
    # raw provider payload cannot be bolted on without this failing.
    assert schema.get("additionalProperties") is False, (
        f"{name} is not closed — additionalProperties is "
        f"{schema.get('additionalProperties')!r}, so extra keys are permitted"
    )


def test_result_matches_the_brief() -> None:
    _assert_matches_brief("Result", BRIEF_RESULT)


def test_draft_matches_the_brief() -> None:
    _assert_matches_brief("Draft", BRIEF_DRAFT)


def test_optional_fields_are_string_or_null_and_nothing_else() -> None:
    """``author?: string`` admits absence, not a different type.

    The wire representation of "absent" is either omission or ``null``; what it
    is not is a number, or an object, or a string that is sometimes a list. Both
    optionals are checked because the normalizer is what fills them, and a
    provider with a missing field is exactly where a half-populated value gets in.
    """
    properties = _schema("Result")["properties"]
    for field in ("author", "timestamp"):
        options = {option.get("type") for option in properties[field]["anyOf"]}
        assert options == {"string", "null"}, f"Result.{field} admits {options}"


def test_generated_client_types_are_current_and_unedited() -> None:
    """The file the console imports is what the server would generate today.

    Regenerating and comparing, rather than parsing the committed file, is what
    makes "never hand-edited" enforceable: an edit and a stale file are the same
    failure here, and both are real. A parse-only check would pass on a file
    somebody tidied by hand.
    """
    assert OUTPUT.exists(), f"{OUTPUT} has never been generated — run `make schema`"
    on_disk = OUTPUT.read_text()
    fresh = render(create_app(run_worker_inline=False).openapi())
    assert on_disk == fresh, (
        f"{OUTPUT.name} is stale or was edited by hand. Run `make schema` and "
        f"commit the result — never edit it directly."
    )


def test_generated_file_declares_both_closed_shapes() -> None:
    """And declares them with the brief's fields, read out of the emitted text.

    Belt and braces over the byte-comparison above: that one proves the file
    matches the schema, this one proves the *emitted TypeScript* actually says
    what we think — a generator bug that dropped every property would satisfy the
    comparison and fail here.
    """
    text = OUTPUT.read_text()
    for name, brief in (("Result", BRIEF_RESULT), ("Draft", BRIEF_DRAFT)):
        block = re.search(rf"^    {name}: \{{\n(.*?)^    \}};$", text, re.S | re.M)
        assert block, f"{name} is not declared in {OUTPUT.name}"
        declared = dict(
            re.findall(r"^      (\w+)(\??): ", block.group(1), re.M)
        )
        assert set(declared) == set(brief), (
            f"{name} in {OUTPUT.name} declares {sorted(declared)}, "
            f"brief says {sorted(brief)}"
        )
        for field, is_required in brief.items():
            assert (declared[field] == "") is is_required, (
                f"{name}.{field} optionality disagrees with the brief"
            )

    assert 'export type Result = components["schemas"]["Result"];' in text
    assert 'export type Draft = components["schemas"]["Draft"];' in text


@pytest.mark.usefixtures("clean_db")
async def test_the_wire_agrees_with_the_schema(
    api_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """A real request, and the keys actually returned.

    Success criterion 3 is "every result conforms to the closed shape", and the
    only way to know is to look at one. The search below runs the fixture web
    source, so this is hermetic — the same assertion against **live** Gmail and
    Slack is verification step 2, run by hand, because a real provider's missing
    field is where this would actually bite.
    """
    from core.jobs.runtime import run_once

    client, auth = api_client

    queued = await client.post("/v1/searches", json={"query": "acme"}, headers=auth)
    search_id = queued.json()["search_id"]
    for _ in range(12):
        await run_once()
        snapshot = (await client.get(f"/v1/searches/{search_id}", headers=auth)).json()
        if snapshot["finished"]:
            break
    assert snapshot["results"], "the search returned nothing to check the shape of"

    required = {field for field, req in BRIEF_RESULT.items() if req}
    for result in snapshot["results"]:
        extra = set(result) - set(BRIEF_RESULT)
        assert not extra, f"a result carries keys outside the closed shape: {extra}"
        assert required <= set(result), f"a result is missing {required - set(result)}"
        # An optional that is present must be a string; one that is absent must
        # be absent rather than null, matching `author?: string`.
        for field in ("author", "timestamp"):
            if field in result:
                assert isinstance(result[field], str), f"{field} is {type(result[field])}"

    # ...and the same for Draft, through the route that mints one.
    draft = (
        await client.post(
            "/v1/drafts",
            json={"channel": "gmail", "to": "someone@example.test", "body": "hi"},
            headers=auth,
        )
    ).json()
    assert set(draft) == {"draft", "confirmation"}, (
        "the confirmation payload must be a SIBLING of the draft — merging it in "
        "would violate the closed interface the brief fixes"
    )
    assert set(draft["draft"]) <= set(BRIEF_DRAFT)
    assert {f for f, req in BRIEF_DRAFT.items() if req} <= set(draft["draft"])
