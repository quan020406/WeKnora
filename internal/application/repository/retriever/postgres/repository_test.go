package postgres

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Tencent/WeKnora/internal/types"
	pgdriver "gorm.io/driver/postgres"
	"gorm.io/gorm"
)

func TestKeywordsRetrieveFilterScores(t *testing.T) {
	for _, scoped := range []bool{false, true} {
		t.Run(fmt.Sprintf("scoped=%t", scoped), func(t *testing.T) {
			conn, mock, err := sqlmock.New(sqlmock.QueryMatcherOption(sqlmock.QueryMatcherFunc(
				func(_, query string) error {
					const enabled = "must => paradedb.all(), must_not => paradedb.term('is_enabled', false)"
					const kbTerms = "paradedb.term('knowledge_base_id', $1::text), " +
						"paradedb.term('knowledge_base_id', $2::text)"
					// Both filters must match without contributing to relevance.
					if !strings.Contains(query, "id @@@ paradedb.const_score(0, paradedb.boolean(") ||
						!strings.Contains(query, enabled) {
						return fmt.Errorf("enabled filter can change BM25 scores: %s", query)
					}
					if scoped && (!strings.Contains(query, "id @@@ paradedb.const_score(0, paradedb.term_set(ARRAY[") ||
						!strings.Contains(query, kbTerms)) {
						return fmt.Errorf("KB filter is not a parameterized zero-score term set: %s", query)
					}
					if strings.Contains(query, "kb-'quoted") || !strings.Contains(query, "content |||") ||
						!strings.Contains(query, `ORDER BY "score" DESC LIMIT`) {
						return fmt.Errorf("query lost parameter binding or content score ordering: %s", query)
					}
					return nil
				})))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			db, err := gorm.Open(pgdriver.New(pgdriver.Config{Conn: conn}), &gorm.Config{})
			if err != nil {
				t.Fatal(err)
			}
			params := types.RetrieveParams{Query: "database", TopK: 5}
			expected := mock.ExpectQuery("keyword query")
			if scoped {
				params.KnowledgeBaseIDs = []string{"kb-'quoted", "kb-2"}
				params.KnowledgeIDs = []string{"doc-1"}
				params.TagIDs = []string{"tag-1"}
				expected.WithArgs("kb-'quoted", "kb-2", "doc-1", "tag-1", "database", 5)
			} else {
				expected.WithArgs("database", 5)
			}
			expected.WillReturnRows(sqlmock.NewRows([]string{"id", "score"}))
			_, err = (&pgRepository{db: db}).KeywordsRetrieve(context.Background(), params)
			if err != nil {
				t.Fatal(err)
			}
			if err := mock.ExpectationsWereMet(); err != nil {
				t.Fatal(err)
			}
		})
	}
}
