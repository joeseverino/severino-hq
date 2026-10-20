package providers

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"slices"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// tailnetLockStatus is `tailscale lock status --json`.
type tailnetLockStatus struct {
	Enabled       bool              `json:"Enabled"`
	NodeKeySigned bool              `json:"NodeKeySigned"`
	TrustedKeys   []json.RawMessage `json:"TrustedKeys"`
	FilteredPeers []struct {
		Name     string `json:"Name"`
		StableID string `json:"StableID"`
	} `json:"FilteredPeers"`
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
	return []any{runtime.HostTailnetLockRecord{
		Record: "lock", Enabled: status.Enabled, NodeKeySigned: status.NodeKeySigned,
		TrustedKeys: len(status.TrustedKeys), LockedOut: lockedOut,
	}}, nil
}
