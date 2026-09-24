"""Exercise BM25 filter migrations on an isolated ParadeDB container.

Requires Python 3 and Docker. No ports or existing database volumes are used.
Run: python scripts/test_bm25_filter_fields.py
Use --baseline to demonstrate the missing filter pushdown before the migration.
"""
import argparse
import json
import pathlib
import subprocess
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = "000111_bm25_filter_fields"


class Database:
    def __init__(self, container):
        self.container = container

    def sql(self, query, database="weknora"):
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "psql", "-X", "-qAt",
             "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1"],
            input=query, text=True, encoding="utf-8", capture_output=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def migrate(self, paths, database="weknora", settings=""):
        for path in paths:
            self.sql(settings + "\n" + path.read_text(encoding="utf-8"), database)


def queries():
    # Same projection, ||| operator, filters and score ordering as KeywordsRetrieve.
    # ID is only a deterministic tie-breaker for comparing equal-score results.
    for term in ("database", "数据库", "unmatchedtoken"):
        for scope in ("", " AND knowledge_base_id = 'kb-0'",
                      " AND knowledge_base_id IN ('kb-0', 'kb-1')",
                      " AND knowledge_base_id = 'kb-0' AND knowledge_id IN ('doc-0', 'doc-4')",
                      " AND knowledge_base_id = 'kb-0' AND tag_id IN ('tag-0', 'tag-1')"):
            for limit in (1, 10, 100):
                yield (
                    "SELECT id, paradedb.score(id) AS score, content, source_id, source_type, "
                    "chunk_id, knowledge_id, knowledge_base_id, tag_id FROM embeddings "
                    f"WHERE content ||| '{term}' AND (is_enabled IS NULL OR is_enabled = true)"
                    f"{scope} ORDER BY score DESC, id LIMIT {limit}"
                )


def seed(db):
    db.sql("""
        INSERT INTO embeddings(source_id, source_type, chunk_id, knowledge_id,
            knowledge_base_id, tag_id, content, dimension, is_enabled)
        SELECT 'source-' || n, 0, 'chunk-' || n, 'doc-' || (n % 12),
            'kb-' || (n % 4), 'tag-' || (n % 3),
            repeat('database 数据库 检索 ', 1 + n % 7) || repeat('document ', n % 11),
            1024, CASE WHEN n % 13 = 0 THEN NULL WHEN n % 5 = 0 THEN false ELSE true END
        FROM generate_series(1, 20000) AS n;
        VACUUM ANALYZE embeddings;
    """)


def fingerprint(db):
    return db.sql("SELECT md5(string_agg(row_to_json(e)::text, '' ORDER BY id)) FROM embeddings e")


def index_definition(db, database="weknora"):
    return db.sql("SELECT pg_get_indexdef('embeddings_search_idx'::regclass)", database)


def filter_plan(db, database="weknora"):
    return json.loads(db.sql("""
        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        SELECT id, paradedb.score(id) AS score, content, source_id, source_type,
            chunk_id, knowledge_id, knowledge_base_id, tag_id
        FROM embeddings
        WHERE content ||| 'database' AND knowledge_base_id IN ('kb-0', 'kb-1')
            AND (is_enabled IS NULL OR is_enabled = true)
        ORDER BY score DESC LIMIT 10
    """, database))


def require_pushdown(plan):
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    scan_nodes = list(nodes(plan[0]["Plan"]))
    custom = [n for n in scan_nodes if n["Node Type"] == "Custom Scan"]
    assert custom, "query did not exercise the BM25 custom scan"
    assert all("Filter" not in n for n in scan_nodes), "Postgres still filters after the scan"
    encoded = json.dumps(custom).lower()
    assert "heap_filter" not in encoded, "BM25 filters still require heap_filter"
    assert "knowledge_base_id" in encoded and "is_enabled" in encoded, encoded


def require_fields(db, database="weknora"):
    definition = index_definition(db, database)
    assert "is_enabled" in definition, definition
    assert '"keyword"' in definition and '"fast"' in definition, definition


def check_writes(db):
    for enabled, expected in (("true", "1"), ("NULL", "1"), ("false", "0")):
        db.sql(f"""INSERT INTO embeddings(source_id,source_type,knowledge_base_id,
            content,dimension,is_enabled) VALUES
            ('write-test',0,'write-kb','uniquewritetoken',1024,{enabled})""")
        query = ("SELECT count(*) FROM embeddings WHERE content ||| 'uniquewritetoken' "
                 "AND knowledge_base_id='write-kb' AND (is_enabled IS NULL OR is_enabled=true)")
        assert db.sql(query) == expected, f"wrong enabled semantics for {enabled}"
        for state, count in (("false", "0"), ("NULL", "1"), ("true", "1")):
            db.sql(f"UPDATE embeddings SET is_enabled={state} WHERE source_id='write-test'")
            assert db.sql(query) == count, f"enabled update to {state} did not update the index"
        db.sql("UPDATE embeddings SET knowledge_base_id='other-kb' WHERE source_id='write-test'")
        assert db.sql(query) == "0", "KB update did not update the index"
        db.sql("DELETE FROM embeddings WHERE source_id='write-test'")
        assert db.sql("SELECT count(*) FROM embeddings WHERE content ||| 'uniquewritetoken'") == "0"


def run(db, output, baseline):
    paths = sorted((ROOT / "migrations/versioned").glob("*.up.sql"))
    up = ROOT / "migrations/versioned" / (MIGRATION + ".up.sql")
    down = up.with_name(MIGRATION + ".down.sql")
    previous = [p for p in paths if p.name < up.name]
    db.migrate(previous)
    seed(db)
    original_rows = fingerprint(db)
    original_index = index_definition(db)
    before = filter_plan(db)
    (output / "before-plan.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    assert "heap_filter" in json.dumps(before).lower(), "baseline did not reproduce issue #3301"
    statements = list(queries())
    results = [db.sql(q) for q in statements]
    print(f"Baseline: {len(results)} retrieval queries captured; heap_filter reproduced", flush=True)
    if baseline:
        require_pushdown(before)
        return

    db.migrate([up])
    require_fields(db)
    after = filter_plan(db)
    (output / "after-plan.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    require_pushdown(after)
    assert [db.sql(q) for q in statements] == results, "result IDs, ordering or scores changed"
    assert fingerprint(db) == original_rows, "migration changed embedding rows"
    check_writes(db)
    print("PASS: filter pushdown, result/score preservation and committed writes", flush=True)

    db.migrate([down])
    assert index_definition(db) == original_index, "rollback did not restore the index"
    assert [db.sql(q) for q in statements] == results, "rollback changed query results"
    db.migrate([up])
    require_pushdown(filter_plan(db))
    upgraded_index = index_definition(db)
    index_oid = db.sql("SELECT 'embeddings_search_idx'::regclass::oid")
    for path in (down, up):
        db.migrate([path], settings="SET app.skip_embedding='true';")
        assert index_definition(db) == upgraded_index, "skip_embedding changed the index definition"
        assert db.sql("SELECT 'embeddings_search_idx'::regclass::oid") == index_oid, "skip_embedding rebuilt the index"
    print("PASS: rollback, reapply, skip_embedding on an existing table", flush=True)

    for database in ("absent", "skipped", "bootstrap", "fresh"):
        db.sql(f"CREATE DATABASE {database}")
    db.migrate([up, down], "absent")
    assert db.sql("SELECT to_regclass('embeddings') IS NULL", "absent") == "t"
    db.migrate(paths, "skipped", "SET app.skip_embedding='true';")
    assert db.sql("SELECT to_regclass('embeddings') IS NULL", "skipped") == "t"
    db.migrate([ROOT / "migrations/paradedb/00-init-db.sql"], "bootstrap")
    db.migrate(paths, "bootstrap")
    db.migrate(paths, "fresh")
    for database in ("bootstrap", "fresh"):
        require_fields(db, database)
        db.sql("INSERT INTO embeddings(source_id,source_type,knowledge_base_id,content,dimension) "
               "VALUES ('fresh',0,'kb-0','新安装数据库检索',1024)", database)
        assert db.sql("SELECT count(*) FROM embeddings WHERE content ||| '数据库' "
                      "AND knowledge_base_id='kb-0' AND (is_enabled IS NULL OR is_enabled=true)", database) == "1"
    print("PASS: absent table, non-Postgres retriever, both fresh-install paths", flush=True)
    (output / "PASS").write_text("BM25 migration checks passed.\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="paradedb/paradedb:v0.22.6-pg17")
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    output = args.output_dir or pathlib.Path(tempfile.mkdtemp(prefix="weknora-bm25-"))
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("Use an empty output directory")
    container = "weknora-bm25-" + uuid.uuid4().hex[:12]
    print(f"Evidence: {output}", flush=True)
    subprocess.run(["docker", "run", "-d", "--name", container, "--network", "none",
                    "--shm-size=512m", "-e", "POSTGRES_PASSWORD=isolated-bm25-test",
                    "-e", "POSTGRES_DB=weknora", args.image], check=True, capture_output=True)
    try:
        for _ in range(90):
            ready = subprocess.run(["docker", "exec", container, "pg_isready", "-h", "127.0.0.1",
                                    "-U", "postgres", "-d", "weknora"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("ParadeDB did not start")
        run(Database(container), output, args.baseline)
    finally:
        logs = subprocess.run(["docker", "logs", container], capture_output=True)
        (output / "postgres.log").write_bytes(logs.stdout + logs.stderr)
        # Remove only the random container and anonymous volume created above.
        subprocess.run(["docker", "rm", "-fv", container], check=True, capture_output=True)


if __name__ == "__main__":
    main()
