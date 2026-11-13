package host

import (
	"bytes"
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

const exampleToken = "ghs_exampletokenvalue"

// dependencyHandoff is the file a pass leaves when a dependency run is due.
var dependencyHandoff = DependencyUpdates.HandoffName()

// relocated is grant with its path under root, as a test's host has it.
func relocated(grant Grant, root string) Grant {
	grant.Path = filepath.Join(root, grant.Path)
	return grant
}

// spool is where the launch's dependency run grant spools its hand-off.
func (l *launch) spool() string {
	grant, _ := l.controller.grantFor(dependencyHandoff)
	return grant.Path
}

func handoffDocument(t testing.TB, edit func(run map[string]any, top map[string]any)) []byte {
	t.Helper()
	run := map[string]any{
		"owner":        "example",
		"repositories": []string{"example/app", "example/site"},
		"token":        exampleToken,
		"expires_at":   time.Now().Add(55 * time.Minute).UTC().Format(time.RFC3339),
	}
	top := map[string]any{"version": 1, "runs": []any{run}}
	if edit != nil {
		edit(run, top)
	}
	data, err := json.Marshal(top)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestTheOutDirectoryIsTheControllersAndMountedWritable(t *testing.T) {
	l := newLaunch(t)
	var owned []string
	l.controller.Owner = func(root *os.Root, name string) error {
		if name == "out" {
			owned = append(owned, root.Name())
		}
		return nil
	}
	var mode os.FileMode
	l.container = func(out string) {
		info, _ := os.Lstat(out)
		mode = info.Mode()
	}

	l.run(t, Apply)

	var mount string
	for i, arg := range l.args {
		if arg == "--mount" && strings.Contains(l.args[i+1], "target="+controllerOut) {
			mount = l.args[i+1]
		}
	}
	source, target, ok := bind(mount)
	if !ok || target != controllerOut || strings.Contains(mount, "readonly") || filepath.Base(source) != "out" {
		t.Errorf("the out directory is mounted as %q", mount)
	}
	if !mode.IsDir() || mode.Perm() != 0o700 {
		t.Errorf("the out directory is %v, not a directory only its owner enters", mode)
	}
	if len(owned) != 1 || owned[0] != filepath.Dir(source) {
		t.Errorf("the out directory was given to the controller from %v", owned)
	}
	if !slices.Contains(l.envs(), "HQ_CONTROLLER_OUT="+controllerOut) {
		t.Error("the controller is not told where its out directory is")
	}
}

func TestAValidHandoffIsSpooledForRootAlone(t *testing.T) {
	l := newLaunch(t)
	var original string
	l.container = func(out string) {
		original = filepath.Join(out, dependencyHandoff)
		if err := os.WriteFile(original, handoffDocument(t, nil), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	l.run(t, Apply)

	data, err := os.ReadFile(filepath.Join(l.spool(), SpoolName))
	if err != nil {
		t.Fatal("the handoff was not spooled")
	}
	handoff, err := ParseHandoff(data)
	if err != nil || len(handoff.Runs) != 1 || handoff.Runs[0].Token != exampleToken || !slices.Equal(handoff.Runs[0].Repositories, []string{"example/app", "example/site"}) {
		t.Errorf("spooled %s (%v)", data, err)
	}
	for path, want := range map[string]os.FileMode{l.spool(): 0o700, filepath.Join(l.spool(), SpoolName): 0o600} {
		if info, _ := os.Lstat(path); info.Mode().Perm() != want {
			t.Errorf("%s is %v", path, info.Mode())
		}
	}
	if _, err := os.Lstat(original); err == nil {
		t.Error("the original was left")
	}
}

func TestAnInvalidHandoffIsDroppedAndNeverSpooled(t *testing.T) {
	for name, write := range map[string]func(t *testing.T, out string){
		"a link to a file it may not read": func(t *testing.T, out string) {
			target := filepath.Join(filepath.Dir(out), "connections.json")
			if err := os.Symlink(target, filepath.Join(out, dependencyHandoff)); err != nil {
				t.Fatal(err)
			}
		},
		"a second name of another file": func(t *testing.T, out string) {
			other := filepath.Join(out, "other.json")
			if err := os.WriteFile(other, handoffDocument(t, nil), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Link(other, filepath.Join(out, dependencyHandoff)); err != nil {
				t.Fatal(err)
			}
		},
		"a directory": func(t *testing.T, out string) {
			if err := os.MkdirAll(filepath.Join(out, dependencyHandoff, "inside"), 0o700); err != nil {
				t.Fatal(err)
			}
		},
		"a repository of another owner": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) { run["repositories"] = []string{"example/app", "other/app"} })
		},
		"an owner GitHub would refuse": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) { run["owner"] = "-example"; run["repositories"] = []string{"-example/app"} })
		},
		"a key the contract does not name": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) { run["permissions"] = "admin" })
		},
		"a key in another casing": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) { run["Token"] = run["token"]; delete(run, "token") })
		},
		"a token that ends a line": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) { run["token"] = exampleToken + "\nRENOVATE_X=1" })
		},
		"an expired token": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) {
				run["expires_at"] = time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
			})
		},
		"a token that outlives an hour": func(t *testing.T, out string) {
			writeHandoff(t, out, func(run, _ map[string]any) {
				run["expires_at"] = time.Now().Add(3 * time.Hour).UTC().Format(time.RFC3339)
			})
		},
		"another version": func(t *testing.T, out string) {
			writeHandoff(t, out, func(_, top map[string]any) { top["version"] = 2 })
		},
		"more than a handoff can be": func(t *testing.T, out string) {
			data := handoffDocument(t, nil)
			padded := append(bytes.TrimSuffix(data, []byte("}")), []byte(`,"pad":"`+strings.Repeat("x", handoffLimit)+`"}`)...)
			if err := os.WriteFile(filepath.Join(out, dependencyHandoff), padded, 0o600); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLaunch(t)
			var stderr bytes.Buffer
			l.controller.Stderr = &stderr
			var out string
			l.container = func(dir string) { out = dir; write(t, dir) }

			l.run(t, Apply)

			if _, err := os.Lstat(filepath.Join(l.spool(), SpoolName)); err == nil {
				t.Error("an invalid handoff was spooled")
			}
			if _, err := os.Lstat(filepath.Join(out, dependencyHandoff)); err == nil {
				t.Error("the original was left")
			}
			if !strings.Contains(stderr.String(), "refused") || strings.Contains(stderr.String(), exampleToken) {
				t.Errorf("said %q", stderr.String())
			}
		})
	}
}

func writeHandoff(t *testing.T, out string, edit func(run, top map[string]any)) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(out, dependencyHandoff), handoffDocument(t, edit), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestAPassThatLeavesNoHandoffSpoolsNothing(t *testing.T) {
	l := newLaunch(t)
	var stderr bytes.Buffer
	l.controller.Stderr = &stderr

	l.run(t, Apply)

	if _, err := os.Lstat(l.spool()); err == nil || stderr.Len() != 0 {
		t.Errorf("spooled or said %q with nothing handed off", stderr.String())
	}
}

func TestTheLastOutcomeIsGivenToTheNextPass(t *testing.T) {
	l := newLaunch(t)
	if err := os.MkdirAll(filepath.Dir(l.controller.DependencyRun), 0o700); err != nil {
		t.Fatal(err)
	}
	outcome := []byte(`{"version":1,"runs":[]}`)
	if err := os.WriteFile(l.controller.DependencyRun, outcome, 0o600); err != nil {
		t.Fatal(err)
	}

	l.run(t, Apply)

	if !bytes.Equal(l.mounted[dependencyRunTarget], outcome) || !slices.Contains(l.envs(), "SEVERINO_DEPENDENCY_RUN="+dependencyRunTarget) {
		t.Errorf("the outcome was not given: %v", l.envs())
	}
	for _, arg := range l.args {
		if strings.Contains(arg, "source="+l.controller.DependencyRun) {
			t.Error("the outcome itself was mounted, not a copy")
		}
	}
}

// A handoff is untrusted input from the controller's out directory. What
// parses holds every rule of the contract, and survives the re-encoding
// root spools it in, unchanged; what is refused is one line of root's log
// that never repeats a token.
func FuzzParseHandoff(f *testing.F) {
	f.Add(handoffDocument(f, nil))
	for _, edit := range []func(run, top map[string]any){
		func(run, _ map[string]any) { run["repositories"] = []string{"example/app", "other/app"} },
		func(run, _ map[string]any) { run["owner"] = "-example"; run["repositories"] = []string{"-example/app"} },
		func(run, _ map[string]any) { run["permissions"] = "admin" },
		func(run, _ map[string]any) { run["Token"] = run["token"]; delete(run, "token") },
		func(run, _ map[string]any) { run["token"] = exampleToken + "\nRENOVATE_X=1" },
		func(run, _ map[string]any) { run["repositories"] = []string{"example/app", "example/app"} },
		func(run, _ map[string]any) { run["repositories"] = []string{"example/.."} },
		func(run, _ map[string]any) { run["expires_at"] = "2030-01-01T00:00:00+05:30" },
		func(_, top map[string]any) { top["version"] = 2 },
		func(run, top map[string]any) {
			twin := map[string]any{}
			maps.Copy(twin, run)
			twin["owner"], twin["repositories"] = "Example", []string{"Example/app"}
			top["runs"] = []any{run, twin}
		},
	} {
		f.Add(handoffDocument(f, edit))
	}
	f.Add([]byte(`{"version":1,"runs":[]}`))
	f.Add([]byte(`{"version":1,"runs":null}{}`))
	f.Fuzz(func(t *testing.T, data []byte) {
		handoff, err := ParseHandoff(data)
		if err != nil {
			if strings.ContainsAny(err.Error(), "\r\n") {
				t.Fatalf("the refusal is more than one line: %q", err)
			}
			var carried struct {
				Runs []struct {
					Token string `json:"token"`
				} `json:"runs"`
			}
			_ = json.Unmarshal(data, &carried)
			for _, run := range carried.Runs {
				if len(run.Token) >= 8 && strings.Contains(err.Error(), run.Token) {
					t.Fatalf("the refusal carries the token: %v", err)
				}
			}
			return
		}
		if handoff.Version != handoffV1 || len(handoff.Runs) == 0 {
			t.Fatalf("parsed %+v", handoff)
		}
		owners := map[string]bool{}
		for _, run := range handoff.Runs {
			if !githubOwner.MatchString(run.Owner) || len(run.Owner) > ownerLimit || owners[strings.ToLower(run.Owner)] {
				t.Fatalf("parsed the owner %q", run.Owner)
			}
			owners[strings.ToLower(run.Owner)] = true
			if !tokenShape.MatchString(run.Token) {
				t.Fatal("parsed a token that is not one printable line")
			}
			if len(run.Repositories) == 0 || len(slices.Compact(slices.Sorted(slices.Values(run.Repositories)))) != len(run.Repositories) {
				t.Fatalf("parsed the repositories %q", run.Repositories)
			}
			for _, repository := range run.Repositories {
				name, under := strings.CutPrefix(repository, run.Owner+"/")
				if !under || !githubName.MatchString(name) || name == "." || name == ".." {
					t.Fatalf("parsed %q under %q", repository, run.Owner)
				}
			}
		}
		spooled, err := json.Marshal(handoff)
		if err != nil {
			t.Fatalf("parsed a handoff root cannot spool: %v", err)
		}
		again, err := ParseHandoff(spooled)
		if err != nil {
			t.Fatalf("spooled a handoff the run refuses: %v", err)
		}
		if respooled, _ := json.Marshal(again); !bytes.Equal(respooled, spooled) {
			t.Fatalf("spooling changed the handoff: %s, then %s", spooled, respooled)
		}
	})
}
