package providers

import (
	"context"
	"fmt"
	"strconv"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A site's contact form writes each message to a D1 table. The cloudflare_api
// connection reads the table (cloudflare.contact_submission) and applies the
// changes HQ declares to it (cloudflare.contact_inbox), so HQ itself holds no
// credential for the database.

const (
	contactTable = "contact_submissions"
	// The newest messages read; HQ lists and searches these.
	contactReadLimit = 500
)

type CloudflareContactReview struct {
	ID         int    `json:"id"`
	Status     string `json:"status"`
	AssignedTo string `json:"assigned_to"`
	AdminNotes string `json:"admin_notes"`
}

type CloudflareContactInboxSpec struct {
	ConnectionRef string                    `json:"connection_ref"`
	AccountID     string                    `json:"account_id"`
	DatabaseID    string                    `json:"database_id"`
	Reviews       []CloudflareContactReview `json:"reviews"`
	Deletions     []int                     `json:"deletions"`
}

// CloudflareContactInboxStatus is what the last apply did: how many changes it
// was given and how many rows they changed.
type CloudflareContactInboxStatus struct {
	Given   int `json:"given"`
	Changed int `json:"changed"`
}

type d1Statement struct {
	SQL    string `json:"sql"`
	Params []any  `json:"params"`
}

type d1QueryResult struct {
	Results []map[string]any `json:"results"`
	Success bool             `json:"success"`
	Meta    struct {
		Changes int `json:"changes"`
	} `json:"meta"`
}

// d1Query runs one statement against one database and returns its result.
func (r *Registry) d1Query(ctx context.Context, ref, account, database, sql string, params ...any) (d1QueryResult, error) {
	if params == nil {
		params = []any{}
	}
	path := "/accounts/" + account + "/d1/database/" + database + "/query"
	envelope, err := r.cloudflareEnvelope(ctx, runtime.ConnectionProviderCloudflareAPI, ref, path, "POST", d1Statement{SQL: sql, Params: params})
	if err != nil {
		return d1QueryResult{}, err
	}
	results, err := cloudflareDecode[[]d1QueryResult](envelope.Result, "D1 query result")
	if err != nil {
		return d1QueryResult{}, err
	}
	if len(results) == 0 {
		return d1QueryResult{}, nil
	}
	if !results[0].Success {
		return d1QueryResult{}, &ProviderError{Message: "D1 did not run the statement"}
	}
	return results[0], nil
}

// cloudflareContactSubmissions reads the newest messages of every database
// that holds the contact table. A database without it is not an inbox.
func (r *Registry) cloudflareContactSubmissions(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		databases, err := cloudflareAccountItems[cfapi.D1DatabaseResponse](ctx, r, ref, "/d1/database", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		for _, database := range databases {
			rows, err := r.contactRows(ctx, ref, account, database.UUID)
			if err != nil {
				return nil, err
			}
			for _, row := range rows {
				found = append(found, contactRecord(ref, account, database, row))
			}
		}
	}
	return found, nil
}

func (r *Registry) contactRows(ctx context.Context, ref, account, database string) ([]map[string]any, error) {
	tables, err := r.d1Query(ctx, ref, account, database, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", contactTable)
	if err != nil || len(tables.Results) == 0 {
		return nil, err
	}
	// SELECT * rather than a column list: a form that adds or lacks a column
	// still reads, and the record keeps only the fields it names.
	read, err := r.d1Query(ctx, ref, account, database, "SELECT * FROM "+contactTable+" ORDER BY id DESC LIMIT ?", contactReadLimit)
	return read.Results, err
}

func contactRecord(ref, account string, database cfapi.D1DatabaseResponse, row map[string]any) runtime.CloudflareContactSubmissionRecord {
	id, _ := strconv.Atoi(d1Text(row["id"]))
	return runtime.CloudflareContactSubmissionRecord{
		ConnectionRef: ref, AccountID: account, DatabaseID: database.UUID, Database: database.Name, ID: id,
		CreatedAt: d1Text(row["created_at"]), UpdatedAt: d1Text(row["updated_at"]),
		Name: d1Text(row["name"]), Email: d1Text(row["email"]), Message: d1Text(row["message"]),
		Status: d1Text(row["status"]), AssignedTo: d1Text(row["assigned_to"]), AdminNotes: d1Text(row["admin_notes"]),
		Country: d1Text(row["country"]), IPAddress: d1Text(row["ip_address"]), Browser: d1Text(row["browser"]),
		Device: d1Text(row["device"]), UserAgent: d1Text(row["user_agent"]), SourceURL: d1Text(row["source_url"]),
		Turnstile: d1Text(row["turnstile"]),
	}
}

// d1Text is a column's value as text: D1 answers numbers as JSON numbers and
// an empty column as null.
func d1Text(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(typed)
	}
	return fmt.Sprint(value)
}

// contactInboxReconcile applies each declared change. Each statement only
// touches a row that differs from what is declared, so applying the same
// declaration again changes nothing and leaves updated_at alone.
func (r *Registry) contactInboxReconcile(ctx context.Context, spec CloudflareContactInboxSpec, _ struct{}, apply bool) (Result, error) {
	given := len(spec.Reviews) + len(spec.Deletions)
	if !apply {
		return result(given > 0, CloudflareContactInboxStatus{Given: given}, "Planned", fmt.Sprintf("%d changes to apply.", given), ""), nil
	}
	if spec.ConnectionRef == "" || spec.AccountID == "" || spec.DatabaseID == "" {
		return Result{}, &ProviderError{Message: "the contact inbox names no database"}
	}
	changed := 0
	for _, review := range spec.Reviews {
		done, err := r.d1Query(ctx, spec.ConnectionRef, spec.AccountID, spec.DatabaseID,
			"UPDATE "+contactTable+" SET status = ?, assigned_to = ?, admin_notes = ?, updated_at = datetime('now') "+
				"WHERE id = ? AND (status IS NOT ? OR assigned_to IS NOT ? OR admin_notes IS NOT ?)",
			review.Status, review.AssignedTo, review.AdminNotes, review.ID, review.Status, review.AssignedTo, review.AdminNotes)
		if err != nil {
			return Result{}, err
		}
		changed += done.Meta.Changes
	}
	for _, id := range spec.Deletions {
		done, err := r.d1Query(ctx, spec.ConnectionRef, spec.AccountID, spec.DatabaseID, "DELETE FROM "+contactTable+" WHERE id = ?", id)
		if err != nil {
			return Result{}, err
		}
		changed += done.Meta.Changes
	}
	status := CloudflareContactInboxStatus{Given: given, Changed: changed}
	detail := fmt.Sprintf("Changed %d messages; %d were already as declared.", changed, given-changed)
	return result(changed > 0, status, "Applied", detail, "Contact messages updated."), nil
}
