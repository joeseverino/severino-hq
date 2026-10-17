package main

import "testing"

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
