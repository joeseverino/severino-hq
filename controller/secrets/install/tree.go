// Package install owns every file the renderer touches: the private
// directories, the locks, the staging directory, and the two ways a rendered
// file reaches its place. Every path is opened beneath an os.Root, so no
// write can leave the directory it was meant for.
package install

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Names inside the layout. The application environment keeps the name and
// format hq/config/settings.py loads.
const (
	AppEnvName      = "severino_hq_env"
	ConnectionsName = "controller-connections.json"
	SSHDirName      = "ssh"
	lockName        = ".refresh.lock"
	sshLockName     = "ssh.lock"
	mcpTokenName    = "severino_mcp_token"
	stagePrefix     = ".refresh."
	maxFileBytes    = 16 << 20
	sshLockWait     = 60 * time.Second
)

// ErrHost is a host that is not as the renderer requires; ErrBusy another
// renderer holding the lock. Match with errors.Is.
var (
	ErrHost = errors.New("the host was refused")
	ErrBusy = errors.New("Secret refresh is already running.")
)

type refusal struct{ message string }

func (e *refusal) Error() string { return e.message }
func (e *refusal) Unwrap() error { return ErrHost }

func hostError(parts ...string) error { return &refusal{message: strings.Join(parts, "")} }

// Layout is where everything lives and who owns it.
type Layout struct {
	// SecretDir is on disk, under the deploy checkout: the lock, and the
	// checkout's copy of the application environment while a container binds it.
	SecretDir string
	// RuntimeDir is the private tmpfs: connections, identities, staging.
	RuntimeDir string
	// WebDir holds the application environment the web container binds.
	WebDir string
	// RootUID and RootGID own the directories and the controller's files;
	// WebUID and WebGID own the application environment.
	RootUID, RootGID, WebUID, WebGID int
}

// ValidateRuntimePath refuses a runtime directory that is, or is under, the
// web container's doorbell mount, or that is not one clean absolute path.
func ValidateRuntimePath(dir string) error {
	if !strings.HasPrefix(dir, "/") {
		return hostError("Controller secret directory must be absolute.")
	}
	if dir == "/run/severino-hq" || strings.HasPrefix(dir, "/run/severino-hq/") ||
		strings.Contains(dir, "//") || strings.Contains(dir, "/../") || strings.Contains(dir, "/./") ||
		strings.HasSuffix(dir, "/..") || strings.HasSuffix(dir, "/.") || strings.HasSuffix(dir, "/") {
		return hostError("Unsafe controller secret directory.")
	}
	return nil
}

func owner(info fs.FileInfo) (uid int, links uint64, ok bool) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, false
	}
	return int(stat.Uid), uint64(stat.Nlink), true
}

// openPrivate opens a directory that must already be a real directory, not a
// link, owned by uid with mode 0700, and returns it as a root.
func openPrivate(dir string, uid int, what string) (*os.Root, error) {
	listed, err := os.Lstat(dir)
	if err != nil || listed.Mode()&os.ModeSymlink != 0 || !listed.IsDir() {
		return nil, hostError(what, " must be a directory, not a link.")
	}
	// A link above the directory would let the mount check answer for
	// somewhere else.
	if resolved, err := filepath.EvalSymlinks(dir); err != nil || resolved != dir {
		return nil, hostError(what, " must not be reached through a link.")
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, hostError(what, " could not be opened.")
	}
	opened, err := root.Stat(".")
	held, _, ok := owner(listed)
	if err != nil || !os.SameFile(listed, opened) || !ok || held != uid || opened.Mode().Perm() != 0o700 {
		root.Close()
		return nil, hostError(what, " must be owned by uid ", itoa(uid), " with mode 0700.")
	}
	return root, nil
}

func itoa(n int) string { return strconv.Itoa(n) }

// ensurePrivate makes dir a directory only uid can enter, as `install -d -m
// 700` did, but never through a link.
func ensurePrivate(dir string, uid, gid int, what string) error {
	if _, err := os.Lstat(dir); errors.Is(err, fs.ErrNotExist) {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return hostError(what, " could not be created.")
		}
	}
	handle, err := os.OpenFile(dir, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if err != nil {
		return hostError(what, " must be a directory, not a link.")
	}
	defer handle.Close()
	if err := handle.Chown(uid, gid); err != nil {
		return hostError(what, " could not be given to uid ", itoa(uid), ".")
	}
	if err := handle.Chmod(0o700); err != nil {
		return hostError(what, " could not be closed to other accounts.")
	}
	return nil
}

// Tree is the opened layout.
type Tree struct {
	Layout  Layout
	secret  *os.Root
	runtime *os.Root
	web     *os.Root
}

// Open checks the layout and opens it. Nothing is read from 1Password before
// this has passed: a disk-backed or swappable runtime directory is refused
// first.
func Open(layout Layout, mounts MountChecker) (*Tree, error) {
	if err := ValidateRuntimePath(layout.RuntimeDir); err != nil {
		return nil, err
	}
	if !filepath.IsAbs(layout.SecretDir) || !filepath.IsAbs(layout.WebDir) ||
		filepath.Clean(layout.SecretDir) != layout.SecretDir || filepath.Clean(layout.WebDir) != layout.WebDir {
		return nil, hostError("Secret directories must be clean absolute paths.")
	}
	if err := ensurePrivate(layout.SecretDir, layout.RootUID, layout.RootGID, "The secret directory"); err != nil {
		return nil, err
	}
	tree := &Tree{Layout: layout}
	var err error
	if tree.runtime, err = openPrivate(layout.RuntimeDir, layout.RootUID, "Controller secret directory"); err != nil {
		return nil, err
	}
	if err := mounts(layout.RuntimeDir); err != nil {
		tree.Close()
		return nil, err
	}
	if tree.secret, err = openPrivate(layout.SecretDir, layout.RootUID, "The secret directory"); err != nil {
		tree.Close()
		return nil, err
	}
	if err := ensurePrivate(layout.WebDir, layout.RootUID, layout.RootGID, "The web secret directory"); err != nil {
		tree.Close()
		return nil, err
	}
	if tree.web, err = openPrivate(layout.WebDir, layout.RootUID, "The web secret directory"); err != nil {
		tree.Close()
		return nil, err
	}
	// The application environment is as secret as the rest: its directory is
	// held to the same memory.
	if err := mounts(layout.WebDir); err != nil {
		tree.Close()
		return nil, err
	}
	return tree, nil
}

// Close releases the directory handles.
func (t *Tree) Close() {
	for _, root := range []*os.Root{t.secret, t.runtime, t.web} {
		if root != nil {
			root.Close()
		}
	}
}

func flock(file *os.File, how int) error {
	for {
		err := syscall.Flock(int(file.Fd()), how)
		if err != syscall.EINTR {
			return err
		}
	}
}

// Lock refuses a concurrent run. The lock is not about speed: two renderers
// interleaving their installs is how a host ends up holding files from two
// different reads of the vault.
func (t *Tree) Lock() (func(), error) {
	file, err := t.secret.OpenFile(lockName, os.O_WRONLY|os.O_CREATE|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, hostError("The refresh lock could not be opened.")
	}
	if err := flock(file, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, ErrBusy
	}
	return func() { file.Close() }, nil
}

// LockSSH serializes the connections document and the identities as one
// generation: the launcher holds the shared lock while it copies them.
func (t *Tree) LockSSH(ctx context.Context) (func(), error) {
	file, err := t.runtime.OpenFile(sshLockName, os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, hostError("The SSH identity lock could not be opened.")
	}
	deadline := time.Now().Add(sshLockWait)
	for {
		if err := flock(file, syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
			return func() { file.Close() }, nil
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			file.Close()
			return nil, hostError("Timed out waiting for the SSH identity lock.")
		}
		select {
		case <-ctx.Done():
		case <-time.After(100 * time.Millisecond):
		}
	}
}

var privateKey = regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`)

// LegacyKeys refuses while private keys remain in the checkout's ssh
// directory. Private keys on disk outlive every rotation; removing them is the
// operator's decision, and refusing keeps a refresh from reporting a clean
// state meanwhile.
func (t *Tree) LegacyKeys() error {
	found := false
	err := fs.WalkDir(t.secret.FS(), SSHDirName, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			if path == SSHDirName && errors.Is(err, fs.ErrNotExist) {
				return fs.SkipAll
			}
			return err
		}
		if !entry.Type().IsRegular() {
			return nil
		}
		file, err := t.secret.Open(path)
		if err != nil {
			return err
		}
		defer file.Close()
		data, err := io.ReadAll(io.LimitReader(file, maxFileBytes))
		if err != nil {
			return err
		}
		if privateKey.Match(data) {
			found = true
			return fs.SkipAll
		}
		return nil
	})
	if err != nil {
		return hostError("The checkout's ssh directory could not be read.")
	}
	if found {
		return hostError("Refusing: private keys remain in ", filepath.Join(t.Layout.SecretDir, SSHDirName),
			". Remove them by hand; identities now come from the vault.")
	}
	return nil
}

// Stage is one run's staging directory on the private tmpfs. Everything is
// read, validated and written here before any installed file is touched.
type Stage struct {
	tree *Tree
	name string
	root *os.Root
}

// NewStage clears what a killed run left and makes this run's directory. Call
// it holding the lock: under it, any other staging directory is abandoned.
func (t *Tree) NewStage() (*Stage, error) {
	directory, err := t.runtime.Open(".")
	if err != nil {
		return nil, hostError("The controller secret directory could not be read.")
	}
	names, err := directory.Readdirnames(-1)
	directory.Close()
	if err != nil {
		return nil, hostError("The controller secret directory could not be read.")
	}
	for _, name := range names {
		if strings.HasPrefix(name, stagePrefix) {
			if err := t.runtime.RemoveAll(name); err != nil {
				return nil, hostError("An abandoned staging directory could not be removed.")
			}
		}
	}
	random := make([]byte, 8)
	if _, err := rand.Read(random); err != nil {
		return nil, hostError("No randomness for the staging directory.")
	}
	name := stagePrefix + hex.EncodeToString(random)
	if err := t.runtime.Mkdir(name, 0o700); err != nil {
		return nil, hostError("The staging directory could not be created.")
	}
	root, err := t.runtime.OpenRoot(name)
	if err != nil {
		t.runtime.RemoveAll(name)
		return nil, hostError("The staging directory could not be opened.")
	}
	return &Stage{tree: t, name: name, root: root}, nil
}

// Close removes only this run's staging directory.
func (s *Stage) Close() {
	s.root.Close()
	s.tree.runtime.RemoveAll(s.name)
}

// Mkdir makes a private directory in the stage.
func (s *Stage) Mkdir(name string) error {
	if err := s.root.Mkdir(name, 0o700); err != nil {
		return hostError("A staging directory could not be created.")
	}
	return nil
}

// Write stages one file with its final owner and mode.
func (s *Stage) Write(name string, data []byte, uid, gid int, mode fs.FileMode) error {
	file, err := s.root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return hostError("A file could not be staged.")
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Chown(uid, gid)
	}
	if err == nil {
		err = file.Chmod(mode)
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return hostError("A file could not be staged.")
	}
	return nil
}

// trusted opens name beneath root if it is a regular file with one name, not
// a link, owned by uid: the only kind of destination root writes into in
// place, or reads a secret back from. Missing is (nil, nil).
func trusted(root *os.Root, name string, uid int) (*os.File, fs.FileInfo, error) {
	listed, err := root.Lstat(name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil, nil
	}
	if err != nil || !listed.Mode().IsRegular() {
		return nil, nil, errUntrusted
	}
	file, err := root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, nil, errUntrusted
	}
	opened, err := file.Stat()
	held, links, ok := owner(opened)
	if err != nil || !os.SameFile(listed, opened) || !opened.Mode().IsRegular() || !ok || held != uid || links != 1 {
		file.Close()
		return nil, nil, errUntrusted
	}
	return file, opened, nil
}

var errUntrusted = errors.New("untrusted file")

// Read returns an installed file's bytes if it is still the file the renderer
// installed: trusted, and with this mode.
func Read(root *os.Root, name string, uid int, mode fs.FileMode) ([]byte, bool) {
	file, info, err := trusted(root, name, uid)
	if err != nil || file == nil {
		return nil, false
	}
	defer file.Close()
	if info.Mode().Perm() != mode {
		return nil, false
	}
	data, err := io.ReadAll(io.LimitReader(file, maxFileBytes+1))
	if err != nil || len(data) > maxFileBytes {
		return nil, false
	}
	return data, true
}

// InPlace installs data at name beneath root, keeping the inode when the file
// exists: replacing it would break a single-file bind mount, and the running
// container would keep the old file indefinitely. The cost is that this is not
// an atomic swap. Anything at the name that is not a file of uid's own (a link
// to a system file, a hard link, a directory) is refused rather than written
// through, followed or re-owned.
func InPlace(root *os.Root, name string, data []byte, uid, gid int, mode fs.FileMode) (bool, error) {
	current, info, err := trusted(root, name, uid)
	if err != nil {
		return false, hostError("Refusing to write ", name, ": it is not a file only uid ", itoa(uid), " owns.")
	}
	if current == nil {
		// A fresh destination: written beside it, then renamed into place.
		random := make([]byte, 8)
		if _, err := rand.Read(random); err != nil {
			return false, hostError("No randomness for a temporary name.")
		}
		temporary := "." + name + "." + hex.EncodeToString(random)
		file, err := root.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
		if err != nil {
			return false, hostError("Could not create ", name, ".")
		}
		_, err = file.Write(data)
		if err == nil {
			err = file.Chown(uid, gid)
		}
		if err == nil {
			err = file.Chmod(mode)
		}
		if closeErr := file.Close(); err == nil {
			err = closeErr
		}
		if err == nil {
			err = root.Rename(temporary, name)
		}
		if err != nil {
			root.Remove(temporary)
			return false, hostError("Could not create ", name, ".")
		}
		return true, nil
	}
	defer current.Close()
	existing, err := io.ReadAll(io.LimitReader(current, maxFileBytes+1))
	if err != nil {
		return false, hostError("Could not read ", name, ".")
	}
	if bytes.Equal(existing, data) {
		if current.Chown(uid, gid) != nil || current.Chmod(mode) != nil {
			return false, hostError("Could not set the owner and mode of ", name, ".")
		}
		return false, nil
	}
	// Writable by its owner for the write; an unprivileged run needs it, root does not.
	if info.Mode().Perm()&0o200 == 0 {
		if err := current.Chmod(0o600); err != nil {
			return false, hostError("Could not open ", name, " for writing.")
		}
	}
	writer, err := root.OpenFile(name, os.O_WRONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return false, hostError("Could not open ", name, " for writing.")
	}
	defer writer.Close()
	// The same inode that was checked, not whatever is at the name now.
	if opened, err := writer.Stat(); err != nil || !os.SameFile(info, opened) {
		return false, hostError("Refusing to write ", name, ": it changed while it was being checked.")
	}
	if err = writer.Truncate(0); err == nil {
		_, err = writer.Write(data)
	}
	if err == nil {
		err = writer.Chown(uid, gid)
	}
	if err == nil {
		err = writer.Chmod(mode)
	}
	if err == nil {
		err = writer.Sync()
	}
	if err != nil {
		return false, hostError("Could not write ", name, ".")
	}
	return true, nil
}

// Secret, Runtime and Web are the opened directories.
func (t *Tree) Secret() *os.Root  { return t.secret }
func (t *Tree) Runtime() *os.Root { return t.runtime }
func (t *Tree) Web() *os.Root     { return t.web }

// RemoveMCPToken removes the token file earlier releases kept: nothing
// accepts one, so none is kept.
func (t *Tree) RemoveMCPToken() error {
	if err := t.secret.Remove(mcpTokenName); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return hostError("The retired MCP token file could not be removed.")
	}
	return nil
}

// HasCheckoutEnv reports whether anything sits at the checkout's copy of the
// application environment, a link included: it is kept current only while a
// container binds it, and the deploy that replaces that container removes it.
func (t *Tree) HasCheckoutEnv() bool {
	_, err := t.secret.Lstat(AppEnvName)
	return err == nil
}

// Rename moves a staged file over name in the runtime directory when the
// installed one differs: on one tmpfs, so each reader sees a complete old or
// new file. same compares an installed file to the staged one; nil compares bytes.
func (s *Stage) Rename(staged, name string, staging []byte, mode fs.FileMode, same func([]byte) bool) (bool, error) {
	runtime := s.tree.runtime
	if installed, ok := Read(runtime, name, s.tree.Layout.RootUID, mode); ok {
		if (same != nil && same(installed)) || (same == nil && bytes.Equal(installed, staging)) {
			return false, nil
		}
	}
	// A directory at the name cannot be renamed over; nothing else belongs there.
	if listed, err := runtime.Lstat(name); err == nil && listed.IsDir() {
		if err := runtime.RemoveAll(name); err != nil {
			return false, hostError("Could not replace ", name, ".")
		}
	}
	if err := runtime.Rename(s.name+"/"+staged, name); err != nil {
		return false, hostError("Could not install ", name, ".")
	}
	return true, nil
}

// PruneSSH drops every entry of the identity directory that is not wanted:
// a connection that no longer exists takes its identity with it.
func (t *Tree) PruneSSH(wanted map[string]bool) (bool, error) {
	directory, err := t.runtime.Open(SSHDirName)
	if err != nil {
		return false, hostError("The identity directory could not be read.")
	}
	names, err := directory.Readdirnames(-1)
	directory.Close()
	if err != nil {
		return false, hostError("The identity directory could not be read.")
	}
	changed := false
	for _, name := range names {
		if wanted[name] {
			continue
		}
		if err := t.runtime.RemoveAll(SSHDirName + "/" + name); err != nil {
			return changed, hostError("A retired identity could not be removed.")
		}
		changed = true
	}
	return changed, nil
}

// EnsureSSHDir makes the identity directory, refusing a link in its place.
func (t *Tree) EnsureSSHDir() error {
	if err := t.runtime.Mkdir(SSHDirName, 0o700); err != nil && !errors.Is(err, fs.ErrExist) {
		return hostError("The identity directory could not be created.")
	}
	listed, err := t.runtime.Lstat(SSHDirName)
	if err != nil || !listed.IsDir() {
		return hostError("The identity directory must be a directory, not a link.")
	}
	return nil
}

// WriteAtomic replaces name in the runtime directory with data in one rename.
func (t *Tree) WriteAtomic(name string, data []byte, mode fs.FileMode) error {
	temporary := name + ".tmp"
	t.runtime.Remove(temporary)
	file, err := t.runtime.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return hostError("Could not write ", name, ".")
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Chmod(mode)
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = t.runtime.Rename(temporary, name)
	}
	if err != nil {
		t.runtime.Remove(temporary)
		return hostError("Could not write ", name, ".")
	}
	return nil
}
