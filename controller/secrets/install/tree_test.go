package install

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func tmpfs(string) error { return nil }

// layout is a host's directories under a temporary root, owned by this account.
func layout(t *testing.T) Layout {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	found := Layout{SecretDir: filepath.Join(root, "secrets"), RuntimeDir: filepath.Join(root, "runtime"),
		RootUID: os.Getuid(), RootGID: os.Getgid(), WebUID: os.Getuid(), WebGID: os.Getgid()}
	found.WebDir = filepath.Join(found.RuntimeDir, "web")
	if err := os.Mkdir(found.RuntimeDir, 0o700); err != nil {
		t.Fatal(err)
	}
	return found
}

func open(t *testing.T) *Tree {
	t.Helper()
	tree, err := Open(layout(t), tmpfs)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(tree.Close)
	return tree
}

func inode(t *testing.T, path string) uint64 {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return uint64(info.Sys().(*syscall.Stat_t).Ino)
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestRuntimePathRefusals(t *testing.T) {
	for _, dir := range []string{
		"/run/severino-hq", "/run/severino-hq/credentials", "/run//severino-hq", "/run/./severino-hq",
		"/run/../run/secrets", "/run/severino-hq-secrets/", "/run/severino-hq-secrets/.", "/run/severino-hq-secrets/..",
		"run/severino-hq-secrets", "", "./secrets",
	} {
		if err := ValidateRuntimePath(dir); !errors.Is(err, ErrHost) {
			t.Errorf("%q was accepted", dir)
		}
	}
	if err := ValidateRuntimePath("/run/severino-hq-secrets"); err != nil {
		t.Fatal(err)
	}
}

func TestOpenRefusesAnUnsafeRuntimeDirectory(t *testing.T) {
	cases := map[string]func(*Layout){
		"open to the group":   func(l *Layout) { os.Chmod(l.RuntimeDir, 0o750) },
		"open to everyone":    func(l *Layout) { os.Chmod(l.RuntimeDir, 0o755) },
		"closed to its owner": func(l *Layout) { os.Chmod(l.RuntimeDir, 0o500) },
		"another owner":       func(l *Layout) { l.RootUID++ },
		"missing":             func(l *Layout) { os.Remove(l.RuntimeDir) },
		"a file":              func(l *Layout) { os.Remove(l.RuntimeDir); os.WriteFile(l.RuntimeDir, nil, 0o600) },
		"a link": func(l *Layout) {
			real := l.RuntimeDir + "-real"
			os.Rename(l.RuntimeDir, real)
			os.Symlink(real, l.RuntimeDir)
		},
		"reached through a link": func(l *Layout) {
			parent := filepath.Dir(l.RuntimeDir)
			os.Symlink(parent, parent+"-link")
			l.RuntimeDir = filepath.Join(parent+"-link", "runtime")
			l.WebDir = filepath.Join(l.RuntimeDir, "web")
		},
		"the doorbell directory": func(l *Layout) { l.RuntimeDir = "/run/severino-hq" },
		"a web directory that is a link": func(l *Layout) {
			elsewhere := l.RuntimeDir + "-elsewhere"
			os.Mkdir(elsewhere, 0o700)
			os.Symlink(elsewhere, l.WebDir)
		},
		"a secret directory that is a link": func(l *Layout) {
			elsewhere := l.RuntimeDir + "-elsewhere"
			os.Mkdir(elsewhere, 0o700)
			os.Symlink(elsewhere, l.SecretDir)
		},
		"a relative secret directory": func(l *Layout) { l.SecretDir = "secrets" },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			found := layout(t)
			change(&found)
			tree, err := Open(found, tmpfs)
			if !errors.Is(err, ErrHost) {
				tree.Close()
				t.Fatalf("accepted: %v", err)
			}
		})
	}
}

func TestOpenAsksTheMountTableBeforeAnythingElseIsMade(t *testing.T) {
	found := layout(t)
	asked := []string{}
	_, err := Open(found, func(dir string) error {
		asked = append(asked, dir)
		return hostError("Controller secret directory must be on tmpfs.")
	})
	if !errors.Is(err, ErrHost) || len(asked) != 1 || asked[0] != found.RuntimeDir {
		t.Fatalf("mount check: %v %v", asked, err)
	}
	if _, err := os.Lstat(found.WebDir); err == nil {
		t.Fatal("the web directory was made on a refused mount")
	}
}

// The application environment is as secret as the rest: a web directory
// outside the runtime mount is held to the same memory.
func TestTheWebDirectoryMustBeOnTheSameKindOfMemory(t *testing.T) {
	found := layout(t)
	found.WebDir = filepath.Join(filepath.Dir(found.RuntimeDir), "web-on-disk")
	asked := []string{}
	tree, err := Open(found, func(dir string) error {
		asked = append(asked, dir)
		if dir == found.WebDir {
			return hostError("Controller secret directory must be on tmpfs.")
		}
		return nil
	})
	if !errors.Is(err, ErrHost) {
		tree.Close()
		t.Fatalf("a web directory on a disk was accepted (asked %v): %v", asked, err)
	}
	if len(asked) != 2 || asked[1] != found.WebDir {
		t.Fatalf("the web directory's mount was not asked about: %v", asked)
	}
}

// A link where a private directory should be is refused before anything is
// done to what it points at.
func TestALinkedDirectoryIsNotReownedOrRemoded(t *testing.T) {
	for name, place := range map[string]func(*Layout) string{
		"the secret directory": func(l *Layout) string { return l.SecretDir },
		"the web directory":    func(l *Layout) string { return l.WebDir },
	} {
		t.Run(name, func(t *testing.T) {
			found := layout(t)
			elsewhere := found.RuntimeDir + "-elsewhere"
			if err := os.Mkdir(elsewhere, 0o755); err != nil {
				t.Fatal(err)
			}
			os.Chmod(elsewhere, 0o755)
			if err := os.Symlink(elsewhere, place(&found)); err != nil {
				t.Fatal(err)
			}
			tree, err := Open(found, tmpfs)
			if !errors.Is(err, ErrHost) {
				tree.Close()
				t.Fatalf("accepted: %v", err)
			}
			if info, _ := os.Stat(elsewhere); info.Mode().Perm() != 0o755 {
				t.Fatalf("the link's target was changed to mode %o", info.Mode().Perm())
			}
		})
	}
}

func TestInPlaceInstall(t *testing.T) {
	tree := open(t)
	dir, uid, gid := tree.Layout.WebDir, os.Getuid(), os.Getgid()
	path := filepath.Join(dir, AppEnvName)

	// A fresh destination is created.
	changed, err := InPlace(tree.Web(), AppEnvName, []byte("SECRET='one'\n"), uid, gid, 0o400)
	if err != nil || !changed || read(t, path) != "SECRET='one'\n" {
		t.Fatalf("a fresh destination was refused: %v", err)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o400 {
		t.Fatalf("mode %o", info.Mode().Perm())
	}
	// An own file is rewritten in place: the bind mount would keep the old inode.
	before := inode(t, path)
	changed, err = InPlace(tree.Web(), AppEnvName, []byte("SECRET='two'\n"), uid, gid, 0o400)
	if err != nil || !changed || read(t, path) != "SECRET='two'\n" {
		t.Fatalf("an own file was not rewritten: %v", err)
	}
	if inode(t, path) != before {
		t.Fatal("an own file lost its inode")
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o400 {
		t.Fatalf("mode after rewrite %o", info.Mode().Perm())
	}
	// Unchanged content is not a change, and the inode stays.
	changed, err = InPlace(tree.Web(), AppEnvName, []byte("SECRET='two'\n"), uid, gid, 0o400)
	if err != nil || changed || inode(t, path) != before {
		t.Fatalf("an identical install changed something: %v", err)
	}
	// A shorter value leaves nothing of the longer one behind.
	if _, err = InPlace(tree.Web(), AppEnvName, []byte("S=1\n"), uid, gid, 0o400); err != nil || read(t, path) != "S=1\n" {
		t.Fatalf("truncation: %v", err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatalf("temporary files were left: %d entries", len(entries))
	}
}

func TestInPlaceRefusesWhatIsNotAFileOfItsOwn(t *testing.T) {
	tree := open(t)
	dir, uid, gid := tree.Layout.WebDir, os.Getuid(), os.Getgid()
	system := filepath.Join(filepath.Dir(tree.Layout.RuntimeDir), "system-file")
	if err := os.WriteFile(system, []byte("system"), 0o600); err != nil {
		t.Fatal(err)
	}
	refused := func(name, why string) {
		t.Helper()
		changed, err := InPlace(tree.Web(), name, []byte("SECRET='three'\n"), uid, gid, 0o400)
		if !errors.Is(err, ErrHost) || changed {
			t.Fatalf("%s was written: %v", why, err)
		}
		if read(t, system) != "system" {
			t.Fatalf("%s: the other file was overwritten", why)
		}
	}
	// A link planted at the name: not followed.
	os.Symlink(system, filepath.Join(dir, "linked"))
	refused("linked", "a symlink destination")
	// A link that dangles is still not a file.
	os.Symlink(filepath.Join(dir, "nowhere"), filepath.Join(dir, "dangling"))
	refused("dangling", "a dangling symlink")
	if _, err := os.Lstat(filepath.Join(dir, "nowhere")); err == nil {
		t.Fatal("a dangling link was written through")
	}
	// A hard link to another file: not written.
	if err := os.Link(system, filepath.Join(dir, "hardlinked")); err != nil {
		t.Fatal(err)
	}
	refused("hardlinked", "a hard-linked destination")
	// A directory where a file should be.
	os.Mkdir(filepath.Join(dir, "dir"), 0o700)
	refused("dir", "a directory destination")
	// Another account's file.
	os.WriteFile(filepath.Join(dir, "foreign"), []byte("theirs"), 0o600)
	if changed, err := InPlace(tree.Web(), "foreign", []byte("x"), uid+1, gid, 0o400); !errors.Is(err, ErrHost) || changed {
		t.Fatalf("another account's file was written: %v", err)
	}
	if read(t, filepath.Join(dir, "foreign")) != "theirs" {
		t.Fatal("another account's file was overwritten")
	}
	// A name that leaves the directory.
	for _, name := range []string{"../escaped", "/etc/escaped", "sub/../../escaped"} {
		if _, err := InPlace(tree.Web(), name, []byte("x"), uid, gid, 0o400); err == nil {
			t.Fatalf("%q was written", name)
		}
	}
	if _, err := os.Lstat(filepath.Join(tree.Layout.RuntimeDir, "escaped")); err == nil {
		t.Fatal("a write escaped its directory")
	}
}

func TestReadTrustsOnlyTheRenderersOwnFile(t *testing.T) {
	tree := open(t)
	dir, uid := tree.Layout.RuntimeDir, os.Getuid()
	os.WriteFile(filepath.Join(dir, "own"), []byte("value"), 0o400)
	if data, ok := Read(tree.Runtime(), "own", uid, 0o400); !ok || string(data) != "value" {
		t.Fatal("an own file is not trusted")
	}
	if _, ok := Read(tree.Runtime(), "own", uid+1, 0o400); ok {
		t.Fatal("another uid's file is trusted")
	}
	if _, ok := Read(tree.Runtime(), "own", uid, 0o600); ok {
		t.Fatal("a file with another mode is trusted")
	}
	os.Symlink(filepath.Join(dir, "own"), filepath.Join(dir, "linked"))
	if _, ok := Read(tree.Runtime(), "linked", uid, 0o400); ok {
		t.Fatal("a link is trusted")
	}
	os.Link(filepath.Join(dir, "own"), filepath.Join(dir, "hard"))
	if _, ok := Read(tree.Runtime(), "own", uid, 0o400); ok {
		t.Fatal("a hard-linked file is trusted")
	}
	if _, ok := Read(tree.Runtime(), "absent", uid, 0o400); ok {
		t.Fatal("a missing file is trusted")
	}
}

func TestLockRefusesASecondRenderer(t *testing.T) {
	found := layout(t)
	first, err := Open(found, tmpfs)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	second, err := Open(found, tmpfs)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	unlock, err := first.Lock()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := second.Lock(); !errors.Is(err, ErrBusy) {
		t.Fatalf("a second renderer took the lock: %v", err)
	}
	unlock()
	again, err := second.Lock()
	if err != nil {
		t.Fatalf("the lock outlived its holder: %v", err)
	}
	again()
	// The lock is a file of its own; a link at its name is not followed.
	os.Remove(filepath.Join(found.RuntimeDir, lockName))
	os.Symlink(filepath.Join(found.RuntimeDir, "elsewhere"), filepath.Join(found.RuntimeDir, lockName))
	if _, err := first.Lock(); !errors.Is(err, ErrHost) {
		t.Fatalf("a linked lock file was followed: %v", err)
	}
}

func TestSSHLockWaitsForAReaderAndGivesUpWithTheContext(t *testing.T) {
	tree := open(t)
	// The launcher's shared lock, as flock(1) takes it.
	reader, err := os.OpenFile(filepath.Join(tree.Layout.RuntimeDir, sshLockName), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if err := syscall.Flock(int(reader.Fd()), syscall.LOCK_SH); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := tree.LockSSH(ctx); !errors.Is(err, ErrHost) {
		t.Fatalf("the exclusive lock was taken over a reader: %v", err)
	}
	syscall.Flock(int(reader.Fd()), syscall.LOCK_UN)
	unlock, err := tree.LockSSH(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if syscall.Flock(int(reader.Fd()), syscall.LOCK_SH|syscall.LOCK_NB) == nil {
		t.Fatal("a reader got in while the renderer held the lock")
	}
	unlock()
}

func TestStagingIsPrivateAndOnlyItsOwnIsRemoved(t *testing.T) {
	tree := open(t)
	runtime := tree.Layout.RuntimeDir
	// A directory a killed run left, and a launcher's run directory beside it.
	os.MkdirAll(filepath.Join(runtime, ".refresh.abandoned", "ssh"), 0o700)
	os.WriteFile(filepath.Join(runtime, ".refresh.abandoned", "ssh", "edge"), []byte("old key"), 0o400)
	os.Mkdir(filepath.Join(runtime, "run.launcher"), 0o700)
	stage, err := tree.NewStage()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(runtime, ".refresh.abandoned")); err == nil {
		t.Fatal("an abandoned staging directory was left holding secrets")
	}
	if _, err := os.Lstat(filepath.Join(runtime, "run.launcher")); err != nil {
		t.Fatal("the launcher's run directory was removed")
	}
	entries, _ := filepath.Glob(filepath.Join(runtime, ".refresh.*"))
	if len(entries) != 1 {
		t.Fatalf("staging directories: %v", entries)
	}
	if info, _ := os.Stat(entries[0]); info.Mode().Perm() != 0o700 {
		t.Fatalf("staging mode %o", info.Mode().Perm())
	}
	if err := stage.Write("connections", []byte("{}"), os.Getuid(), os.Getgid(), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := stage.Write("connections", []byte("{}"), os.Getuid(), os.Getgid(), 0o400); !errors.Is(err, ErrHost) {
		t.Fatal("a staged file was overwritten")
	}
	if err := stage.Write("../escaped", []byte("{}"), os.Getuid(), os.Getgid(), 0o400); err == nil {
		t.Fatal("a staged write left the staging directory")
	}
	// The run's own lock is not staging: taking a stage leaves it held.
	unlock, err := tree.Lock()
	if err != nil {
		t.Fatal(err)
	}
	stage.Close()
	if stage, err = tree.NewStage(); err != nil {
		t.Fatal(err)
	}
	second, err := Open(tree.Layout, tmpfs)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := second.Lock(); !errors.Is(err, ErrBusy) {
		t.Fatalf("staging removed the held lock: %v", err)
	}
	second.Close()
	unlock()
	if err := stage.Write("connections", []byte("{}"), os.Getuid(), os.Getgid(), 0o400); err != nil {
		t.Fatal(err)
	}
	changed, err := stage.Rename("connections", ConnectionsName, []byte("{}"), 0o400, nil)
	if err != nil || !changed || read(t, filepath.Join(runtime, ConnectionsName)) != "{}" {
		t.Fatalf("rename install: %v", err)
	}
	stage.Close()
	if entries, _ := filepath.Glob(filepath.Join(runtime, ".refresh.*")); len(entries) != 0 {
		t.Fatalf("staging was left behind: %v", entries)
	}
}

func TestRenameInstallReplacesTheInodeOnlyWhenTheFileDiffers(t *testing.T) {
	tree := open(t)
	runtime := tree.Layout.RuntimeDir
	path := filepath.Join(runtime, ConnectionsName)
	install := func(data string, same func([]byte) bool) bool {
		t.Helper()
		stage, err := tree.NewStage()
		if err != nil {
			t.Fatal(err)
		}
		defer stage.Close()
		if err := stage.Write("connections", []byte(data), os.Getuid(), os.Getgid(), 0o400); err != nil {
			t.Fatal(err)
		}
		changed, err := stage.Rename("connections", ConnectionsName, []byte(data), 0o400, same)
		if err != nil {
			t.Fatal(err)
		}
		return changed
	}
	if !install("one", nil) {
		t.Fatal("a first install was not a change")
	}
	first := inode(t, path)
	if install("one", nil) || inode(t, path) != first {
		t.Fatal("an identical file was replaced")
	}
	if !install("two", nil) || inode(t, path) == first || read(t, path) != "two" {
		t.Fatal("a changed file kept its inode: a reader could see half of each")
	}
	if install("three", func([]byte) bool { return true }) || read(t, path) != "two" {
		t.Fatal("a file its comparison calls the same was replaced")
	}
	// A wrong mode, a link and a directory at the name are all replaced by the file.
	os.Chmod(path, 0o644)
	if !install("two", nil) {
		t.Fatal("a file with the wrong mode was kept")
	}
	os.Remove(path)
	os.Symlink(filepath.Join(runtime, "elsewhere"), path)
	if !install("four", nil) || read(t, path) != "four" {
		t.Fatal("a link at the name was kept")
	}
	if _, err := os.Lstat(filepath.Join(runtime, "elsewhere")); err == nil {
		t.Fatal("the link was written through")
	}
	os.Remove(path)
	os.MkdirAll(filepath.Join(path, "nested"), 0o700)
	if !install("five", nil) || read(t, path) != "five" {
		t.Fatal("a directory at the name was kept")
	}
}

func TestLegacyPrivateKeysStopTheRefresh(t *testing.T) {
	tree := open(t)
	legacy := filepath.Join(tree.Layout.SecretDir, SSHDirName)
	if err := tree.LegacyKeys(); err != nil {
		t.Fatalf("no legacy directory was refused: %v", err)
	}
	os.MkdirAll(filepath.Join(legacy, "nested"), 0o700)
	// Public halves alone do not stop it.
	os.WriteFile(filepath.Join(legacy, "edge.pub"), []byte("ssh-ed25519 AAAA example\n"), 0o644)
	if err := tree.LegacyKeys(); err != nil {
		t.Fatalf("public halves alone were refused: %v", err)
	}
	os.WriteFile(filepath.Join(legacy, "nested", "edge"), []byte("-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n"), 0o600)
	err := tree.LegacyKeys()
	if !errors.Is(err, ErrHost) || !strings.Contains(err.Error(), legacy) {
		t.Fatalf("private keys on disk were accepted: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(legacy, "nested", "edge")); statErr != nil {
		t.Fatal("the renderer removed a key; that is the operator's decision")
	}
}

func TestPruneDropsEverythingNotWanted(t *testing.T) {
	tree := open(t)
	if err := tree.EnsureSSHDir(); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(tree.Layout.RuntimeDir, SSHDirName)
	for _, name := range []string{"edge", "edge.pub", "retired", ".hidden"} {
		os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o400)
	}
	os.MkdirAll(filepath.Join(dir, "stray", "deep"), 0o700)
	changed, err := tree.PruneSSH(map[string]bool{"edge": true, "edge.pub": true})
	if err != nil || !changed {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 2 {
		t.Fatalf("left %d entries", len(entries))
	}
	if changed, _ := tree.PruneSSH(map[string]bool{"edge": true, "edge.pub": true}); changed {
		t.Fatal("pruning nothing reported a change")
	}
	// A link in place of the directory is refused, not followed.
	os.RemoveAll(dir)
	os.Symlink(tree.Layout.SecretDir, dir)
	if err := tree.EnsureSSHDir(); !errors.Is(err, ErrHost) {
		t.Fatalf("a linked identity directory was accepted: %v", err)
	}
}

func TestMountInfo(t *testing.T) {
	const dir = "/run/severino-hq-secrets"
	root := "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw,errors=remount-ro\n"
	run := "25 22 0:23 / /run rw,nosuid,nodev shared:5 - tmpfs tmpfs rw,size=800000k,mode=755\n"
	own := "90 25 0:50 / /run/severino-hq-secrets rw,nosuid,nodev,noexec,relatime shared:40 - tmpfs tmpfs rw,size=16384k,mode=700,noswap\n"
	accepted := map[string]string{
		"a mount of its own with noswap":    root + run + own,
		"noswap on the mount it lies under": root + "25 22 0:23 / /run rw - tmpfs tmpfs rw,noswap\n",
		"a subdirectory of the mount":       root + run + own,
		"an escaped mount point":            root + "90 25 0:50 / /run/severino\\055hq\\055secrets rw - tmpfs tmpfs rw,noswap\n",
		"optional fields before the dash":   root + "90 25 0:50 / /run/severino-hq-secrets rw shared:1 master:2 propagate_from:3 - tmpfs tmpfs rw,noswap\n",
		"a sibling with a longer name":      root + own + "91 25 0:51 / /run/severino-hq-secrets-other rw - ext4 /dev/sdb rw\n",
	}
	for name, table := range accepted {
		target := dir
		if name == "a subdirectory of the mount" {
			target = dir + "/web"
		}
		if err := CheckMountInfo([]byte(table), target); err != nil {
			t.Errorf("%s was refused: %v", name, err)
		}
	}
	refused := map[string]string{
		"disk":                       root,
		"a disk mounted at the path": root + run + "90 25 8:2 / /run/severino-hq-secrets rw - ext4 /dev/sdb rw\n",
		"swappable tmpfs":            root + run + "90 25 0:50 / /run/severino-hq-secrets rw - tmpfs tmpfs rw,size=16384k\n",
		"under a swappable tmpfs":    root + run,
		// noswapfile is not noswap.
		"a lookalike option": root + "90 25 0:50 / /run/severino-hq-secrets rw,nosuid - tmpfs tmpfs rw,noswapfile\n",
		// A disk mounted over the tmpfs is what the directory now is.
		"a disk stacked on the tmpfs": root + run + own + "95 25 8:2 / /run/severino-hq-secrets rw - ext4 /dev/sdb rw\n",
		// A swappable tmpfs stacked on the protected one hides it.
		"a swappable tmpfs stacked on top": root + run + own + "95 25 0:60 / /run/severino-hq-secrets rw - tmpfs tmpfs rw\n",
		"a tmpfs stacked on a disk":        root + "90 25 8:2 / /run/severino-hq-secrets rw - ext4 /dev/sdb rw\n" + own,
		"an empty table":                   "",
		"an unreadable table":              "not a mount table\n",
		"noswap in the source name":        root + "90 25 0:50 / /run/severino-hq-secrets rw - tmpfs noswap rw\n",
	}
	for name, table := range refused {
		if err := CheckMountInfo([]byte(table), dir); !errors.Is(err, ErrHost) {
			t.Errorf("%s was accepted", name)
		}
	}
}
