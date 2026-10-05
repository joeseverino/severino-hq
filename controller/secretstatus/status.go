// Package secretstatus declares the document a secret renderer writes about
// its own runs: the renderer writes it, the controller reads it for HQ, and
// both use this one type. It imports nothing of either, so the controller
// links no Connect client and the renderer no provider code.
package secretstatus

import (
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"regexp"
	"time"
)

// Name is the status document's name in the renderer's runtime directory.
const Name = "status.json"

// SchemaVersion is the only status document version.
const SchemaVersion = 1

// MaxBytes bounds a document on read. One holds at most MaxDependencies
// short words beside its times and counts.
const MaxBytes = 16 << 10

// MaxDependencies is how many of Connect's dependencies a document names.
const MaxDependencies = 16

// What a run did.
const (
	OutcomeRendered = "rendered"
	OutcomeCurrent  = "current"
	OutcomeFailed   = "failed"
)

// Unreadable stands in for a word the document may not carry.
const Unreadable = "unreadable"

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
// failure the one word it is filed under.
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

// WordPattern is every string a document carries besides its times: a short
// word, never a sentence.
const WordPattern = `^[A-Za-z0-9_.+-]{1,64}$`

var wordPattern = regexp.MustCompile(WordPattern)

// Word is value when it is a short word, and Unreadable when it is not.
func Word(value *string) string {
	if value == nil || !wordPattern.MatchString(*value) {
		return Unreadable
	}
	return *value
}

// ErrInvalid is every reason a document is refused; match with errors.Is.
var ErrInvalid = errors.New("invalid secret render status document")

func invalid(reason string) error { return fmt.Errorf("%w: %s", ErrInvalid, reason) }

// Decode reads a status document strictly: an unknown or repeated member, a
// second value, another schema version, an outcome that is not one of the
// three, or a string that is not a short word is an error.
func Decode(data []byte) (Status, error) {
	if len(data) > MaxBytes {
		return Status{}, invalid("the document is larger than a status document")
	}
	var status Status
	if err := jsonv2.Unmarshal(data, &status, jsonv2.RejectUnknownMembers(true)); err != nil {
		return Status{}, fmt.Errorf("%w: %w", ErrInvalid, err)
	}
	if status.SchemaVersion != SchemaVersion {
		return Status{}, invalid("schema version is not 1")
	}
	if err := status.check(); err != nil {
		return Status{}, err
	}
	return status, nil
}

func (s Status) check() error {
	attempt := s.LastAttempt
	switch attempt.Outcome {
	case OutcomeRendered, OutcomeCurrent:
		if attempt.Failure != "" {
			return invalid("a run that did not fail names a failure")
		}
	case OutcomeFailed:
		if !wordPattern.MatchString(attempt.Failure) {
			return invalid("a failed run names its failure in one word")
		}
	default:
		return invalid("the outcome is not rendered, current or failed")
	}
	if s.Connect == nil {
		return nil
	}
	if !wordPattern.MatchString(s.Connect.Version) || len(s.Connect.Dependencies) > MaxDependencies {
		return invalid("the Connect account is not short words")
	}
	for _, dependency := range s.Connect.Dependencies {
		if !wordPattern.MatchString(dependency.Service) || !wordPattern.MatchString(dependency.Status) {
			return invalid("a Connect dependency is not short words")
		}
	}
	return nil
}
