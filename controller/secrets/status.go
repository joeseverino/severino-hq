package secrets

import (
	jsonv2 "encoding/json/v2"
	"regexp"
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets/connect"
)

// StatusName is the status document's name in the runtime directory.
const StatusName = "status.json"

// StatusSchemaVersion is the only status document version.
const StatusSchemaVersion = 1

// Status is what the renderer says about itself after every run that held the
// lock. It holds no secret, no vault or item name, and no connection ref:
// times, versions and counts, so a reader can tell fresh secrets from a
// renderer that has been failing while cached files kept everything running.
type Status struct {
	SchemaVersion int      `json:"schema_version"`
	LastAttempt   Attempt  `json:"last_attempt"`
	LastSuccess   *Success `json:"last_success,omitzero"`
	Connect       *Connect `json:"connect,omitzero"`
}

// Attempt is the most recent run: "rendered", "current" or "failed", and for a
// failure its class (see Class).
type Attempt struct {
	At      time.Time `json:"at"`
	Outcome string    `json:"outcome"`
	Failure string    `json:"failure,omitzero"`
}

// Success is the most recent run that ended with current files installed.
type Success struct {
	// At is when the installed files were last confirmed current; RenderedAt
	// when they were last read in full from the vault.
	At         time.Time `json:"at"`
	RenderedAt time.Time `json:"rendered_at"`
	// The vault's versions at that read; absent if Connect did not report them.
	ContentVersion   *int   `json:"content_version,omitzero"`
	AttributeVersion *int   `json:"attribute_version,omitzero"`
	Counts           Counts `json:"counts"`
}

// Counts is how much one full render read and installed.
type Counts struct {
	ItemsRead    int `json:"items_read"`
	Connections  int `json:"connections"`
	AppVariables int `json:"app_variables"`
	Identities   int `json:"identities"`
	SigningKeys  int `json:"signing_keys"`
}

// Connect is the server's own account from /health at the last attempt that
// reached it. Sync state is the part to watch: a cached read succeeds while
// synchronization with 1Password is stalled.
type Connect struct {
	ReadAt       time.Time    `json:"read_at"`
	Version      string       `json:"version"`
	Dependencies []Dependency `json:"dependencies"`
}

// Dependency is one of Connect's dependencies and its state, such as sync
// ACTIVE or TOKEN_NEEDED. Connect's free-text message is not kept.
type Dependency struct {
	Service string `json:"service"`
	Status  string `json:"status"`
}

var statusWord = regexp.MustCompile(`^[A-Za-z0-9_.+-]{1,64}$`)

func word(value *string) string {
	if value == nil || !statusWord.MatchString(*value) {
		return "unreadable"
	}
	return *value
}

// connectStatus keeps only short words of what the server said: the document
// is read by HQ, and what an unauthenticated endpoint returned is not trusted
// to be harmless text.
func connectStatus(health connect.Health, at time.Time) *Connect {
	found := &Connect{ReadAt: at, Version: word(&health.Version), Dependencies: []Dependency{}}
	for index, dependency := range health.Dependencies {
		if index == 16 {
			break
		}
		found.Dependencies = append(found.Dependencies, Dependency{Service: word(dependency.Service), Status: word(dependency.Status)})
	}
	return found
}

// DecodeStatus reads a status document strictly.
func DecodeStatus(data []byte) (Status, error) {
	var status Status
	if err := jsonv2.Unmarshal(data, &status, jsonv2.RejectUnknownMembers(true)); err != nil {
		return Status{}, err
	}
	if status.SchemaVersion != StatusSchemaVersion {
		return Status{}, errConfig("status document schema version is not 1")
	}
	return status, nil
}
