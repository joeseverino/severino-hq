package host

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
)

// The controller's one writable place: a directory of each run's, the web
// account's alone, mounted where the variable names it. It carries a request
// to root, never authority: root reads it as untrusted input.
const (
	controllerOut    = "/run/severino-hq/out"
	controllerOutEnv = "HQ_CONTROLLER_OUT"
	// DependencyHandoff is the file a pass leaves in its out directory when
	// a dependency run is due: one token per owner it minted.
	DependencyHandoff = "dependency-updates.json"
	// handoffLimit is far more than any real handoff: a token and a list of
	// repository names per owner.
	handoffLimit = 64 << 10
	// TokenLife is how long an installation token lives. A handoff whose
	// token outlives it by more than clock skew was not minted for this run.
	TokenLife = time.Hour
	clockSkew = 2 * time.Minute
	// SpoolName is the handoff in root's spool, which the path unit watches.
	SpoolName  = "handoff.json"
	handoffV1  = 1
	ownerLimit = 39
)

// Handoff is a dependency run the controller asks root for: per owner, the
// repositories and the token minted for exactly them.
type Handoff struct {
	Version int          `json:"version"`
	Runs    []HandoffRun `json:"runs"`
}

// HandoffRun is one owner's share of a Handoff.
type HandoffRun struct {
	Owner        string    `json:"owner"`
	Repositories []string  `json:"repositories"`
	Token        string    `json:"token"`
	ExpiresAt    time.Time `json:"expires_at"`
}

var (
	// GitHub's rule for a user or organization: letters, digits and single
	// hyphens, neither leading nor trailing.
	githubOwner = regexp.MustCompile(`^[A-Za-z0-9](?:-?[A-Za-z0-9])*$`)
	githubName  = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
	// A token is one line of printable characters: it is written into an
	// environment file, where a newline would start another variable.
	tokenShape = regexp.MustCompile(`^[\x21-\x7e]{1,512}$`)
)

// ParseHandoff is data as a Handoff when it has exactly the shape the
// contract names: every key it names and no other, spelled as named, every
// repository under its own run's owner, each owner once.
func ParseHandoff(data []byte) (Handoff, error) {
	var top map[string]json.RawMessage
	if err := strictJSON(data, &top); err != nil {
		return Handoff{}, err
	}
	if err := exactKeys(top, "version", "runs"); err != nil {
		return Handoff{}, err
	}
	var h Handoff
	if err := strictJSON(top["version"], &h.Version); err != nil || h.Version != handoffV1 {
		return Handoff{}, errors.New("the handoff is not version 1")
	}
	var runs []map[string]json.RawMessage
	if err := strictJSON(top["runs"], &runs); err != nil || len(runs) == 0 {
		return Handoff{}, errors.New("the handoff names no runs")
	}
	owners := map[string]bool{}
	for i, raw := range runs {
		run, err := parseRun(raw)
		if err != nil {
			return Handoff{}, fmt.Errorf("run %d: %w", i, err)
		}
		if owners[strings.ToLower(run.Owner)] {
			return Handoff{}, fmt.Errorf("run %d: its owner has a run already", i)
		}
		owners[strings.ToLower(run.Owner)] = true
		h.Runs = append(h.Runs, run)
	}
	return h, nil
}

func parseRun(raw map[string]json.RawMessage) (HandoffRun, error) {
	var run HandoffRun
	if err := exactKeys(raw, "owner", "repositories", "token", "expires_at"); err != nil {
		return run, err
	}
	for key, target := range map[string]any{"owner": &run.Owner, "repositories": &run.Repositories, "token": &run.Token, "expires_at": &run.ExpiresAt} {
		if err := strictJSON(raw[key], target); err != nil {
			return run, fmt.Errorf("%s is not what the contract names", key)
		}
	}
	if len(run.Owner) > ownerLimit || !githubOwner.MatchString(run.Owner) {
		return run, errors.New("the owner is not a GitHub account name")
	}
	if len(run.Repositories) == 0 {
		return run, errors.New("it names no repositories")
	}
	for _, repository := range run.Repositories {
		owner, name, _ := strings.Cut(repository, "/")
		if owner != run.Owner || !githubName.MatchString(name) || name == "." || name == ".." {
			return run, errors.New("a repository is not one of its owner's")
		}
	}
	if sorted := slices.Sorted(slices.Values(run.Repositories)); len(slices.Compact(sorted)) != len(run.Repositories) {
		return run, errors.New("a repository is named twice")
	}
	if !tokenShape.MatchString(run.Token) {
		return run, errors.New("its token is empty or not one printable line")
	}
	return run, nil
}

// Fresh is whether every token is still alive and was minted for this run:
// it expires after now and within one token's life of it.
func (h Handoff) Fresh(now time.Time) error {
	for i, run := range h.Runs {
		if !run.ExpiresAt.After(now) || run.ExpiresAt.After(now.Add(TokenLife+clockSkew)) {
			return fmt.Errorf("run %d: its token does not expire within the next hour", i)
		}
	}
	return nil
}

// strictJSON decodes exactly one value, and nothing after it.
func strictJSON(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("trailing data after the value")
	}
	return nil
}

// exactKeys holds an object to exactly the keys named, as spelled: the
// decoder alone would take any casing of a known key and ignore a missing one.
func exactKeys(object map[string]json.RawMessage, keys ...string) error {
	got := make([]string, 0, len(object))
	for key := range object {
		got = append(got, key)
	}
	slices.Sort(got)
	want := slices.Sorted(slices.Values(keys))
	if !slices.Equal(got, want) {
		return fmt.Errorf("keys %v, not %v", got, want)
	}
	return nil
}

// readUntrusted is name beneath root, when it is a regular file with one name
// and at most limit bytes. A link is not followed, and a file swapped between
// the look and the open is refused.
func readUntrusted(root *os.Root, name string, limit int64) ([]byte, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("not a regular file")
	}
	if _, links, ok := Ownership(info); !ok || links != 1 {
		return nil, errors.New("a file with another name")
	}
	if info.Size() > limit {
		return nil, errors.New("larger than a handoff can be")
	}
	file, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return nil, errors.New("replaced while it was read")
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("larger than a handoff can be")
	}
	return data, nil
}

// prepareOut makes the run's out directory, the controller's alone.
func (c *Controller) prepareOut(run string) error {
	out := filepath.Join(run, "out")
	if err := os.Mkdir(out, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(out, 0o700); err != nil {
		return err
	}
	root, err := os.OpenRoot(run)
	if err != nil {
		return err
	}
	defer root.Close()
	return c.Owner(root, "out")
}

// takeHandoff moves a valid handoff the pass left into root's spool, where
// its unit starts the dependency run. The original is removed whatever it
// was; one that is not valid is said so and never run.
func (c *Controller) takeHandoff(run string) {
	runRoot, err := os.OpenRoot(run)
	if err != nil {
		return
	}
	defer runRoot.Close()
	out, err := runRoot.OpenRoot("out")
	if err != nil {
		return
	}
	defer out.Close()
	defer func() { _ = out.RemoveAll(DependencyHandoff) }()

	data, err := readUntrusted(out, DependencyHandoff, handoffLimit)
	if errors.Is(err, fs.ErrNotExist) {
		return
	}
	var handoff Handoff
	if err == nil {
		handoff, err = ParseHandoff(data)
	}
	if err == nil {
		err = handoff.Fresh(c.Now())
	}
	if err == nil {
		err = spool(c.Spool, handoff)
	}
	if err != nil {
		fmt.Fprintf(c.Stderr, "The dependency run handoff was refused and dropped: %v\n", err)
	}
}

// spool writes handoff, as root re-encodes it, where only root can read it.
func spool(dir string, handoff Handoff) error {
	data, err := json.Marshal(handoff)
	if err != nil {
		return err
	}
	if err := ownDirectory(dir); err != nil {
		return err
	}
	return writeAtomic(filepath.Join(dir, SpoolName), data, 0o600)
}

// ownDirectory makes dir a real directory only its owner can enter.
func ownDirectory(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("%s is not a directory", dir)
	}
	return os.Chmod(dir, 0o700)
}
