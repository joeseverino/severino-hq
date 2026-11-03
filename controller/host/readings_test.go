package host

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

// An instance name with no unit type starts no template, and reading it
// leaves the rest of the report intact.
func TestReportedPassesOverAnInstanceWithNoUnitType(t *testing.T) {
	dir := t.TempDir()
	files := map[string]string{
		"job@.service": "",
		"a.timer":      "[Timer]\nUnit=job@two\n",
		"b.timer":      "[Timer]\nUnit=job@one.service\n",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	shipped, _ := Shipped(dir)

	reported, err := Reported(dir, shipped)

	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"a.timer", "b.timer", "job@one.service"}; !slices.Equal(reported, want) {
		t.Errorf("reported %v", reported)
	}
}
