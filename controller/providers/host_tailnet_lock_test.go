package providers

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// lockStatus is `tailscale lock status --json` as the daemon answers it, with
// invented keys and names.
const lockStatus = `{
  "Enabled": true,
  "Head": "example-head",
  "PublicKey": "example-public-key",
  "NodeKey": "example-node-key",
  "NodeKeySigned": true,
  "TrustedKeys": [
    {"Key": "example-trusted-key-one", "Metadata": null, "Votes": 1},
    {"Key": "example-trusted-key-two", "Metadata": null, "Votes": 1}
  ],
  "VisiblePeers": [],
  "FilteredPeers": [
    {"Name": "phone.example.ts.net.", "ID": 2, "StableID": "nexample2"},
    {"Name": "", "ID": 1, "StableID": "nexample1"}
  ],
  "StateID": 1
}`

func writeLockStatus(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tailnet-lock.json")
	if err := os.WriteFile(path, []byte(body), 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

func lockInventory(t *testing.T, path string) runtime.KindReport {
	t.Helper()
	kind := runtime.ResourceKindHostTailnetLock
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	controller := &Controller{Registry: New(runtime.Environment{TailnetLock: path}, supplied(), &fakeHTTP{}), Declared: declared}
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)]
}

func TestTheLockIsReadFromTheDaemonsStatus(t *testing.T) {
	report := lockInventory(t, writeLockStatus(t, lockStatus))
	if !report.OK || len(report.Records) != 1 {
		t.Fatalf("%+v", report)
	}
	lock, ok := report.Records[0].(runtime.HostTailnetLockRecord)
	if !ok {
		t.Fatalf("record is %T", report.Records[0])
	}
	if !lock.Enabled || !lock.NodeKeySigned || lock.TrustedKeys != 2 {
		t.Errorf("read %+v", lock)
	}
	if !slices.Equal(lock.LockedOut, []string{"nexample1", "phone.example.ts.net."}) {
		t.Errorf("locked out %v", lock.LockedOut)
	}
}

// A run the launcher named the reading for, but could not take it, fails the
// read and says why: HQ keeps the last good reading beside the reason. A run
// with nothing named has no source for the kind at all. Neither produces an
// empty lock.
func TestALockNotTakenIsNeverAnEmptyReading(t *testing.T) {
	missing := lockInventory(t, filepath.Join(t.TempDir(), "tailnet-lock.json"))
	if missing.OK || len(missing.Records) != 0 || missing.Error == "" {
		t.Errorf("a lock status the launcher could not take: %+v", missing)
	}
	unreadable := lockInventory(t, writeLockStatus(t, "not json"))
	if unreadable.OK || len(unreadable.Records) != 0 || unreadable.Error == "" {
		t.Errorf("a lock status that is not status: %+v", unreadable)
	}
	unnamed := lockInventory(t, "")
	if unnamed.Connected == nil || *unnamed.Connected || len(unnamed.Records) != 0 {
		t.Errorf("a run handed no lock status: %+v", unnamed)
	}
}
