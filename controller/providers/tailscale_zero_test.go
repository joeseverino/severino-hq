package providers

import "testing"

func TestAnUnsetDaemonTimeIsNoMoment(t *testing.T) {
	zero := "0001-01-01T00:00:00Z"
	record, ok := tailnetRecord(tailnetNode{HostName: "an-edge", Online: true, LastSeen: zero, KeyExpiry: zero, LastHandshake: zero})
	if !ok || record.LastSeen != "" || record.KeyExpires != "" || record.LastHandshake != "" {
		t.Errorf("zero times leaked: %+v", record)
	}
	record, _ = tailnetRecord(tailnetNode{HostName: "an-edge", LastSeen: "2026-01-02T12:00:00+01:00"})
	if record.LastSeen != "2026-01-02T11:00:00Z" {
		t.Errorf("LastSeen = %q", record.LastSeen)
	}
}
