# unicode-precheck

Catch the schema identifiers that NVIDIA/auto-ontology will silently blank, fragment or
fail to classify — **before** you run semantic compilation.

Upstream: https://github.com/NVIDIA/auto-ontology

## 上游是什么，它缺什么

auto-ontology builds a semantic layer over an existing database. Four of its normalization
paths are ASCII-only, and each one turns a non-Latin identifier into a *silent* result
instead of an error:

| Stage | Upstream code | What happens to `電話番号` / `téléphone` |
| --- | --- | --- |
| fallback Term name | `semantic/deterministic.py#L51` | `re.split(r"[^A-Za-z0-9]+")` → `""`; the empty Term is stored and the embedding pass skips it ([#468](https://github.com/NVIDIA/auto-ontology/issues/468)) |
| PII rule normalizer | `pii_detection/rules.py#L18` | `[^a-z0-9]+` → `""`; the rules engine answers `REVIEW "Column name is empty."` with `source="rules"`, so the column is marked **processed** and the LLM classifier never runs ([#469](https://github.com/NVIDIA/auto-ontology/issues/469)) |
| synonym tokenizer | `dal/search.py#L228` | `[a-z0-9]+` → `[]`; synonym whole-word search can never match the term ([#472](https://github.com/NVIDIA/auto-ontology/issues/472)) |
| date format inference | `semantic/date_format.py#L39` | no candidate parses `2024.03.15`, `2024년 3월 15일`; the column gets no format hint, and SQL "runs, returns nothing, and looks correct" ([#473](https://github.com/NVIDIA/auto-ontology/issues/473)) |

These are all open, code-linked reports against commit `c447003f`. None of them ships a
preflight: you only find out after compilation, when a Term is empty or a PII column was
skipped. This tool is that preflight.

## 这个工具做什么 / 不做什么

**Does:** reads your `CREATE TABLE` DDL offline, reproduces the four upstream normalizers
byte-for-byte, and reports every identifier that loses information — with the **shortest
substring that already collapses** (the witness), the Unicode code points, an ASCII alias
you can rename to, and the upstream file:line as evidence.

**Does not:** connect to the database, modify your DDL, run auto-ontology, or prove what the
LLM would have produced. It audits the deterministic normalization boundary only.

## 安装

Python 3.9+, standard library only. Nothing to install.

```bash
git clone git@github.com:linmoxi33/unicode-precheck.git
cd unicode-precheck
python3 -m unittest discover -s tests -v      # 18 tests, all offline
```

## 用法

```bash
./bin/unicode-precheck examples/in/erp.sql examples/in/fr_dates.sql \
    --samples examples/in/samples.json
```

Text output (exit code 1 because findings exist):

```
HIGH    U001 examples/in/erp.sql:顧客マスタ [term_name_collapse] fallback Term name is empty; ...
        witness '顧' (U+9867) CJK UNIFIED IDEOGRAPH-9867
HIGH    U003 examples/in/erp.sql:顧客マスタ.電話番号 [pii_normalize_collapse] ...
MEDIUM  U002 examples/in/erp.sql:Société [term_name_fragment] fallback Term name 'Soci T' ...
        witness 'é' (U+00E9) LATIN SMALL LETTER E WITH ACUTE
        suggested ascii alias: societe
```

`--json` emits one object (`examples/out/report.json` is a real run, not hand-written):

```bash
./bin/unicode-precheck --json examples/in/erp.sql examples/in/fr_dates.sql \
    --samples examples/in/samples.json > report.json
python3 -c "import json;print(json.load(open('report.json'))['summary'])"
# {'by_rule': {'U001': 2, 'U002': 2, 'U003': 4, 'U005': 4, 'U006': 1},
#  'by_severity': {'high': 6, 'medium': 7}, 'columns': 12, 'findings': 13, 'tables': 5}
```

| Code | Severity | Meaning |
| --- | --- | --- |
| U001 | high | fallback Term name collapses to empty |
| U002 | medium | normalizer fragments an accented word |
| U003 | high | PII normalizer collapses; column marked processed unclassified |
| U005 | medium | synonym tokenizer returns no tokens |
| U006 | medium | no upstream date candidate parses every sample |

`--samples` takes `{"table.column": ["2024.03.15", ...]}`; only date/time columns are checked.

| Exit code | Meaning |
| --- | --- |
| 0 | no finding at or above `--fail-on` (default `low`) |
| 1 | at least one finding at or above `--fail-on` |
| 2 | usage or input error (unreadable file, non-UTF-8, no `CREATE TABLE`, bad `--samples`) |

## 本轮创新

Most linters grep for a pattern; this one makes **information loss** the falsifiable object:
for each identifier it returns the shortest substring that already collapses under the
upstream regex, plus a per-code-point witness, so `顧客マスタ` is not "suspicious" but proven
to yield zero tokens at a one-character boundary — and the same finding is attributed to the
four distinct upstream stages that each lose a different thing (name, PII decision, synonym
tokens, date hint). It runs before compilation, which is the only point where the user can
still rename. It is not a wrapper: no network, no auto-ontology import, all four regexes and
the 23-candidate date table are reproduced from upstream and asserted in tests.

## 局限

- Only parses `CREATE TABLE` (schema-qualified, `"..."`/`` `...` ``/`[...]` quoted, bare).
  `CREATE VIEW`, `ALTER`, and dialect type bodies are ignored.
- U002 is a heuristic: it flags names where the folded ASCII form differs from the
  normalizer's token string; transliterations that happen to agree are not flagged.
- U006 needs user-supplied samples; with no `--samples`, no date finding is produced.
- Severities reflect consequences documented in the upstream issues, not a quantified error
  rate. No claim is made about how often real schemas hit these paths.
- Witness minimality is by substring length over the identifier, not a general
  "smallest input" claim.

## License

MIT. Mirrored constants are from NVIDIA/auto-ontology (Apache-2.0); see `NOTICE`.
