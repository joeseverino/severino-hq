package host

import (
	"bytes"
	"context"
	"encoding/base64"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/internal/sealing"
)

// seeds is every seed in the launch's sealing directory, by name.
func (l *launch) seeds(t *testing.T) map[string][]byte {
	t.Helper()
	names, err := filepath.Glob(filepath.Join(l.controller.Seal, "*.seed"))
	if err != nil {
		t.Fatal(err)
	}
	seeds := map[string][]byte{}
	for _, name := range names {
		data, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		seeds[filepath.Base(name)] = data
	}
	return seeds
}

func TestAPassMakesOneSealingKeyAndNeverReplacesIt(t *testing.T) {
	l := newLaunch(t)

	l.run(t, Plan)
	first := l.seeds(t)
	if len(first) != 1 {
		t.Fatalf("a first pass made %d seeds", len(first))
	}
	var key sealing.Key
	for name, seed := range first {
		var err error
		if key, err = sealing.NewKey(seed); err != nil {
			t.Fatal(err)
		}
		if name != key.ID+".seed" {
			t.Errorf("the seed of key %s is named %s", key.ID, name)
		}
		if info, _ := os.Stat(filepath.Join(l.controller.Seal, name)); info.Mode().Perm() != 0o400 {
			t.Errorf("the seed is %v", info.Mode().Perm())
		}
	}
	if info, _ := os.Stat(l.controller.Seal); info.Mode().Perm() != 0o700 {
		t.Errorf("the sealing directory is %v", info.Mode().Perm())
	}
	public := func() []byte {
		data, err := os.ReadFile(l.controller.SealPublic)
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(data)))
		if err != nil {
			t.Fatal(err)
		}
		return decoded
	}
	if got := public(); !bytes.Equal(got, key.PublicKey()) || len(got) != sealing.PublicKeySize {
		t.Errorf("the published key (%d bytes) is not the seed's", len(got))
	}

	// A public key gone or changed is put back as the seed's; the seed stays.
	if err := os.WriteFile(l.controller.SealPublic, []byte("a key of someone else's\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	l.run(t, Apply)
	if !bytes.Equal(public(), key.PublicKey()) {
		t.Error("a changed public key was left in place")
	}
	if err := os.Remove(l.controller.SealPublic); err != nil {
		t.Fatal(err)
	}
	l.run(t, Plan)
	if !bytes.Equal(public(), key.PublicKey()) {
		t.Error("a missing public key was not written again")
	}
	if again := l.seeds(t); len(again) != 1 || !bytes.Equal(again[key.ID+".seed"], first[key.ID+".seed"]) {
		t.Error("a later pass replaced the seed")
	}
}

func TestThePassIsLentItsKeysReadOnlyAndAGlanceNone(t *testing.T) {
	l := newLaunch(t)
	glanceDocument(t, l)

	l.run(t, Glance)
	if _, err := os.Stat(l.controller.Seal); err == nil {
		t.Error("a glance made the sealing directory")
	}
	if slices.ContainsFunc(l.args, func(arg string) bool { return strings.Contains(arg, controllerSeal) }) {
		t.Error("a glance was lent the sealing keys")
	}

	l.run(t, Apply)
	want := readOnly(l.controller.Seal, controllerSeal)
	if !slices.Contains(l.args, want) {
		t.Errorf("the pass was not lent its keys as %s", want)
	}
	if !slices.Contains(l.envs(), "HQ_SEAL_DIR="+controllerSeal) {
		t.Error("the pass was not told where its keys are")
	}
}

func TestASecondSeedStopsThePassRatherThanChoosing(t *testing.T) {
	l := newLaunch(t)
	l.run(t, Plan)
	if err := os.WriteFile(filepath.Join(l.controller.Seal, "00000000.seed"), bytes.Repeat([]byte{1}, sealing.SeedSize), 0o400); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(l.controller.SealPublic)

	if err := l.controller.Run(t.Context(), Plan); err == nil {
		t.Fatal("a pass ran with two seeds to choose between")
	}
	if after, _ := os.ReadFile(l.controller.SealPublic); !bytes.Equal(before, after) {
		t.Error("the public key changed while the seeds were in doubt")
	}
}

func TestHQIsGivenOnlyRootsPublicKey(t *testing.T) {
	uid := os.Getuid()
	for _, test := range []struct {
		name  string
		plant func(t *testing.T, path string)
		bound bool
	}{
		{"absent", func(*testing.T, string) {}, false},
		{"root's", func(t *testing.T, path string) { writeKey(t, path, 0o644) }, true},
		{"writable by others", func(t *testing.T, path string) { writeKey(t, path, 0o666) }, false},
		{"in a directory others can write", func(t *testing.T, path string) {
			writeKey(t, path, 0o644)
			if err := os.Chmod(filepath.Dir(path), 0o777); err != nil {
				t.Fatal(err)
			}
		}, false},
		{"a directory at the name", func(t *testing.T, path string) {
			if err := os.MkdirAll(path, 0o755); err != nil {
				t.Fatal(err)
			}
		}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "seal", "public.key")
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			test.plant(t, path)
			runner := &WebRunner{SealPublic: path, RootUID: uid}
			if got := runner.sealPublic(); got != test.bound {
				t.Errorf("a key %s is bound: %v", test.name, got)
			}
			if bound := (&WebRunner{SealPublic: path, RootUID: uid + 1}).sealPublic(); bound {
				t.Error("a key another account owns is bound")
			}
		})
	}

	args := Web{Image: "ghcr.io/example/hq@" + digest, SealPublic: "/var/lib/example/public.key"}.Args()
	if !slices.Contains(args, readOnly("/var/lib/example/public.key", "/run/secrets/seal_public_key")) ||
		!slices.Contains(args, "SEVERINO_SEAL_PUBLIC_KEY_FILE=/run/secrets/seal_public_key") {
		t.Errorf("the key is not bound read-only where HQ is told: %v", args)
	}
	if args := (Web{Image: "ghcr.io/example/hq@" + digest}).Args(); slices.ContainsFunc(args, func(arg string) bool {
		return strings.Contains(arg, "SEVERINO_SEAL_PUBLIC_KEY")
	}) {
		t.Error("HQ is told of a key it was not given")
	}
}

// A release on a host with no key yet makes one before it restarts HQ, which
// is when HQ reads the public half: no deploy leaves HQ unable to seal.
func TestAReleaseMakesTheKeyBeforeHQRestarts(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	d, _ := newDeployer(f)
	program, restarts, keyed := f.machine.Command, 0, 0
	f.machine.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		if name == "systemctl" && slices.Equal(args, []string{"restart", WebUnit}) {
			restarts++
			seeds, _ := filepath.Glob(filepath.Join(f.machine.Seal, "*.seed"))
			if _, err := os.Stat(f.machine.SealPublic); err == nil && len(seeds) == 1 {
				keyed++
			}
		}
		return program(ctx, name, args...)
	}

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}
	if restarts == 0 || keyed != restarts {
		t.Errorf("HQ restarted %d times, %d of them with its key in place", restarts, keyed)
	}
}

func writeKey(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, []byte("a key\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}
