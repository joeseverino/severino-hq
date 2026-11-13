package born

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBornIsWhenTheFileWasMade(t *testing.T) {
	path := filepath.Join(t.TempDir(), "flag")
	before := time.Now().Add(-time.Second)
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	at, err := At(path)
	if err != nil {
		t.Fatal(err)
	}
	if at.Before(before) || at.After(time.Now().Add(time.Second)) {
		t.Errorf("born %v, written after %v", at, before)
	}
}
