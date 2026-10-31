// Package connecttest is an in-process 1Password Connect for tests: the
// endpoints the renderer reads, on IPv4 loopback, holding only made-up values.
package connecttest

import (
	"cmp"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
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

// State is what the server answers from. A test reaches it only through Edit
// or Set, so no write can race a request still being served.
type State struct {
	// ContentVersion is the vault's; Edit bumps it.
	ContentVersion   int
	AttributeVersion int
	Items            []Item
	// ExtraVaults are listed beside the default one.
	ExtraVaults []map[string]any
	// Unavailable answers that many authenticated requests with 503 first.
	Unavailable int
	// SyncStatus is the sync dependency /health reports.
	SyncStatus string
}

// An Interceptor that returns true has answered the request itself.
type Interceptor func(w http.ResponseWriter, r *http.Request) bool

// Fake is the server.
type Fake struct {
	Server *httptest.Server

	mu          sync.Mutex
	state       State
	interceptor Interceptor
	// requests is every path asked, in order; authorizations every header sent.
	requests       []string
	authorizations []string

	intercepting sync.Mutex
}

// New starts a Fake holding items and closes it with the test.
func New(t testing.TB, items ...Item) *Fake {
	t.Helper()
	fake := &Fake{state: State{ContentVersion: 1, AttributeVersion: 1, Items: items, SyncStatus: "ACTIVE"}}
	fake.Server = httptest.NewServer(http.HandlerFunc(fake.serve))
	t.Cleanup(fake.Server.Close)
	return fake
}

// Edit changes the vault and bumps its content version, as an edit does.
func (f *Fake) Edit(change func(*State)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	change(&f.state)
	f.state.ContentVersion++
}

// Set changes the server's state without bumping the content version.
func (f *Fake) Set(change func(*State)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	change(&f.state)
}

// Intercept sees every later request first; nil stops it. An interceptor runs
// outside the state's lock, so it may Edit or Set, and one at a time, so its
// own variables need no lock of their own.
func (f *Fake) Intercept(interceptor Interceptor) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.interceptor = interceptor
}

// Requests is every path asked so far, in order.
func (f *Fake) Requests() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.requests)
}

// Authorizations is every Authorization header sent so far, in order.
func (f *Fake) Authorizations() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.authorizations)
}

// Count is how many requests were made to a path with this prefix.
func (f *Fake) Count(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	count := 0
	for _, path := range f.requests {
		if strings.HasPrefix(path, prefix) {
			count++
		}
	}
	return count
}

func (s *State) vault() map[string]any {
	return map[string]any{"id": VaultID, "name": VaultName, "contentVersion": s.ContentVersion,
		"attributeVersion": s.AttributeVersion, "items": len(s.Items), "createdAt": "2026-01-01T00:00:00Z"}
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
	_ = json.NewEncoder(w).Encode(value)
}

func (f *Fake) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.URL.Path)
	f.authorizations = append(f.authorizations, r.Header.Get("Authorization"))
	interceptor := f.interceptor
	f.mu.Unlock()
	if interceptor != nil && f.intercepted(interceptor, w, r) {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.state.answer(w, r)
}

func (f *Fake) intercepted(interceptor Interceptor, w http.ResponseWriter, r *http.Request) bool {
	f.intercepting.Lock()
	defer f.intercepting.Unlock()
	return interceptor(w, r)
}

func (s *State) answer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		write(w, http.StatusMethodNotAllowed, map[string]any{"status": 405})
		return
	}
	if r.URL.Path == "/health" {
		write(w, http.StatusOK, map[string]any{"name": "1Password Connect API", "version": "1.8.1",
			"dependencies": []map[string]any{{"service": "sync", "status": s.SyncStatus},
				{"service": "sqlite", "status": "ACTIVE", "message": "Connected to ./example.sqlite"}}})
		return
	}
	if r.Header.Get("Authorization") != "Bearer "+Token {
		write(w, http.StatusUnauthorized, map[string]any{"status": 401, "message": "Invalid token signature"})
		return
	}
	if s.Unavailable > 0 {
		s.Unavailable--
		write(w, http.StatusServiceUnavailable, map[string]any{"status": 503})
		return
	}
	items := append([]Item{}, s.Items...)
	slices.SortFunc(items, func(a, b Item) int { return cmp.Compare(a.ID, b.ID) })
	switch path := r.URL.Path; {
	case path == "/v1/vaults":
		write(w, http.StatusOK, append([]map[string]any{s.vault()}, s.ExtraVaults...))
	case path == "/v1/vaults/"+VaultID:
		write(w, http.StatusOK, s.vault())
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
