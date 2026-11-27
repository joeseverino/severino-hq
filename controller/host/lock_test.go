package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// A lock another account could reach is refused before it is opened: a
// symlink planted at its path, and a directory where a file could be swapped
// in for one someone else holds.
func TestALockAnotherAccountCouldReachIsRefused(t *testing.T) {
	ctx := context.Background()

	dir := t.TempDir()
	elsewhere := filepath.Join(t.TempDir(), "created")
	if err := os.Symlink(elsewhere, filepath.Join(dir, "planted.lock")); err != nil {
		t.Fatal(err)
	}
	for name, take := range map[string]func(context.Context) (func(), error){
		"shared":    Lock{Path: filepath.Join(dir, "planted.lock")}.Shared,
		"exclusive": Lock{Path: filepath.Join(dir, "planted.lock")}.Exclusive,
	} {
		if release, err := take(ctx); err == nil {
			release()
			t.Errorf("the %s take followed a planted symlink", name)
		}
		if _, err := os.Lstat(elsewhere); err == nil {
			t.Fatalf("the %s take created the file a planted symlink names", name)
		}
	}

	open := t.TempDir()
	if err := os.Chmod(open, 0o777); err != nil {
		t.Fatal(err)
	}
	swapped := filepath.Join(open, "swapped.lock")
	if err := os.WriteFile(swapped, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if release, err := (Lock{Path: swapped}).Exclusive(ctx); err == nil {
		release()
		t.Error("a lock in a directory others can write was taken")
	}
}

// A take that waits out its wait says which lock was held, so a deploy that
// cannot hold the units ends with a reason instead of blocking.
func TestATakeThatRunsOutNamesTheLock(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "deploy.lock")
	release, err := Lock{Path: path}.Shared(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	_, err = Lock{Path: path}.Exclusive(ctx)
	if held := (LockHeld{}); !errors.As(err, &held) || !strings.Contains(err.Error(), path) {
		t.Fatalf("an exclusive take under a shared holder answered %v", err)
	}
	if again, err := (Lock{Path: path}).Shared(ctx); err != nil {
		t.Errorf("a second shared holder was refused: %v", err)
	} else {
		again()
	}
}

// The deploy lock is made at boot where only root can enter, and a deploy
// waits out the longest unit request before it gives up.
func TestTheDeployLockIsRootsAndOutlastsAUnitRequest(t *testing.T) {
	rules, err := os.ReadFile(filepath.Join("..", "..", "deploy", "tmpfiles", "severino-hq.conf"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(rules), "\n")
	for _, want := range []string{
		fmt.Sprintf("d %s 0700 root root -", DeployLockDir),
		fmt.Sprintf("f %s 0600 root root -", DeployLock),
	} {
		if !slices.Contains(lines, want) {
			t.Errorf("no tmpfiles rule %q", want)
		}
	}
	if request := unitSetting(t, "severino-hq-unit@.service", "RuntimeMaxSec"); deployLockWait <= request {
		t.Errorf("a deploy gives up on the lock after %s, inside a unit request's %s", deployLockWait, request)
	}
}
