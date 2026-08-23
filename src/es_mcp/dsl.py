"""Deterministic builder: structured spec -> Elasticsearch Query DSL.

The model describes intent in a small, checkable schema; this module emits valid
DSL. That is far more reliable than hand-writing JSON, and the result is then
validated against the real index via ``_validate/query?rewrite=true``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .errors import SafetyError

Op = Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "exists", "missing", "prefix", "wildcard", "match", "match_phrase"]


class Filter(BaseModel):
    field: str
    op: Op = "eq"
    value: Any = None


class Agg(BaseModel):
    name: str
    type: Literal["terms", "date_histogram", "histogram", "avg", "sum", "min", "max", "cardinality", "percentiles", "stats"]
    field: str
    size: int | None = None
    interval: str | None = Field(default=None, description="calendar/fixed interval for date_histogram, e.g. 1h, 1d")
    sub_aggs: list["Agg"] | None = None


class QuerySpec(BaseModel):
    text: str | None = Field(default=None, description="free-text search phrase")
    text_fields: list[str] | None = Field(default=None, description="fields for the text search; omit for all")
    operator: Literal["and", "or"] = "or"
    filters: list[Filter] = Field(default_factory=list)
    must_not: list[Filter] = Field(default_factory=list)
    should: list[Filter] = Field(default_factory=list)
    time_field: str | None = None
    time_from: str | None = Field(default=None, description="e.g. now-24h or 2026-01-01T00:00:00Z")
    time_to: str | None = "now"
    sort: list[str] = Field(default_factory=list, description="e.g. ['@timestamp:desc', '_score:desc']")
    size: int | None = None
    from_: int = Field(default=0, alias="from")
    source_includes: list[str] | None = None
    aggs: list[Agg] = Field(default_factory=list)
    highlight_fields: list[str] | None = None
    track_total_hits: bool | int = True

    model_config = {"populate_by_name": True}


Agg.model_rebuild()


def _clause(f: Filter) -> dict[str, Any]:
    fld, op, val = f.field, f.op, f.value
    if op == "eq":
        return {"term": {fld: val}}
    if op == "in":
        return {"terms": {fld: list(val) if isinstance(val, (list, tuple)) else [val]}}
    if op in ("gt", "gte", "lt", "lte"):
        return {"range": {fld: {op: val}}}
    if op == "exists":
        return {"exists": {"field": fld}}
    if op == "prefix":
        return {"prefix": {fld: val}}
    if op == "wildcard":
        return {"wildcard": {fld: val}}
    if op == "match":
        return {"match": {fld: val}}
    if op == "match_phrase":
        return {"match_phrase": {fld: val}}
    raise SafetyError(f"unsupported filter op for positive clause: {op}")


def _agg(a: Agg) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if a.type == "terms":
        body = {"terms": {"field": a.field, "size": a.size or 10}}
    elif a.type == "date_histogram":
        body = {"date_histogram": {"field": a.field, "fixed_interval": a.interval or "1h"}}
        if a.interval in ("1d", "1w", "1M", "1q", "1y", "day", "week", "month", "quarter", "year"):
            body = {"date_histogram": {"field": a.field, "calendar_interval": a.interval}}
    elif a.type == "histogram":
        body = {"histogram": {"field": a.field, "interval": float(a.interval or 10)}}
    else:
        body = {a.type: {"field": a.field}}
    if a.sub_aggs:
        body["aggs"] = {s.name: _agg(s) for s in a.sub_aggs}
    return body


def build_query(spec: QuerySpec) -> dict[str, Any]:
    must: list[dict[str, Any]] = []
    filter_: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []
    should: list[dict[str, Any]] = []

    if spec.text:
        if spec.text_fields:
            must.append(
                {
                    "multi_match": {
                        "query": spec.text,
                        "fields": spec.text_fields,
                        "operator": spec.operator,
                        "type": "best_fields",
                    }
                }
            )
        else:
            must.append({"query_string": {"query": spec.text, "default_operator": spec.operator.upper()}})

    for f in spec.filters:
        if f.op == "neq":
            must_not.append({"term": {f.field: f.value}})
        elif f.op == "not_in":
            must_not.append({"terms": {f.field: list(f.value)}})
        elif f.op == "missing":
            must_not.append({"exists": {"field": f.field}})
        else:
            filter_.append(_clause(f))

    for f in spec.must_not:
        must_not.append(_clause(f) if f.op not in ("neq", "not_in", "missing") else {"term": {f.field: f.value}})
    for f in spec.should:
        should.append(_clause(f))

    if spec.time_field and (spec.time_from or spec.time_to):
        rng: dict[str, Any] = {}
        if spec.time_from:
            rng["gte"] = spec.time_from
        if spec.time_to:
            rng["lte"] = spec.time_to
        filter_.append({"range": {spec.time_field: rng}})

    bool_: dict[str, Any] = {}
    if must:
        bool_["must"] = must
    if filter_:
        bool_["filter"] = filter_
    if must_not:
        bool_["must_not"] = must_not
    if should:
        bool_["should"] = should
        bool_["minimum_should_match"] = 1

    query = {"bool": bool_} if bool_ else {"match_all": {}}

    body: dict[str, Any] = {"query": query, "track_total_hits": spec.track_total_hits}
    if spec.size is not None:
        body["size"] = spec.size
    if spec.from_:
        body["from"] = spec.from_
    if spec.sort:
        body["sort"] = [
            {s.split(":")[0]: {"order": s.split(":")[1]}} if ":" in s else s for s in spec.sort
        ]
    if spec.source_includes:
        body["_source"] = spec.source_includes
    if spec.aggs:
        body["aggs"] = {a.name: _agg(a) for a in spec.aggs}
        if spec.size is None:
            body["size"] = 0
    if spec.highlight_fields:
        body["highlight"] = {"fields": {f: {} for f in spec.highlight_fields}}
    return body
