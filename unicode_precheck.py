#!/usr/bin/env python3
"""unicode-precheck: offline preflight for non-ASCII identifiers in a SQL schema.

It reproduces four ASCII-only normalization paths of NVIDIA/auto-ontology and
reports, before semantic compilation runs, which identifiers will lose their
name / tokens / date format.  Zero third-party dependencies, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime

VERSION = "0.1.0"
UPSTREAM = "NVIDIA/auto-ontology"
UPSTREAM_COMMIT = "22e22c894df8cde7a78f854ea40476a8577e9a3d"

# ---------------------------------------------------------------------------
# Faithful copies of the upstream normalization paths under audit.
# Each function names the upstream file:line it mirrors. Keep them identical:
# the findings are only meaningful if these agree with upstream byte for byte.
# ---------------------------------------------------------------------------


def to_term_name(table_name: str) -> str:
    """Mirror of auto_ontology/semantic/deterministic.py:51 (fallback Term name)."""
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", table_name.strip())
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", separated)
    parts = re.split(r"[^A-Za-z0-9]+", separated)
    return " ".join(part if part.isupper() else part.capitalize() for part in parts if part)


def normalize(value: str | None) -> str:
    """Mirror of auto_ontology/pii_detection/rules.py:18 (PII rule normalizer)."""
    if not value:
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


_WORD_RE = re.compile(r"[a-z0-9]+")


def synonym_word_tokens(search_term: str) -> list[str]:
    """Mirror of auto_ontology/dal/search.py:228 + :276 (synonym whole-word tokens)."""
    return _WORD_RE.findall(search_term.lower())


# Mirror of auto_ontology/semantic/date_format.py:39, in upstream order.
_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("%Y-%m-%d %H:%M:%S.%f", "YYYY-MM-DD HH:MM:SS.f"),
    ("%Y-%m-%dT%H:%M:%S.%f", "YYYY-MM-DDTHH:MM:SS.f"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "YYYY-MM-DDTHH:MM:SS"),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"),
    ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%d/%m/%y", "DD/MM/YY"),
    ("%m/%d/%y", "MM/DD/YY"),
    ("%d-%m-%y", "DD-MM-YY"),
    ("%m-%d-%y", "MM-DD-YY"),
    ("%d.%m.%y", "DD.MM.YY"),
    ("%Y%m%d", "YYYYMMDD"),
    ("%Y%m", "YYYYMM"),
    ("%y%m%d", "YYMMDD"),
    ("%H:%M:%S", "HH:MM:SS"),
    ("%H:%M", "HH:MM"),
    ("%Y", "YYYY"),
)
_MAX_VALUE_LEN = 40
_YEAR_MIN, _YEAR_MAX = 1900, 2099
_DATE_TYPE_TOKENS = ("date", "time", "timestamp", "datetime")


def _parses(fmt: str, value: str) -> bool:
    """Parse first, then apply the upstream year window when the format has a year."""
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:
        return False
    if "%Y" in fmt or "%y" in fmt:
        return _YEAR_MIN <= parsed.year <= _YEAR_MAX
    return True


def infer_date_format(values) -> str | None:
    """Mirror of auto_ontology/semantic/date_format.py:152 (validation, not shape)."""
    sample = []
    for v in values:
        text = "" if v is None else str(v).strip()
        if not text or len(text) > _MAX_VALUE_LEN:
            continue
        if text not in sample:
            sample.append(text)
    if not sample:
        return None
    for fmt, label in _CANDIDATES:
        if all(_parses(fmt, v) for v in sample):
            return label
    return None


# ---------------------------------------------------------------------------
# Witnesses: the shortest substring that already loses everything.
# ---------------------------------------------------------------------------


def _tokenize(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", name) if t]


def minimal_collapse_witness(name: str) -> dict | None:
    """Shortest non-empty substring that yields no ASCII token, plus chars.

    A witness is minimal when it collapses and every proper substring either is
    empty or does not collapse; tests assert both halves of that statement.
    """
    best: str | None = None
    for start in range(len(name)):
        for end in range(start + 1, len(name) + 1):
            window = name[start:end]
            if not any(c.isalnum() for c in window):
                continue  # a bare separator is not a lost name
            if not _tokenize(window):
                if best is None or len(window) < len(best):
                    best = window
                break  # extending this start cannot give a shorter witness
    if best is None:
        return None
    return {
        "substring": best,
        "length": len(best),
        "codepoints": ["U+%04X" % ord(c) for c in best],
        "names": [unicodedata.name(c, "UNASSIGNED") for c in best],
    }


def ascii_fold(value: str) -> str:
    """Lossy ASCII alias: strip accents, drop everything that is not [A-Za-z0-9]."""
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", folded).strip("_").lower()


def suggested_term_name(table_name: str) -> str:
    """A name-preserving Term: keep Unicode letters/digits, split on separators."""
    parts = re.split(r"[^\w]+", table_name.strip(), flags=re.UNICODE)
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


# ---------------------------------------------------------------------------
# DDL parsing: the same textual input upstream's catalog ingest reads.
# ---------------------------------------------------------------------------


class InputError(Exception):
    pass


def _split_identifier(item: str) -> tuple[str, str]:
    item = item.strip()
    if not item:
        return "", ""
    if item[0] in '"`[':
        close = "]" if item[0] == "[" else item[0]
        idx = item.find(close, 1)
        if idx < 0:
            raise InputError("unterminated quoted identifier: %r" % item[:40])
        return item[1:idx], item[idx + 1 :].strip()
    match = re.match(r"([^\s(]+)(.*)$", item, re.S)
    if not match:
        return "", ""
    return match.group(1), match.group(2).strip()


_CONSTRAINT = re.compile(
    r"^(primary|foreign|unique|check|constraint|key|index|fulltext|period)\b", re.I
)


def _split_top_level(body: str) -> list[str]:
    out, depth, quote, buf = [], 0, None, []
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in '"`[':
            quote = "]" if ch == "[" else ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"((?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z0-9_$]+)(?:\s*\.\s*(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z0-9_$]+))?)"
    r"\s*\((.*?)\)\s*(?:ENGINE|;|$)",
    re.I | re.S,
)


def parse_ddl(text: str, source: str = "<string>") -> list[dict]:
    tables = []
    for match in _CREATE.finditer(text):
        raw_name = match.group(1)
        pieces = [_split_identifier(p)[0] for p in re.split(r"\s*\.\s*", raw_name)]
        table = pieces[-1] if pieces else raw_name
        columns = []
        for item in _split_top_level(match.group(2)):
            item = item.strip()
            if not item or _CONSTRAINT.match(item):
                continue
            col, rest = _split_identifier(item)
            if not col or not rest:
                continue
            data_type = re.split(r"[\s(]", rest, maxsplit=1)[0].strip().rstrip(",")
            columns.append({"name": col, "type": data_type.lower()})
        if not columns:
            continue
        tables.append({"name": table, "schema": pieces[0] if len(pieces) > 1 else None,
                       "columns": columns, "source": source})
    if not tables:
        raise InputError("no CREATE TABLE with at least one column found in " + source)
    return tables


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _finding(rule, severity, kind, where, name, message, evidence, extra=None):
    item = {
        "rule": rule,
        "severity": severity,
        "kind": kind,
        "where": where,
        "identifier": name,
        "message": message,
        "evidence": evidence,
    }
    if extra:
        item.update(extra)
    return item


def audit_tables(tables: list[dict], samples: dict[str, list[str]] | None = None) -> list[dict]:
    samples = samples or {}
    findings: list[dict] = []
    for table in tables:
        tname = table["name"]
        where = "%s:%s" % (table.get("source") or "<input>", table.get("line") or tname)
        term = to_term_name(tname)
        if not term:
            w = minimal_collapse_witness(tname)
            findings.append(_finding(
                "U001", "high", "term_name_collapse", where, tname,
                "fallback Term name is empty; semantic compilation stores \"\" and the "
                "embedding pass skips the Term",
                "auto_ontology/semantic/deterministic.py:51",
                {"witness": w, "suggested_term": suggested_term_name(tname),
                 "suggested_ascii_alias": ascii_fold(tname)},
            ))
        else:
            folded = ascii_fold(tname)
            if folded and term.replace(" ", "").lower() != folded.replace("_", ""):
                w = minimal_collapse_witness(tname)
                findings.append(_finding(
                    "U002", "medium", "term_name_fragment", where, tname,
                    "fallback Term name %r is not a faithful rendering of the identifier "
                    "(accented letters are split, not transliterated)" % term,
                    "auto_ontology/semantic/deterministic.py:51",
                    {"witness": w, "rendered": term,
                     "suggested_ascii_alias": folded},
                ))
        for col in table["columns"]:
            cname = col["name"]
            cwhere = "%s:%s.%s" % (table.get("source") or "<input>", tname, cname)
            norm = normalize(cname)
            if not norm and cname:
                findings.append(_finding(
                    "U003", "high", "pii_normalize_collapse", cwhere, cname,
                    "PII rule normalizer emits \"\"; rules return REVIEW \"Column name is "
                    "empty\" with source=rules, so the column is marked processed and the "
                    "LLM classifier never sees it",
                    "auto_ontology/pii_detection/rules.py:18",
                    {"witness": minimal_collapse_witness(cname),
                     "suggested_ascii_alias": ascii_fold(cname)},
                ))
            elif norm and cname.isascii() is False:
                folded = ascii_fold(cname)
                if folded and norm.replace(" ", "") != folded.replace("_", ""):
                    findings.append(_finding(
                        "U002", "medium", "pii_normalize_fragment", cwhere, cname,
                        "PII rule normalizer splits the accented word into %r instead of %r"
                        % (norm, folded),
                        "auto_ontology/pii_detection/rules.py:18",
                        {"witness": minimal_collapse_witness(cname),
                         "normalized": norm, "suggested_ascii_alias": folded},
                    ))
            if not cname.isascii():
                toks = synonym_word_tokens(cname)
                if not toks:
                    findings.append(_finding(
                        "U005", "medium", "synonym_token_collapse", cwhere, cname,
                        "synonym tokenizer [a-z0-9]+ yields []; a search for this term can "
                        "never whole-word match a synonym",
                        "auto_ontology/dal/search.py:228",
                        {"witness": minimal_collapse_witness(cname),
                         "suggested_ascii_alias": ascii_fold(cname)},
                    ))
            if "date" in col.get("type", "") or "time" in col.get("type", ""):
                key = "%s.%s" % (tname, cname)
                values = samples.get(key) or samples.get("%s.%s" % (tname, cname)) or []
                if values and infer_date_format(values) is None:
                    findings.append(_finding(
                        "U006", "medium", "date_format_unclaimed", cwhere, cname,
                        "none of the %d upstream date candidates parses every sample; the "
                        "column gets no format hint and SQL can run yet match nothing"
                        % len(_CANDIDATES),
                        "auto_ontology/semantic/date_format.py:39",
                        {"samples": list(values)[:5]},
                    ))
    return findings

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
_RULES = {
    "U001": "fallback Term name collapses to empty (#468)",
    "U002": "normalizer fragments an accented word (#468/#469)",
    "U003": "PII normalizer collapses and silently marks the column processed (#469)",
    "U005": "synonym tokenizer yields no tokens (#472)",
    "U006": "no upstream date candidate parses the samples (#473)",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unicode-precheck",
        description="Preflight a SQL schema for non-ASCII identifiers that NVIDIA/auto-ontology "
                    "will silently drop, fragment, or fail to classify.",
    )
    p.add_argument("ddl", nargs="+", help="DDL .sql files to check (read-only)")
    p.add_argument("--json", action="store_true", help="emit one JSON report on stdout")
    p.add_argument("--samples", help="JSON file: {\"table.column\": [\"2024.03.15\", ...]}")
    p.add_argument("--fail-on", choices=["low", "medium", "high"], default="low",
                   help="lowest severity that makes the exit code 1 (default: low)")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


def _load(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc.strerror or exc)) from exc
    except UnicodeDecodeError as exc:
        raise InputError("%s is not UTF-8: %s" % (path, exc)) from exc


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    samples = None
    if args.samples:
        try:
            samples = json.loads(_load(args.samples))
        except ValueError as exc:
            print("unicode-precheck: --samples is not JSON: %s" % exc, file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(samples, dict):
            print("unicode-precheck: --samples must be a JSON object", file=sys.stderr)
            return EXIT_USAGE
    tables: list[dict] = []
    try:
        for path in args.ddl:
            tables.extend(parse_ddl(_load(path), source=path))
    except InputError as exc:
        print("unicode-precheck: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    findings = audit_tables(tables, samples)
    threshold = _SEVERITY_RANK[args.fail_on]
    tripped = [f for f in findings if _SEVERITY_RANK[f["severity"]] >= threshold]
    by_rule: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    if args.json:
        report = {
            "tool": "unicode-precheck",
            "version": VERSION,
            "upstream": UPSTREAM,
            "upstream_commit": UPSTREAM_COMMIT,
            "inputs": list(args.ddl),
            "summary": {
                "tables": len(tables),
                "columns": sum(len(t["columns"]) for t in tables),
                "findings": len(findings),
                "tripped": len(tripped),
                "by_severity": by_severity,
                "by_rule": by_rule,
                "fail_on": args.fail_on,
            },
            "rules": _RULES,
            "findings": findings,
        }
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for f in findings:
            print("%-7s %s %s [%s] %s" % (f["severity"].upper(), f["rule"], f["where"],
                                          f["kind"], f["message"]))
            w = f.get("witness")
            if w:
                print("        witness %r (%s) %s" % (w["substring"], ",".join(w["codepoints"]),
                                                       " ".join(w["names"])))
            if f.get("suggested_ascii_alias"):
                print("        suggested ascii alias: %s" % f["suggested_ascii_alias"])
        print("unicode-precheck: %d table(s), %d finding(s), %d at/above %s"
              % (len(tables), len(findings), len(tripped), args.fail_on))
    return EXIT_FINDINGS if tripped else EXIT_OK


def main() -> None:
    try:
        sys.exit(run(sys.argv[1:]))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(EXIT_USAGE)


if __name__ == "__main__":
    main()
