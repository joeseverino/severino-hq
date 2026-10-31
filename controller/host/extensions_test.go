package host

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// lockOf is a merged lock carrying ids, as Cordon writes one.
func lockOf(ids ...string) string {
	plugins := []map[string]string{}
	for _, id := range ids {
		plugins = append(plugins, map[string]string{"plugin": id})
	}
	data, _ := json.Marshal(map[string]any{"schema_version": 1, "host": "severino-hq", "plugins": plugins})
	return string(data)
}

// extensionDeploy deploys next over a running release carrying runs, with
// next carrying carries and declaring retired (nil: ships no declaration).
func extensionDeploy(t *testing.T, runs, carries, retired []string) (*fakeHost, *handover, error) {
	t.Helper()
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	nextFiles := map[string]string{ExtensionLock: lockOf(carries...)}
	if retired != nil {
		data, _ := json.Marshal(retired)
		nextFiles[RetiredExtension] = string(data)
	}
	f.files = map[string]map[string]string{
		running: {ExtensionLock: lockOf(runs...)},
		next:    nextFiles,
	}
	d, exec := newDeployer(f)
	return f, exec, d.Deploy(context.Background(), next, nil)
}

func TestAReleaseThatKeepsEveryExtensionDeploys(t *testing.T) {
	for name, carries := range map[string][]string{
		"the same set": {"example.alpha", "example.beta"},
		"a superset":   {"example.alpha", "example.beta", "example.gamma"},
	} {
		t.Run(name, func(t *testing.T) {
			f, exec, err := extensionDeploy(t, []string{"example.alpha", "example.beta"}, carries, nil)
			if err != nil || exec.argv0 == "" {
				t.Fatalf("refused: %v\n%s", err, f.out.String())
			}
		})
	}
}

func TestAReleaseThatDropsAnExtensionIsRefused(t *testing.T) {
	for name, retired := range map[string][]string{
		"no declaration":                  nil,
		"an empty declaration":            {},
		"a declaration naming another id": {"example.gamma"},
	} {
		t.Run(name, func(t *testing.T) {
			f, exec, err := extensionDeploy(t, []string{"example.alpha", "example.beta"}, []string{"example.alpha"}, retired)

			if err == nil || !strings.Contains(err.Error(), "drops 1 extension(s)") {
				t.Fatalf("got %v", err)
			}
			if exec.argv0 != "" {
				t.Error("handed over to a release that drops an extension")
			}
			// Private: the public log and the error say how many, never which.
			if strings.Contains(err.Error()+f.out.String(), "example.beta") {
				t.Error("the dropped extension was named in the public log")
			}
			kept, _ := os.ReadFile(filepath.Join(f.machine.LogDir, "extensions.log"))
			if !strings.Contains(string(kept), "example.beta") || strings.Contains(string(kept), "example.alpha") {
				t.Errorf("the private log is %q", kept)
			}
		})
	}
}

func TestAnExtensionTheCompositionRetiredMayBeDropped(t *testing.T) {
	f, exec, err := extensionDeploy(t, []string{"example.alpha", "example.beta"}, []string{"example.alpha"}, []string{"example.beta"})

	if err != nil || exec.argv0 == "" {
		t.Fatalf("refused: %v\n%s", err, f.out.String())
	}
	if !strings.Contains(f.out.String(), "retired by the composition: 1") {
		t.Errorf("the retirement was not said: %s", f.out.String())
	}
}

func TestDroppingEveryExtensionIsRefusedAndTheEmptySetIsAllowedWhenDeclared(t *testing.T) {
	if _, _, err := extensionDeploy(t, []string{"example.alpha"}, nil, nil); err == nil {
		t.Error("a release with no extensions replaced one with some")
	}
	if _, _, err := extensionDeploy(t, []string{"example.alpha"}, nil, []string{"example.alpha"}); err != nil {
		t.Errorf("a declared empty composition was refused: %v", err)
	}
	if _, _, err := extensionDeploy(t, nil, nil, nil); err != nil {
		t.Errorf("an empty composition replacing an empty one was refused: %v", err)
	}
}

func TestTheFirstReleaseHasNothingToKeep(t *testing.T) {
	f := newFakeHost(t)
	f.files = map[string]map[string]string{next: {ExtensionLock: lockOf()}}
	d, exec := newDeployer(f)

	if err := d.Deploy(context.Background(), next, nil); err != nil || exec.argv0 == "" {
		t.Fatalf("refused: %v\n%s", err, f.out.String())
	}
}

func TestAnImageWhoseExtensionsCannotBeReadIsRefused(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	f.files = map[string]map[string]string{
		running: {ExtensionLock: lockOf("example.alpha")},
		next:    {ExtensionLock: `{"plugins": "not a list"}`},
	}
	d, exec := newDeployer(f)

	if err := d.Deploy(context.Background(), next, nil); err == nil || exec.argv0 != "" {
		t.Fatalf("deployed an image whose extensions are unknown: %v", err)
	}
}

func TestTheRunningSetIsReadBeforeThePruneAndPulledWhenGone(t *testing.T) {
	f, _, err := extensionDeploy(t, []string{"example.alpha"}, []string{"example.alpha"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	f.before(t, "docker create --pull never "+running, "docker image prune")
	if f.called("docker pull " + running) {
		t.Error("pulled a running image the host still has")
	}

	// Gone from the host: pulled by its digest, then read.
	f = newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	f.fail = []string{"docker create --pull never " + running}
	f.files = map[string]map[string]string{next: {ExtensionLock: lockOf()}}
	d, _ := newDeployer(f)
	if err := d.Deploy(context.Background(), next, nil); err == nil {
		t.Fatal("deployed without knowing what the running release carries")
	}
	if !f.called("docker pull " + running) {
		t.Error("the running image was not pulled by its digest")
	}
	if f.called("docker pull " + next) {
		t.Error("went on to pull the new image")
	}
}

func TestTheLockAndTheDeclarationAreReadStrictly(t *testing.T) {
	for _, bad := range []string{`{}`, `{"plugins": null}`, `{"plugins": [{"plugin": ""}]}`, `not json`} {
		if _, err := lockedIDs([]byte(bad)); err == nil {
			t.Errorf("read %s as a lock", bad)
		}
	}
	if ids, err := lockedIDs([]byte(lockOf("example.beta", "example.alpha"))); err != nil || strings.Join(ids, ",") != "example.alpha,example.beta" {
		t.Errorf("read %v %v", ids, err)
	}
	for _, bad := range []string{`null`, `{}`, `[""]`, `[1]`} {
		if _, err := retiredIDs([]byte(bad)); err == nil {
			t.Errorf("read %s as a declaration", bad)
		}
	}
}
