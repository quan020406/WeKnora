-- Restore the pre-000111 BM25 index without changing embeddings or NULL values.
-- Like the up migration, this rebuild blocks access until the DO block commits.
DO $$
BEGIN
    IF current_setting('app.skip_embedding', true) = 'true'
        OR to_regclass('embeddings') IS NULL THEN
        RETURN;
    END IF;

    DROP INDEX IF EXISTS embeddings_search_idx;
    CREATE INDEX embeddings_search_idx ON embeddings
    USING bm25 (id, knowledge_base_id, content, knowledge_id, chunk_id)
    WITH (
        key_field = 'id',
        text_fields = '{
            "content": {
                "tokenizer": {"type": "chinese_lindera"}
            }
        }'
    );
END $$;
