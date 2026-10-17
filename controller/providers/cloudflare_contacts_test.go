package providers

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// d1Fake answers D1 query calls by database, recording each statement.
type d1Fake struct {
	mu         sync.Mutex
	statements []d1Statement
	// tables says which databases hold the contact table; rows are what
	// SELECT * answers.
	tables map[string]bool
	rows   []map[string]any
	// changes is what an UPDATE or DELETE reports it changed.
	changes int
}

func (f *d1Fake) handler(database string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var statement d1Statement
		_ = json.NewDecoder(r.Body).Decode(&statement)
		f.mu.Lock()
		f.statements = append(f.statements, statement)
		f.mu.Unlock()
		result := map[string]any{"success": true, "results": []any{}, "meta": map[string]any{"changes": 0}}
		switch {
		case strings.Contains(statement.SQL, "sqlite_master"):
			if f.tables[database] {
				result["results"] = []any{map[string]any{"name": contactTable}}
			}
		case strings.HasPrefix(statement.SQL, "SELECT *"):
			result["results"] = f.rows
		default:
			result["meta"] = map[string]any{"changes": f.changes}
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "errors": []any{}, "result": []any{result}})
	}
}

func contactRoutes(fake *d1Fake) map[string]http.HandlerFunc {
	return map[string]http.HandlerFunc{
		"GET /accounts": answer([]any{map[string]any{"id": "acct"}}),
		"GET /accounts/acct/d1/database": answer([]any{
			map[string]any{"uuid": "db-contacts", "name": "contacts"},
			map[string]any{"uuid": "db-other", "name": "other"},
		}),
		"POST /accounts/acct/d1/database/db-contacts/query": fake.handler("db-contacts"),
		"POST /accounts/acct/d1/database/db-other/query":    fake.handler("db-other"),
	}
}

// Only a database holding the contact table is read, and each row becomes a
// record naming its connection, account and database. D1's numbers and nulls
// read as text.
func TestContactSubmissionsReadOnlyTheDatabaseHoldingTheTable(t *testing.T) {
	fake := &d1Fake{
		tables: map[string]bool{"db-contacts": true},
		rows: []map[string]any{{
			"id": 7, "name": "Example Person", "email": "person@example.com", "message": "Hello",
			"status": "unread", "country": nil, "turnstile": 1, "extra_column": "ignored",
		}},
	}
	r, _ := newCloudflare(t, contactRoutes(fake))

	records, err := r.cloudflareContactSubmissions(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 {
		t.Fatalf("records: %+v", records)
	}
	record := records[0].(runtime.CloudflareContactSubmissionRecord)
	want := runtime.CloudflareContactSubmissionRecord{
		ConnectionRef: "example-api", AccountID: "acct", DatabaseID: "db-contacts", Database: "contacts",
		ID: 7, Name: "Example Person", Email: "person@example.com", Message: "Hello", Status: "unread", Turnstile: "1",
	}
	if record != want {
		t.Errorf("record\n got %+v\nwant %+v", record, want)
	}
	selects := 0
	for _, statement := range fake.statements {
		if strings.HasPrefix(statement.SQL, "SELECT *") {
			selects++
			if len(statement.Params) != 1 || statement.Params[0] != float64(contactReadLimit) {
				t.Errorf("the read is bounded: %+v", statement.Params)
			}
		}
	}
	if selects != 1 {
		t.Errorf("read %d tables, want the one database holding it", selects)
	}
}

// A refused query is the reading's failure, never an empty inbox.
func TestContactSubmissionsRefusedIsAnError(t *testing.T) {
	routes := contactRoutes(&d1Fake{})
	routes["POST /accounts/acct/d1/database/db-contacts/query"] = refusing(http.StatusForbidden, 10000, "Authentication error")
	r, _ := newCloudflare(t, routes)

	if _, err := r.cloudflareContactSubmissions(t.Context()); err == nil {
		t.Fatal("a refused D1 query read as an empty inbox")
	}
}

func inboxSpec() Object {
	return Object{
		"connection_ref": "example-api", "account_id": "acct", "database_id": "db-contacts",
		"reviews":   []any{map[string]any{"id": 7, "status": "replied", "assigned_to": "", "admin_notes": "answered"}},
		"deletions": []any{9},
	}
}

// Applying runs one guarded UPDATE per review and one DELETE per deletion,
// against the database the declaration names, through cloudflare_api.
func TestContactInboxReconcileAppliesEachChange(t *testing.T) {
	fake := &d1Fake{changes: 1}
	r, _ := newCloudflare(t, contactRoutes(fake))

	result, err := r.runAction(runtime.ResourceKindCloudflareContactInbox, "reconcile", t.Context(), inboxSpec(), nil, true)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Changed || result.Status.(CloudflareContactInboxStatus) != (CloudflareContactInboxStatus{Given: 2, Changed: 2}) {
		t.Errorf("result %+v", result)
	}
	if len(fake.statements) != 2 {
		t.Fatalf("statements %+v", fake.statements)
	}
	update, remove := fake.statements[0], fake.statements[1]
	if !strings.HasPrefix(update.SQL, "UPDATE contact_submissions SET status = ?") || !strings.Contains(update.SQL, "status IS NOT ?") {
		t.Errorf("update %q", update.SQL)
	}
	if update.Params[0] != "replied" || update.Params[2] != "answered" || update.Params[3] != float64(7) {
		t.Errorf("update params %+v", update.Params)
	}
	if remove.SQL != "DELETE FROM contact_submissions WHERE id = ?" || remove.Params[0] != float64(9) {
		t.Errorf("delete %q %+v", remove.SQL, remove.Params)
	}
}

// Applying what is already so changes nothing, and planning writes nothing.
func TestContactInboxReconcileIsIdempotentAndPlansWithoutWriting(t *testing.T) {
	fake := &d1Fake{changes: 0}
	r, _ := newCloudflare(t, contactRoutes(fake))

	result, err := r.runAction(runtime.ResourceKindCloudflareContactInbox, "reconcile", t.Context(), inboxSpec(), nil, true)
	if err != nil || result.Changed {
		t.Fatalf("a second apply changed something: %v %+v", err, result)
	}
	before := len(fake.statements)
	plan, err := r.runAction(runtime.ResourceKindCloudflareContactInbox, "reconcile", t.Context(), inboxSpec(), nil, false)
	if err != nil || !plan.Changed || len(fake.statements) != before {
		t.Fatalf("a plan wrote: %v %+v %d", err, plan, len(fake.statements)-before)
	}
}

// A declaration naming no database is refused before any call, and a refused
// write is the action's error, so HQ keeps the change waiting.
func TestContactInboxReconcileRefusals(t *testing.T) {
	routes := contactRoutes(&d1Fake{})
	routes["POST /accounts/acct/d1/database/db-contacts/query"] = refusing(http.StatusForbidden, 10000, "Authentication error")
	r, fake := newCloudflare(t, routes)

	spec := inboxSpec()
	delete(spec, "database_id")
	if _, err := r.runAction(runtime.ResourceKindCloudflareContactInbox, "reconcile", t.Context(), spec, nil, true); err == nil || len(fake.requests) != 0 {
		t.Fatalf("a declaration naming no database reached Cloudflare: %v", err)
	}
	if _, err := r.runAction(runtime.ResourceKindCloudflareContactInbox, "reconcile", t.Context(), inboxSpec(), nil, true); err == nil {
		t.Fatal("a refused write reported success")
	}
}
