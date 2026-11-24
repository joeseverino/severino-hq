package providers

import (
	"fmt"
	"os"
	"testing"
)

func TestMain(m *testing.M) {
	// The package's tests make their temporary files in a directory of the
	// run's own, removed when it ends: op's state directory lives for the
	// process (opState), and nothing else would remove it.
	run, err := os.MkdirTemp("", "hq-providers-test-")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.Setenv("TMPDIR", run); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	code := m.Run()
	if err := os.RemoveAll(run); err != nil {
		fmt.Fprintln(os.Stderr, err)
		code = 1
	}
	os.Exit(code)
}
