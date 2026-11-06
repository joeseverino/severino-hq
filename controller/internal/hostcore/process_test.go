package hostcore

import (
	"context"
	"os/exec"
	"slices"
	"strings"
	"testing"
)

func TestAProgramGetsOnlyTheEnvironmentItIsHanded(t *testing.T) {
	t.Setenv("HQ_EXAMPLE_SECRET", "inherited")
	env, err := exec.LookPath("env")
	if err != nil {
		t.Skip("no env program")
	}
	for name, given := range map[string][]string{"nil": nil, "empty": {}, "one": {"LC_ALL=C"}} {
		t.Run(name, func(t *testing.T) {
			out, err := Process(context.Background(), given, env).Output()
			if err != nil {
				t.Fatal(err)
			}
			got := strings.Fields(string(out))
			if !slices.Equal(got, given) {
				t.Errorf("the program saw %v, was handed %v", got, given)
			}
		})
	}
}

func TestAProcessEnvironmentIsItsOwnCopy(t *testing.T) {
	shared := []string{"LC_ALL=C"}
	first := Process(context.Background(), shared, "true")
	first.Env[0] = "LC_ALL=example"
	if shared[0] != "LC_ALL=C" || Process(context.Background(), shared, "true").Env[0] != "LC_ALL=C" {
		t.Error("one command's environment changed another's")
	}
}
