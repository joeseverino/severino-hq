package providers

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// tailnetLockStatus is the daemon's answer at /localapi/v0/tka/status, the
// one `tailscale lock status` prints: ipnstate.TailnetLockStatus, each trusted
// key an ipnstate.TKAKey (tailscale.com/ipn/ipnstate/ipnstate.go at v1.104.7).
type tailnetLockStatus struct {
	Enabled       bool `json:"Enabled"`
	NodeKeySigned bool `json:"NodeKeySigned"`
	TrustedKeys   []struct {
		Kind     string            `json:"Kind"`
		Key      string            `json:"Key"`
		Metadata map[string]string `json:"Metadata"`
		Votes    int               `json:"Votes"`
	} `json:"TrustedKeys"`
	FilteredPeers []struct {
		Name     string `json:"Name"`
		StableID string `json:"StableID"`
	} `json:"FilteredPeers"`
}

// lockKey is a tailnet lock public key in the form `tailscale lock remove`
// takes, "tlpub:<hex>". The local API writes the same key as "nlpub:<hex>"
// (key.NLPublic's MarshalText, tailscale.com/types/key/nl.go), and both name
// one key, so every record carries the one form and a key joins by equality.
func lockKey(key string) string {
	key = strings.ToLower(strings.TrimSpace(key))
	if hex, ok := strings.CutPrefix(key, "nlpub:"); ok {
		return "tlpub:" + hex
	}
	return key
}

// hostTailnetLock is tailnet lock as this machine's daemon states it, from the
// copy the launcher mounts. The launcher names the file on every run, so a
// file it could not take is a failed read, which leaves the last one standing.
func (r *Registry) hostTailnetLock(context.Context) ([]any, error) {
	path := r.Env.TailnetLock
	if path == "" {
		return nil, &ProviderError{Message: "no tailnet lock reading was mounted"}
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, &ProviderError{Message: "the launcher could not ask the tailnet daemon for its lock status"}
	}
	if err != nil {
		return nil, &ProviderError{Message: "read the tailnet lock reading", Err: err}
	}
	var status tailnetLockStatus
	if err := json.Unmarshal(data, &status); err != nil {
		return nil, &ProviderError{Message: "the tailnet lock reading is not lock status", Err: err}
	}
	lockedOut := []string{}
	for _, peer := range status.FilteredPeers {
		name := peer.Name
		if name == "" {
			name = peer.StableID
		}
		lockedOut = append(lockedOut, name)
	}
	slices.Sort(lockedOut)
	signers := []runtime.HostTailnetLockTailnetLockSigner{}
	for _, trusted := range status.TrustedKeys {
		signers = append(signers, runtime.HostTailnetLockTailnetLockSigner{
			Key: lockKey(trusted.Key), Kind: trusted.Kind, Metadata: trusted.Metadata, Votes: trusted.Votes,
		})
	}
	return []any{runtime.HostTailnetLockRecord{
		Record: "lock", Enabled: status.Enabled, NodeKeySigned: status.NodeKeySigned,
		Signers: signers, LockedOut: lockedOut,
	}}, nil
}
