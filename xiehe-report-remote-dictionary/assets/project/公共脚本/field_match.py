"""Conservative public-definition reuse for onboarding plans (no I/O or queries).

F identifiers belong to a template. D identifiers describe a business definition,
SM identifiers describe a source. This module compares those separately and never
claims that a matching definition proves a station/period has usable values.
See Skill references/onboarding.md for the JSON plan contract.
"""
from copy import deepcopy
from difflib import SequenceMatcher
import re


ACTIONS = {
    "reuse_existing_definition", "add_source_to_existing_definition",
    "new_definition_candidate", "needs_clarification",
}
SEMANTICS = ("definition", "grain", "period_semantics", "unit", "role",
             "grain_object", "caliber", "scope")
CORE_SEMANTICS = ("definition", "grain", "period_semantics", "unit")
INSTANCE_KEYS = {
    "station_code", "stationcode", "stationcodes", "station_in", "station_id",
    "period", "business_period", "settlement_month", "starttime", "endtime",
    "start_time", "end_time", "profile", "processinsid", "taskid",
    "electricitybill_correlation_id",
}
MONTH_KEYS = {"period", "business_period", "settlement_month", "starttime",
              "endtime", "start_time", "end_time"}
INSTANCE_BINDINGS = {
    "station_code": "${station_code}", "stationcode": "${station_code}",
    "stationcodes": "${station_code}", "station_in": "${station_code}",
    "station_id": "${station_id}", "period": "${period}",
    "business_period": "${period}", "settlement_month": "${settlement_month}",
    "starttime": "${period}", "endtime": "${period}", "start_time": "${period}",
    "end_time": "${period}", "profile": "${profile}",
    "processinsid": "${selected.processInstanceId}", "taskid": "${selected.taskId}",
    "electricitybill_correlation_id": "${detail.data.process.variables.r_id}",
}
SOURCE_KEYS = ("source_id", "query_method_id", "method", "endpoint",
               "response_path", "response_field", "value_binding", "original_unit", "normalization", "time_field")
RATIOS = {("kWh", "万kWh"): 0.0001, ("万kWh", "kWh"): 10000,
          ("kW", "MW"): 0.001, ("MW", "kW"): 1000}


def _present(value):
    return value is not None and value != "" and value != {} and value != []


def _text(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _evidence(value):
    """Evidence is a declared reference; file authenticity is an upstream check."""
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value)


def _parameter_contract(value, key=""):
    # Only named instance dimensions are normalized. Device groups, dateType,
    # name_role, phase, collection, meter_policy etc. remain semantic conditions.
    if isinstance(value, dict):
        return {k: _parameter_contract(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        if key.casefold() in INSTANCE_KEYS:
            return [_parameter_contract(v, key) for v in value]
        return [_parameter_contract(v, key) for v in value]
    if key.casefold() in INSTANCE_KEYS and _present(value):
        if isinstance(value, str) and value.startswith("${") and value != INSTANCE_BINDINGS.get(key.casefold()):
            return value  # A different binding is a changed contract, not an instance.
        if key.casefold() in MONTH_KEYS and not (
                isinstance(value, str) and (re.fullmatch(r"\$\{[^}]+\}", value)
                or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value))):
            return {"invalid_month_format": value}
        return "${instance:" + key.casefold() + "}"
    return value


def _source(field):
    return field.get("source") or field.get("source_mapping") or {}


def _binding(field):
    binding = field.get("data_definition") or {}
    return field.get("standard_id") or binding.get("standard_id"), deepcopy(
        field.get("parameters", binding.get("parameters", {})))


def _semantics(field):
    result = {key: field[key] for key in SEMANTICS if _present(field.get(key))}
    result.update({key: value for key, value in field.get("semantics", {}).items()
                   if key in SEMANTICS and _present(value)})
    return result


def _parameter_issues(standard, parameters):
    spec = standard.get("definition_parameters", {})
    if not isinstance(spec, dict) or any(not isinstance(v, dict) for v in spec.values()):
        return ["definition_parameters must contain rule objects"]
    if not isinstance(parameters, dict):
        return ["parameters must be an object"]
    issues = []
    if set(parameters) != set(spec):
        issues.append("definition parameters missing or unexpected: " +
                      ",".join(sorted(set(parameters) ^ set(spec))))
    for key, rule in spec.items():
        value = parameters.get(key)
        if rule.get("required") is not True:
            issues.append("definition parameter must declare required: " + key)
        if (not isinstance(value, str) or not value or
                (rule.get("allowed_values") and value not in rule["allowed_values"])):
            issues.append("invalid definition parameter: " + key)
    if parameters.get("collection") in {"before_data", "after_data"}:
        if parameters.get("phase") not in {"before", "after"} or (
                parameters.get("phase", "") + "_data" != parameters["collection"]):
            issues.append("collection and phase refer to different stages")
    return issues


def _source_comparison(source, mapping):
    """A shared endpoint alone is not a shared source field."""
    same, conflicts, missing = [], [], []
    for key in SOURCE_KEYS:
        left, right = source.get(key), mapping.get(key)
        if not _present(left) or not _present(right):
            missing.append(key)
        elif left == right:
            same.append(key)
        else:
            conflicts.append(key)
    left, right = source.get("request_parameters"), mapping.get("request_parameters")
    if not isinstance(left, dict) or not isinstance(right, dict):
        missing.append("request_parameters")
    elif _parameter_contract(left) == _parameter_contract(right):
        same.append("request_parameters")
    else:
        conflicts.append("request_parameters")
    for key in ("input_contract", "period_format"):
        if key in source or key in mapping:
            if not _present(source.get(key)) or not _present(mapping.get(key)):
                missing.append(key)
            elif source[key] != mapping[key]:
                conflicts.append(key)
            else:
                same.append(key)
    return {"exact": not conflicts and not missing, "same": same,
            "conflicts": conflicts, "missing": missing}


def _equivalence_issues(standard, field, equivalence):
    """Require an explicit, evidenced business comparison for different sources."""
    if not isinstance(equivalence, dict):
        return ["business equivalence requires structured evidence"]
    issues = []
    if equivalence.get("status") != "confirmed" or not _evidence(equivalence.get("evidence")):
        issues.append("business equivalence is not confirmed with evidence")
    dimensions = equivalence.get("dimensions", {})
    if not isinstance(dimensions, dict):
        return issues + ["equivalence dimensions must be an object"]
    required = set(CORE_SEMANTICS) | {k for k in SEMANTICS if _present(standard.get(k))}
    for key in sorted(required):
        if not _present(standard.get(key)) or dimensions.get(key) != standard.get(key):
            issues.append("equivalence must explicitly match target " + key)
    for key, value in _semantics(field).items():
        if _present(standard.get(key)) and value != standard[key]:
            issues.append("field contradicts target " + key)
    return issues


def _catalog_parts(dictionary):
    for key in ("standard_fields", "source_mappings", "query_methods", "sources"):
        if not isinstance(dictionary.get(key), dict):
            raise ValueError("dictionary requires object: " + key)
    return dictionary["standard_fields"], dictionary["source_mappings"]


def match_fields(dictionary, fields):
    """Return candidates, including ambiguity and missing semantic dimensions.

    The result never grants filling approval. Exact source matches may recommend
    reuse, but runtime station/period, unit and applicability checks still apply.
    """
    standards, mappings = _catalog_parts(dictionary)
    if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
        raise ValueError("fields must be a list of objects")
    results = []
    for index, field in enumerate(fields):
        requested_id, parameters = _binding(field)
        source, semantic = _source(field), _semantics(field)
        if not isinstance(source, dict) or not isinstance(field.get("semantics", {}), dict):
            raise ValueError("source and semantics must be objects")
        aliases = dictionary.get("deduplication", {}).get("definition_aliases", {})
        requested_id = aliases.get(requested_id, requested_id)
        candidates = []
        for sid, standard in standards.items():
            score, reasons, conflicts, missing = 0, [], [], []
            if requested_id == sid:
                score += 30
                reasons.append("explicit public definition reference")
            if field.get("key") and field["key"] == standard.get("key"):
                score += 35
                reasons.append("same public machine key")
            name = field.get("name", "")
            if name and _text(name) == _text(standard.get("name")):
                score += 15
                reasons.append("same name is only a clue")
            elif name and standard.get("name"):
                similarity = SequenceMatcher(None, _text(name), _text(standard["name"])).ratio()
                if similarity >= 0.45:
                    score += round(similarity * 10, 2)
                    reasons.append("similar name is only a clue")
            comparisons = []
            if source:
                for mid, mapping in mappings.items():
                    if mapping.get("standard_id") != sid:
                        continue
                    comparison = _source_comparison(source, mapping)
                    # Include all same-method candidates to expose differing
                    # response fields/paths; never select one by result order.
                    if (comparison["exact"] or source.get("query_method_id") == mapping.get("query_method_id")
                            or source.get("mapping_id") == mid):
                        comparisons.append({"mapping_id": mid, **comparison})
                    if comparison["exact"]:
                        score += 100
                        reasons.append("same complete source contract: " + mid)
                    elif (source.get("query_method_id") and
                          source["query_method_id"] == mapping.get("query_method_id")):
                        score += 8
            for key, value in semantic.items():
                if not _present(standard.get(key)):
                    missing.append("target semantic " + key)
                elif value != standard[key]:
                    conflicts.append("semantic " + key)
                elif key in CORE_SEMANTICS:
                    score += 3
            # Dimension matches alone are too broad to be a useful candidate.
            if not reasons and not comparisons:
                continue
            missing.extend(_parameter_issues(standard, parameters))
            if not all(key in semantic for key in CORE_SEMANTICS):
                missing.append("business equivalence not fully described")
            exact = [c["mapping_id"] for c in comparisons if c["exact"]]
            candidates.append({
                "standard_id": sid, "name": standard.get("name"), "score": score,
                "reasons": reasons, "conflicts": conflicts, "missing": missing,
                "source_matches": comparisons, "exact_source_mapping_ids": exact,
                "parameters_valid": not _parameter_issues(standard, parameters),
            })
        candidates.sort(key=lambda c: (-c["score"], c["standard_id"]))
        exact = [c for c in candidates if c["exact_source_mapping_ids"]]
        selected, action = None, "needs_clarification"
        if len(exact) == 1 and not exact[0]["conflicts"] and exact[0]["parameters_valid"]:
            if not requested_id or requested_id == exact[0]["standard_id"]:
                selected, action = exact[0]["standard_id"], "reuse_existing_definition"
        results.append({
            "field_id": field.get("field_id", field.get("id", "field-" + str(index + 1))),
            "name": field.get("name"), "candidates": candidates,
            "recommended_action": action, "selected_standard_id": selected,
            "parameters": parameters, "can_fill": False,
            "reason": ("exact source contract supports reuse; runtime validation still required"
                       if selected else "candidate review required; missing source/value is not a new definition"),
        })
    return {"schema_version": 1, "fields": results}


def _source_mapping_issues(dictionary, mapping, standard):
    issues = []
    if not isinstance(mapping, dict):
        return ["source_mapping must be an object"]
    mid = mapping.get("mapping_id", "")
    if not re.fullmatch(r"SM\d{3}", mid):
        issues.append("mapping_id must use SM followed by three digits")
    if mid in dictionary["source_mappings"]:
        issues.append("mapping_id already exists: " + mid)
    if mapping.get("standard_id") != standard["standard_id"]:
        issues.append("source_mapping standard_id differs from selected definition")
    for key in SOURCE_KEYS + ("source_field_name", "original_unit", "technical_status", "semantic_status"):
        if not _present(mapping.get(key)):
            issues.append("source_mapping missing " + key)
    source_id, method_id = mapping.get("source_id"), mapping.get("query_method_id")
    if source_id not in dictionary["sources"]:
        issues.append("unknown source: " + str(source_id))
    method = dictionary["query_methods"].get(method_id)
    if not method:
        issues.append("query method is not implemented; implement and verify adapter first")
    elif method.get("source_id") != source_id:
        issues.append("method and mapping refer to different sources")
    elif method.get("path") and (mapping.get("endpoint") != method["path"]
                                 or mapping.get("method") != method.get("method")):
        issues.append("mapping endpoint/method contradicts query method")
    if not isinstance(mapping.get("request_parameters"), dict):
        issues.append("source_mapping missing request parameter contract")
    elif _parameter_contract(mapping["request_parameters"]) != mapping["request_parameters"]:
        # Public mappings must bind instances, not freeze the sample station or
        # month. Compare literals separately because canonical tokens are internal.
        for key, value in mapping["request_parameters"].items():
            if key.casefold() in INSTANCE_KEYS:
                values = value if isinstance(value, list) else [value]
                if any(not isinstance(v, str) or not re.fullmatch(r"\$\{[^}]+\}", v) for v in values):
                    issues.append("public source has concrete instance parameter: " + key)
    if not _evidence(mapping.get("evidence")):
        issues.append("new source requires evidence")
    binding = mapping.get("value_binding", {})
    if not isinstance(binding, dict) or binding.get("field") != mapping.get("response_field"):
        issues.append("value_binding field differs from response_field")
    conversion = mapping.get("normalization", {})
    origin, unit = mapping.get("original_unit"), standard.get("unit")
    factor = 1 if origin == unit else RATIOS.get((origin, unit))
    if (factor is None or not isinstance(conversion, dict)
            or isinstance(conversion.get("factor"), bool) or conversion.get("factor") != factor
            or conversion.get("operation") != ("identity" if origin == unit else "multiply")):
        issues.append("source normalization is missing or not a supported unit conversion")
    for existing_id, existing in dictionary["source_mappings"].items():
        comparison = _source_comparison(mapping, existing)
        if comparison["exact"]:
            issues.append("source field already mapped by " + existing_id +
                          "; reuse/review that mapping instead of duplicating it")
    return issues


def _new_definition_issues(dictionary, definition, field, decision, candidates):
    issues = []
    if not isinstance(definition, dict):
        return ["definition must be an object"]
    sid = definition.get("standard_id", "")
    if not re.fullmatch(r"D\d{3}", sid):
        issues.append("standard_id must use D followed by three digits")
    aliases = dictionary.get("deduplication", {}).get("definition_aliases", {})
    if sid in dictionary["standard_fields"] or sid in aliases:
        issues.append("standard_id already exists or is a retired alias")
    for key in ("name", "key", "definition_status", "role") + CORE_SEMANTICS:
        if not _present(definition.get(key)):
            issues.append("new definition missing " + key)
    if definition.get("key") in {s.get("key") for s in dictionary["standard_fields"].values()}:
        issues.append("public machine key already exists")
    if definition.get("definition_status") != "business_definition_documented_source_pending":
        issues.append("new definition candidate must remain source pending")
    if definition.get("historical_source") or definition.get("name_basis", {}).get("mapping_ids"):
        issues.append("new definition cannot inherit historical approval or unregistered mappings")
    if definition.get("name_basis", {}).get("status") != "business_definition_source_pending":
        issues.append("new definition name basis must remain source pending")
    retrieval = definition.get("retrieval", {})
    if retrieval and (retrieval.get("status") != "pending_verification" or any(
            _present(retrieval.get(k)) for k in ("source_id", "query_method_id", "endpoint", "response_field", "parameters"))):
        issues.append("unknown source must remain null/pending; add verified source separately")
    if not _evidence(decision.get("evidence")):
        issues.append("new definition requires business evidence")
    differences = decision.get("differentiation", {})
    if not isinstance(differences, dict):
        differences = {}
        issues.append("differentiation must be an object keyed by candidate D ID")
    for candidate in candidates:
        sid = candidate["standard_id"]
        if candidate["exact_source_mapping_ids"]:
            issues.append("same source already has public definition " + sid + "; do not create a duplicate")
        item = differences.get(sid, {})
        if not isinstance(item, dict) or not _present(item.get("reason")) or not _evidence(item.get("evidence")):
            issues.append("missing evidenced business difference from candidate " + sid)
    _, parameters = _binding(field)
    issues.extend(_parameter_issues(definition, parameters))
    return issues


def validate_decisions(dictionary, decisions):
    """Validate a batch transaction and return its dictionary copy.

    On any error catalog is an unchanged copy of the input, so callers cannot
    accidentally apply the valid prefix of an invalid plan. No files are touched.
    A valid candidate definition is still not a filling or business approval.
    """
    _catalog_parts(dictionary)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("decisions must be a list of objects")
    original, working, results, errors = deepcopy(dictionary), deepcopy(dictionary), [], []
    seen_fields = set()
    for index, decision in enumerate(decisions):
        action, field = decision.get("action"), decision.get("field", {})
        issues, sid = [], decision.get("standard_id")
        if not isinstance(field, dict):
            field, issues = {}, ["decision field must be an object"]
        field_id = field.get("field_id", field.get("id", decision.get("field_id")))
        # A field_id is local to a template; a batch spanning templates must give
        # their template_id. F001 in two templates is never an identity match.
        local_key = (field.get("template_id", decision.get("template_id")), field_id)
        if not isinstance(field_id, str) or not field_id:
            issues.append("each decision needs a template-local field_id")
        elif local_key in seen_fields:
            issues.append("duplicate decision for the same template field")
        seen_fields.add(local_key)
        field = deepcopy(field)
        field["field_id"] = field_id
        if action not in ACTIONS:
            issues.append("unknown action")
        match = match_fields(working, [field])["fields"][0]
        candidates = match["candidates"]
        _, parameters = _binding(field)
        if action == "needs_clarification":
            if not _present(decision.get("reason")):
                issues.append("clarification must state the unresolved question")
        elif action in {"reuse_existing_definition", "add_source_to_existing_definition"}:
            standard = working["standard_fields"].get(sid)
            if not standard:
                issues.append("selected public definition does not exist")
            else:
                issues.extend(_parameter_issues(standard, parameters))
                choice = next((c for c in candidates if c["standard_id"] == sid), None)
                exact_ids = {c["standard_id"] for c in candidates if c["exact_source_mapping_ids"]}
                if exact_ids and exact_ids != {sid}:
                    issues.append("source maps to another definition or is ambiguous: " + ",".join(sorted(exact_ids)))
                if choice and choice["conflicts"]:
                    issues.extend(choice["conflicts"])
                exact = bool(choice and choice["exact_source_mapping_ids"])
                if action == "add_source_to_existing_definition" or not exact:
                    issues.extend(_equivalence_issues(standard, field, decision.get("equivalence")))
                if action == "reuse_existing_definition" and _source(field) and not exact:
                    issues.append("unregistered/different source must be reviewed as add_source_to_existing_definition")
                if action == "add_source_to_existing_definition":
                    mapping = decision.get("source_mapping", {})
                    issues.extend(_source_mapping_issues(working, mapping, standard))
                    if not issues:
                        working["source_mappings"][mapping["mapping_id"]] = deepcopy(mapping)
                        basis = working["standard_fields"][sid].setdefault("name_basis", {})
                        mids = basis.setdefault("mapping_ids", [])
                        if mapping["mapping_id"] not in mids:
                            mids.append(mapping["mapping_id"])
                        if basis.get("preferred_mapping_id") is None:
                            basis["preferred_mapping_id"] = mapping["mapping_id"]
                        if basis.get("status") == "business_definition_source_pending":
                            basis["status"] = "business_definition_with_source_mapping"
                            basis["system"] = working["sources"][mapping["source_id"]].get("system")
                            basis["evidence"] = deepcopy(mapping["evidence"])
                            working["standard_fields"][sid].pop("retrieval", None)
                            working["standard_fields"][sid]["definition_status"] = "business_definition_source_mapping_documented"
        elif action == "new_definition_candidate":
            definition = deepcopy(decision.get("definition", {}))
            # Search both the extracted field and proposed business definition.
            # Omitting the source/name from one side cannot suppress dedup review.
            if isinstance(definition, dict):
                definition.setdefault("name_basis", {
                    "system": None, "preferred_mapping_id": None, "mapping_ids": [],
                    "status": "business_definition_source_pending"})
                definition.setdefault("retrieval", {
                    "source_id": None, "query_method_id": None, "endpoint": None,
                    "response_field": None, "parameters": None, "status": "pending_verification"})
                definition.setdefault("pending_questions", ["来源、取数方法及适用范围尚未核验。"])
                probe = {**definition, "field_id": field_id, "parameters": parameters}
                extra = match_fields(working, [probe])["fields"][0]["candidates"]
                candidates = list({c["standard_id"]: c for c in extra + candidates}.values())
            issues.extend(_new_definition_issues(working, definition, field, decision, candidates))
            if not issues:
                sid = definition["standard_id"]
                working["standard_fields"][sid] = deepcopy(definition)
        result = {"field_id": field_id, "action": action, "standard_id": sid,
                  "parameters": parameters, "valid": not issues, "can_fill": False,
                  "candidate_ids": [c["standard_id"] for c in candidates], "errors": issues}
        results.append(result)
        errors.extend({"index": index, "field_id": field_id, "message": issue} for issue in issues)
    valid = not errors
    catalog = working if valid else original
    return {"schema_version": 1, "valid": valid, "errors": errors, "decisions": results,
            "catalog": catalog, "changed": valid and catalog != original,
            "counts": {"definitions_before": len(original["standard_fields"]),
                       "definitions_after": len(catalog["standard_fields"]),
                       "mappings_before": len(original["source_mappings"]),
                       "mappings_after": len(catalog["source_mappings"])}}
