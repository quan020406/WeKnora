-- Put the filters used by KeywordsRetrieve inside the BM25 index (#3301).
-- The single DO statement makes replacement atomic: a failed CREATE restores
-- the old index. Rebuilding takes locks and blocks access to embeddings until
-- commit; schedule upgrades of large databases during a maintenance window.
DO $$
BEGIN
    IF current_setting('app.skip_embedding', true) = 'true'
        OR to_regclass('embeddings') IS NULL THEN
        RETURN;
    END IF;

    DROP INDEX IF EXISTS embeddings_search_idx;
    CREATE INDEX embeddings_search_idx ON embeddings
    USING bm25 (id, knowledge_base_id, content, knowledge_id, chunk_id, is_enabled)
    WITH (
        key_field = 'id',
        text_fields = '{
            "content": {"tokenizer": {"type": "chinese_lindera"}},
            "knowledge_base_id": {"tokenizer": {"type": "keyword"}, "fast": true}
        }',
        boolean_fields = '{"is_enabled": {"fast": true}}'
    );
END $$;
