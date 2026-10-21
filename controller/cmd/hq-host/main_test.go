package main

import (
	"context"
	"io"
	"strings"
	"testing"
)

func TestARunStagesWhereItsUnitMayWrite(t *testing.T) {
	for _, c := range []struct{ given, want string }{
		{"", "/run"},
		{"/run/severino-hq-verify", "/run/severino-hq-verify"},
		{"/run/first:/run/second", "/run/first"},
	} {
		t.Setenv("RUNTIME_DIRECTORY", c.given)
		if got := runtimeDir(); got != c.want {
			t.Errorf("RUNTIME_DIRECTORY=%q: staged in %q, want %q", c.given, got, c.want)
		}
	}
}

// The deploy rule admits any arguments after `deploy`, so the count is held
// here, before anything runs as root.
func TestADeployTakesExactlyOneImage(t *testing.T) {
	for _, args := range [][]string{{"deploy"}, {"deploy", "a", "--stage", "/tmp"}} {
		var stderr strings.Builder
		if code := run(context.Background(), args, func(string) string { return "" }, 0, strings.NewReader(""), io.Discard, &stderr); code != 2 {
			t.Errorf("%q exited %d, want 2", args, code)
		}
	}
}
