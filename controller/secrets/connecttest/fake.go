// Package connecttest is an in-process 1Password Connect for tests: the
// endpoints the renderer reads, on IPv4 loopback, holding only made-up values.
package connecttest

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
)

// VaultID and Token are what a default Fake answers to.
const (
	VaultID   = "vvvvvvvvvvvvvvvvvvvvvvvvvv"
	VaultName = "Example Vault"
	Token     = "sentinel.connect.token-0123456789"
)

// Field is one item field as Connect serves it.
type Field struct {
	ID    string  `json:"id"`
	Type  string  `json:"type"`
	Label string  `json:"label,omitempty"`
	Value *string `json:"value,omitempty"`
}

// URL is one item URL.
type URL struct {
	Href    string `json:"href"`
	Primary bool   `json:"primary,omitempty"`
}

// Item is one vault item.
type Item struct {
	ID      string
	Title   string
	State   string
	Version int
	Fields  []Field
	URLs    []URL
}

// F is a field selected by label, with an opaque id as custom fields have.
func F(label, value string) Field {
	return Field{ID: "id-" + strings.ReplaceAll(label, " ", "-"), Type: "STRING", Label: label, Value: &value}
}

// B is a built-in field, selected by its stable id.
func B(id, value string) Field {
	return Field{ID: id, Type: "CONCEALED", Label: id, Value: &value}
}

// ID is the n-th well-formed item identifier.
func ID(n int) string { return fmt.Sprintf("item%022d", n) }

// Fake is the server. Change its fields under Lock.
type Fake struct {
	sync.Mutex
	Server *httptest.Server
	// ContentVersion is the vault's; Edit bumps it.
	ContentVersion   int
	AttributeVersion int
	Items            []Item
	// ExtraVaults are listed beside the default one.
	ExtraVaults []map[string]any
	// Unavailable answers that many authenticated requests with 503 first.
	Unavailable int
	// Intercept, when it returns true, has answered the request itself.
	Intercept func(w http.ResponseWriter, r *http.Request) bool
	// SyncStatus is the sync dependency /health reports.
	SyncStatus string
	// Requests is every path asked, in order; Authorizations every header sent.
	Requests       []string
	Authorizations []string
}

// New starts a Fake holding items and closes it with the test.
func New(t testing.TB, items ...Item) *Fake {
	t.Helper()
	fake := &Fake{ContentVersion: 1, AttributeVersion: 1, Items: items, SyncStatus: "ACTIVE"}
	fake.Server = httptest.NewServer(http.HandlerFunc(fake.serve))
	t.Cleanup(fake.Server.Close)
	return fake
}

// Edit changes the vault and bumps its content version, as an edit does.
func (f *Fake) Edit(change func(*Fake)) {
	f.Lock()
	defer f.Unlock()
	change(f)
	f.ContentVersion++
}

// Count is how many requests were made to a path with this prefix.
func (f *Fake) Count(prefix string) int {
	f.Lock()
	defer f.Unlock()
	count := 0
	for _, path := range f.Requests {
		if strings.HasPrefix(path, prefix) {
			count++
		}
	}
	return count
}

func (f *Fake) vault() map[string]any {
	return map[string]any{"id": VaultID, "name": VaultName, "contentVersion": f.ContentVersion,
		"attributeVersion": f.AttributeVersion, "items": len(f.Items), "createdAt": "2026-01-01T00:00:00Z"}
}

func summary(item Item) map[string]any {
	out := map[string]any{"id": item.ID, "title": item.Title, "version": item.Version,
		"category": "LOGIN", "vault": map[string]any{"id": VaultID}}
	if item.State != "" {
		out["state"] = item.State
	}
	return out
}

func full(item Item) map[string]any {
	out := summary(item)
	out["fields"] = item.Fields
	if item.URLs != nil {
		out["urls"] = item.URLs
	}
	return out
}

// Full is the item as the client decodes it from the wire.
func (i Item) Full() connectapi.FullItem {
	raw, err := json.Marshal(full(i))
	if err != nil {
		panic(err)
	}
	var decoded connectapi.FullItem
	if err := json.Unmarshal(raw, &decoded); err != nil {
		panic(err)
	}
	return decoded
}

func write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(value)
}

func (f *Fake) serve(w http.ResponseWriter, r *http.Request) {
	f.Lock()
	defer f.Unlock()
	f.Requests = append(f.Requests, r.URL.Path)
	f.Authorizations = append(f.Authorizations, r.Header.Get("Authorization"))
	if f.Intercept != nil && f.Intercept(w, r) {
		return
	}
	if r.Method != http.MethodGet {
		write(w, http.StatusMethodNotAllowed, map[string]any{"status": 405})
		return
	}
	if r.URL.Path == "/health" {
		write(w, http.StatusOK, map[string]any{"name": "1Password Connect API", "version": "1.8.1",
			"dependencies": []map[string]any{{"service": "sync", "status": f.SyncStatus},
				{"service": "sqlite", "status": "ACTIVE", "message": "Connected to ./example.sqlite"}}})
		return
	}
	if r.Header.Get("Authorization") != "Bearer "+Token {
		write(w, http.StatusUnauthorized, map[string]any{"status": 401, "message": "Invalid token signature"})
		return
	}
	if f.Unavailable > 0 {
		f.Unavailable--
		write(w, http.StatusServiceUnavailable, map[string]any{"status": 503})
		return
	}
	items := append([]Item{}, f.Items...)
	sort.Slice(items, func(i, j int) bool { return items[i].ID < items[j].ID })
	switch path := r.URL.Path; {
	case path == "/v1/vaults":
		write(w, http.StatusOK, append([]map[string]any{f.vault()}, f.ExtraVaults...))
	case path == "/v1/vaults/"+VaultID:
		write(w, http.StatusOK, f.vault())
	case path == "/v1/vaults/"+VaultID+"/items":
		listing := []map[string]any{}
		for _, item := range items {
			listing = append(listing, summary(item))
		}
		write(w, http.StatusOK, listing)
	case strings.HasPrefix(path, "/v1/vaults/"+VaultID+"/items/"):
		id := strings.TrimPrefix(path, "/v1/vaults/"+VaultID+"/items/")
		for _, item := range items {
			if item.ID == id {
				write(w, http.StatusOK, full(item))
				return
			}
		}
		write(w, http.StatusNotFound, map[string]any{"status": 404})
	default:
		write(w, http.StatusNotFound, map[string]any{"status": 404})
	}
}
