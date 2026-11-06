package main

import (
	"os/exec"
	"slices"
	"strings"
	"testing"
)

// rootPrograms are the binaries root runs on a host with the least code
// behind them, each with every module outside the standard library it may
// link. A module joins a list only when the program calls into it, since
// each one is attack surface running as root.
var rootPrograms = map[string][]string{
	"github.com/joeseverino/severino-hq/controller/cmd/hq-secrets": {
		"github.com/joeseverino/severino-hq/controller",
		// secrets/project derives the SSH public keys it installs.
		"golang.org/x/crypto",
	},
	"github.com/joeseverino/severino-hq/controller/cmd/hq-edge": {
		"github.com/joeseverino/severino-hq/controller",
	},
}

func TestARootProgramLinksOnlyTheModulesItUses(t *testing.T) {
	t.Parallel()
	for program, allowed := range rootPrograms {
		t.Run(program[strings.LastIndex(program, "/")+1:], func(t *testing.T) {
			t.Parallel()
			out, err := exec.CommandContext(t.Context(), "go", "list", "-deps", "-f", "{{with .Module}}{{.Path}}{{end}}", program).Output()
			if err != nil {
				t.Fatalf("go list %s: %v", program, err)
			}
			modules := strings.Fields(string(out))
			slices.Sort(modules)
			for _, module := range slices.Compact(modules) {
				if !slices.Contains(allowed, module) {
					t.Errorf("%s links %s, a module not on its list", program, module)
				}
			}
		})
	}
}
