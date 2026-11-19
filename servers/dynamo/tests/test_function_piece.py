from __future__ import annotations

import json
import logging

import pytest

from conftest import API, GRAPHQL, TOKEN, build_tools, make_settings, writes

SV_A = "https://w3id.org/okn/i/mint/sv-a"
SV_B = "https://w3id.org/okn/i/mint/sv-b"
INPUTS = [{"standard_variable_uri": SV_A, "format": "csv"}]
OUTPUTS = [{"standard_variable_uri": SV_B, "unit": "m"}]
FUNCTION = "def transform(inputs):\n    return {'n': 1}\n"
SCRIPT = ("from owe_python_sdk.runtime import execution_context as ctx\n"
          "ctx.set_output('result', '1')\n")
ENC = "https%3A%2F%2Fw3id.org%2Fokn%2Fi%2Fmint%2F"


@pytest.fixture
def wt():
    return build_tools(make_settings(enable_catalog_writes=True, tapis_token=TOKEN))


def known_variables(rsps, *slugs):
    for slug in slugs:
        rsps.get(f"{API}/standardvariables/{ENC}{slug}", json={"id": slug})


def create(tools, **over):
    args = dict(label="Double it", description="d", inputs=INPUTS, outputs=OUTPUTS, python_source=FUNCTION)
    args.update(over)
    return tools["create_etl_function_piece"](**args)


def test_dry_run_shows_the_exact_insert_and_sends_nothing(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    out = create(wt, env_from_args={"SOURCE_URI": "source_uri"})
    assert out["dry_run"] is True and out["valid"] is True
    obj = out["variables"]["object"]
    assert obj["id"] == out["process_id"]
    assert obj["id"].startswith("https://w3id.org/okn/i/mint/etl/double-it-")
    assert obj["visibility"] == "private" and obj["runtime_kind"] == "tapis_function"
    assert "owner_username" not in obj  # Hasura sets it from the session
    assert obj["runtime_json"] == {
        "language": "python", "entrypoint": "transform",
        "source": {"language": "python", "code": FUNCTION},
        "env_from_args": {"SOURCE_URI": "source_uri"},
    }
    assert obj["contracts"]["data"] == [
        {"role": "input", "position": 0, "standard_variable_uri": SV_A, "format": "csv"},
        {"role": "output", "position": 0, "standard_variable_uri": SV_B, "unit": "m"},
    ]
    assert writes(rsps) == []


def test_default_id_is_random_and_process_id_pins_it(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    first, second = create(wt)["process_id"], create(wt)["process_id"]
    assert first != second
    assert create(wt, process_id=first)["process_id"] == first


def test_script_mode_has_no_entrypoint(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    out = create(wt, python_source=SCRIPT, entrypoint=None)
    assert out["valid"] and out["warnings"] == []
    assert "entrypoint" not in out["variables"]["object"]["runtime_json"]


@pytest.mark.parametrize("over,fragment", [
    ({"python_source": "def transform(:\n"}, "does not parse"),
    ({"python_source": "x = 1\n"}, "no top-level function named 'transform'"),
    ({"python_source": "async def transform(inputs):\n    return 1\n"}, "not async"),
    ({"python_source": FUNCTION, "entrypoint": "not valid"}, "valid Python function name"),
    ({"python_source": "   "}, "python_source is required"),
    ({"python_source": "#" * 64_001}, "larger than 64000 bytes"),
    ({"inputs": []}, "at least one input is required"),
    ({"outputs": [{"format": "csv"}]}, "outputs[0]: standard_variable_uri is required"),
    ({"env_from_args": {"X": "signal_token"}}, "may not bind: signal_token"),
    ({"env_from_args": {"X": "ckan_token", "Y": "token_fetch_token"}}, "ckan_token, token_fetch_token"),
])
def test_validation_errors_stop_before_any_request(wt, rsps, over, fragment):
    out = create(wt, **over)
    assert out["error"] == "validation_failed"
    assert fragment in " ".join(out["errors"])
    assert len(rsps.calls) == 0


def test_tapis_token_and_source_uri_may_be_bound(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    assert create(wt, env_from_args={"TAPIS_TOKEN": "tapis_token", "S": "source_uri"})["valid"]


def test_warnings_for_a_script_that_sets_no_output_and_a_two_argument_entrypoint(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    assert "set_output" in create(wt, python_source="x = 1\n", entrypoint=None)["warnings"][0]
    two = create(wt, python_source="def transform(a, b):\n    return 1\n")
    assert "one argument" in two["warnings"][0]


def test_unknown_standard_variable_is_a_validation_error(wt, rsps):
    known_variables(rsps, "sv-a")
    rsps.get(f"{API}/standardvariables/{ENC}sv-b", status=404, json={"message": "not found"})
    out = create(wt)
    assert out["error"] == "validation_failed"
    assert out["errors"] == [f"standard variable not found in the catalog: {SV_B}"]
    assert writes(rsps) == []


def test_live_create_posts_one_mutation_as_the_caller(wt, rsps, caplog):
    known_variables(rsps, "sv-a", "sv-b")
    rsps.post(GRAPHQL, json={"data": {"insert_modelcatalog_etl_process_one": {
        "id": "etl-1", "label": "Double it", "owner_username": "testuser", "visibility": "private"}}})
    with caplog.at_level(logging.INFO):
        out = create(wt, process_id="etl-1", dry_run=False, confirm=True)
    assert out == {"ok": True, "status": "created", "process_id": "etl-1", "owner_username": "testuser",
                   "visibility": "private", "warnings": [], "next": out["next"]}
    (call,) = writes(rsps)
    assert call.request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(call.request.body)
    assert "insert_modelcatalog_etl_process_one" in body["query"]
    assert body["variables"]["object"]["id"] == "etl-1"
    # Neither the token nor the source reaches the logs.
    assert TOKEN not in caplog.text and "def transform" not in caplog.text
    assert "tool=create_etl_function_piece" in caplog.text and "status=ok" in caplog.text


def test_existing_id_is_reported_and_not_an_error(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    rsps.post(GRAPHQL, json={"errors": [{"message": "Uniqueness violation. duplicate key value violates "
                                                    "unique constraint \"modelcatalog_etl_process_pkey\""}]})
    out = create(wt, process_id="etl-1", dry_run=False, confirm=True)
    assert out["ok"] is True and out["status"] == "exists" and out["process_id"] == "etl-1"


def test_hasura_errors_come_back_verbatim_and_scrubbed(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    rsps.post(GRAPHQL, json={"errors": [{"message": f"permission denied for Bearer {TOKEN}"}]})
    out = create(wt, process_id="etl-1", dry_run=False, confirm=True)
    assert out["error"] == "backend_error" and out["service"] == "hasura"
    assert "permission denied" in json.dumps(out["body"]) and TOKEN not in json.dumps(out)


@pytest.mark.parametrize("settings_over,kwargs,error", [
    ({"enable_catalog_writes": True, "tapis_token": TOKEN}, {"confirm": False}, "confirm_required"),
    ({"enable_catalog_writes": True, "tapis_token": None}, {"confirm": True}, "writes_require_token"),
    ({"enable_catalog_writes": False, "tapis_token": TOKEN}, {"confirm": True}, "catalog_writes_disabled"),
])
def test_live_write_gates(rsps, settings_over, kwargs, error):
    known_variables(rsps, "sv-a", "sv-b")
    tools = build_tools(make_settings(**settings_over))
    out = create(tools, dry_run=False, **kwargs)
    assert out["error"] == error
    assert writes(rsps) == []


# --- run arguments ----------------------------------------------------------

PARAMS = {"properties": {"output_uri": {"type": "string", "description": "Where to write."},
                         "match_mode": {"type": "string", "default": "auto"}},
          "required": ["output_uri"]}


def test_declared_arguments_are_stored_as_the_parameter_schema(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    out = create(wt, env_from_args={"TAPIS_TOKEN": "tapis_token", "OUTPUT_URI": "output_uri"},
                 parameters=PARAMS)
    assert out["valid"] is True
    assert out["variables"]["object"]["runtime_json"]["parameters_schema_json"] == {
        "type": "object", "properties": PARAMS["properties"], "required": ["output_uri"]}


def test_no_parameters_means_no_schema_is_stored(wt, rsps):
    known_variables(rsps, "sv-a", "sv-b")
    out = create(wt, env_from_args={"S": "source_uri"})
    assert "parameters_schema_json" not in out["variables"]["object"]["runtime_json"]


@pytest.mark.parametrize("over,fragment", [
    ({"env_from_args": {"OUTPUT_URI": "output_uri"}}, "does not declare in parameters: output_uri"),
    ({"env_from_args": {"OUTPUT_URI": "output_uri"}, "parameters": {"properties": {"other": {}}}},
     "does not declare in parameters: output_uri"),
    ({"parameters": {"properties": {"signal_token": {}}}}, "parameters may not declare: signal_token"),
    ({"parameters": {"properties": {"tapis_token": {}}}}, "parameters may not declare: tapis_token"),
    ({"parameters": {"properties": {"a": {}}, "required": ["b"]}}, "required names undeclared arguments: b"),
    ({"parameters": {"properties": {"a": "string"}}}, "must map each argument name to a schema object"),
])
def test_argument_declarations_are_checked_before_any_request(wt, rsps, over, fragment):
    out = create(wt, **over)
    assert out["error"] == "validation_failed"
    assert fragment in " ".join(out["errors"])
    assert len(rsps.calls) == 0


# --- delete -----------------------------------------------------------------

PIECE = {"id": "etl-1", "label": "Double it", "version": "1.0.0", "owner_username": "testuser",
         "visibility": "private", "runtime_kind": "tapis_function"}


def test_delete_dry_run_looks_the_piece_up_and_deletes_nothing(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_etl_process_by_pk": PIECE}})
    out = wt["delete_etl_function_piece"](process_id="etl-1")
    assert out["dry_run"] is True and out["found"] is True and out["piece"] == PIECE
    (call,) = rsps.calls
    assert "delete_modelcatalog" not in json.loads(call.request.body)["query"]


def test_delete_dry_run_reports_a_piece_the_caller_cannot_see(wt, rsps):
    rsps.post(GRAPHQL, json={"data": {"modelcatalog_etl_process_by_pk": None}})
    out = wt["delete_etl_function_piece"](process_id="nope")
    assert out["found"] is False and out["piece"] is None


def test_live_delete_sends_one_mutation_as_the_caller(wt, rsps, caplog):
    rsps.post(GRAPHQL, json={"data": {"delete_modelcatalog_etl_process_by_pk": {
        "id": "etl-1", "label": "Double it", "owner_username": "testuser"}}})
    with caplog.at_level(logging.INFO):
        out = wt["delete_etl_function_piece"](process_id="etl-1", dry_run=False, confirm=True)
    assert out["ok"] is True and out["status"] == "deleted" and out["process_id"] == "etl-1"
    (call,) = rsps.calls
    assert call.request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(call.request.body)
    assert "delete_modelcatalog_etl_process_by_pk" in body["query"] and body["variables"] == {"id": "etl-1"}
    assert TOKEN not in caplog.text
    assert "tool=delete_etl_function_piece" in caplog.text and "status=ok" in caplog.text


def test_a_piece_owned_by_someone_else_is_not_deleted(wt, rsps):
    # Hasura applies the owner filter and answers null, not an error.
    rsps.post(GRAPHQL, json={"data": {"delete_modelcatalog_etl_process_by_pk": None}})
    out = wt["delete_etl_function_piece"](process_id="theirs", dry_run=False, confirm=True)
    assert out["ok"] is False and out["status"] == "not_deleted"


@pytest.mark.parametrize("settings_over,kwargs,error", [
    ({"enable_catalog_writes": True, "tapis_token": TOKEN}, {"confirm": False}, "confirm_required"),
    ({"enable_catalog_writes": True, "tapis_token": None}, {"confirm": True}, "writes_require_token"),
    ({"enable_catalog_writes": False, "tapis_token": TOKEN}, {"confirm": True}, "catalog_writes_disabled"),
])
def test_live_delete_gates(rsps, settings_over, kwargs, error):
    tools = build_tools(make_settings(**settings_over))
    out = tools["delete_etl_function_piece"](process_id="etl-1", dry_run=False, **kwargs)
    assert out["error"] == error
    assert rsps.calls == [] or len(rsps.calls) == 0
