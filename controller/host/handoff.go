package host

import (
	"encoding/json"
	jsonv2 "encoding/json/v2"
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

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/internal/secret"
)

// The controller's one writable place: a directory of each run's, the web
// account's alone, mounted where the variable names it. It carries a request
// to root, never authority: root reads it as untrusted input.
const (
	controllerOut    = "/run/severino-hq/out"
	controllerOutEnv = "HQ_CONTROLLER_OUT"
	// handoffLimit is far more than any real handoff: a token and a list of
	// repository names per owner.
	handoffLimit = 64 << 10
	// TokenLife is how long an installation token lives. A handoff whose
	// token outlives it by more than clock skew was not minted for this run.
	TokenLife = time.Hour
	// TokenRefresh is the age at which a placed token is minted again: half
	// its life, so a pass that fails still leaves the next one time to.
	TokenRefresh = TokenLife / 2
	clockSkew    = 2 * time.Minute
	// grantsTarget is where a pass finds the placed grants due this pass, and
	// grantsEnv the variable that names it.
	grantsTarget = hostcore.RunDir + "/grants.json"
	grantsEnv    = "HQ_CONTROLLER_GRANTS"
	// SpoolName is the handoff in root's spool, which the path unit watches.
	SpoolName  = "handoff.json"
	handoffV1  = 1
	ownerLimit = 39
	// branchLimit is far more than the branches one Open PR names.
	branchLimit = 50
)

// Handoff is a dependency run the controller asks root for: per owner, the
// repositories and the token minted for exactly them.
type Handoff struct {
	Version int          `json:"version"`
	Runs    []HandoffRun `json:"runs"`
}

// HandoffRun is one owner's share of a Handoff. Branches are the branches
// Renovate is to open, as if their boxes on the Dependency Dashboard were
// ticked (its checkedBranches): a run that names any names one repository,
// since Renovate applies them to every repository it is given.
type HandoffRun struct {
	Owner        string       `json:"owner"`
	Repositories []string     `json:"repositories"`
	Branches     []string     `json:"branches"`
	Token        secret.Value `json:"token"`
	ExpiresAt    time.Time    `json:"expires_at"`
}

var (
	// GitHub's rule for a user or organization: letters, digits and single
	// hyphens, neither leading nor trailing.
	githubOwner = regexp.MustCompile(`^[A-Za-z0-9](?:-?[A-Za-z0-9])*$`)
	githubName  = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
	// A token is one line of printable characters: it is written into an
	// environment file, where a newline would start another variable.
	tokenShape = regexp.MustCompile(`^[\x21-\x7e]{1,512}$`)
	// A branch Renovate names, as HQ records one; never with "..". It is
	// written into the environment file as one JSON line.
	branchShape = api.MustPattern("GitHubDependencyRunBranch", "properties", "branch")
)

// ParseHandoff is data as a Handoff when it has exactly the shape the
// contract names: no key it does not name, each spelled as named and given
// once, every repository under its own run's owner, each owner once. A key
// left out decodes to its zero value, which every check below refuses.
func ParseHandoff(data []byte) (Handoff, error) {
	var h Handoff
	err := jsonv2.Unmarshal(data, &h, jsonv2.RejectUnknownMembers(true))
	// Learned before anything is checked, so a refused run's token is struck
	// too, a run decoded before the decoder stopped included.
	for _, run := range h.Runs {
		mask.Learn(run.Token.Reveal())
	}
	if err != nil {
		// The decoder's error quotes the sender's text, which root's log
		// never repeats.
		return Handoff{}, errors.New("the handoff does not have the shape the contract names")
	}
	if h.Version != handoffV1 {
		return Handoff{}, errors.New("the handoff is not version 1")
	}
	if len(h.Runs) == 0 {
		return Handoff{}, errors.New("the handoff names no runs")
	}
	owners := map[string]bool{}
	for i, run := range h.Runs {
		if err := checkRun(run); err != nil {
			return Handoff{}, fmt.Errorf("run %d: %w", i, err)
		}
		if owners[strings.ToLower(run.Owner)] {
			return Handoff{}, fmt.Errorf("run %d: its owner has a run already", i)
		}
		owners[strings.ToLower(run.Owner)] = true
	}
	return h, nil
}

func checkRun(run HandoffRun) error {
	if run.ExpiresAt.IsZero() {
		return errors.New("it names no expiry")
	}
	if len(run.Owner) > ownerLimit || !githubOwner.MatchString(run.Owner) {
		return errors.New("the owner is not a GitHub account name")
	}
	if len(run.Repositories) == 0 {
		return errors.New("it names no repositories")
	}
	for _, repository := range run.Repositories {
		owner, name, _ := strings.Cut(repository, "/")
		if owner != run.Owner || !githubName.MatchString(name) || name == "." || name == ".." {
			return errors.New("a repository is not one of its owner's")
		}
	}
	if sorted := slices.Sorted(slices.Values(run.Repositories)); len(slices.Compact(sorted)) != len(run.Repositories) {
		return errors.New("a repository is named twice")
	}
	if len(run.Branches) > 0 && len(run.Repositories) != 1 {
		return errors.New("a run that names branches names one repository")
	}
	if len(run.Branches) > branchLimit {
		return errors.New("it names more branches than one run opens")
	}
	for _, branch := range run.Branches {
		if !branchShape.MatchString(branch) || strings.Contains(branch, "..") {
			return errors.New("a branch is not a branch Renovate names")
		}
	}
	if !run.Token.Holds(tokenShape.MatchString) {
		return errors.New("its token is empty or not one printable line")
	}
	return nil
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
	if _, links, ok := hostcore.Ownership(info); !ok || links != 1 {
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

// takeHandoffs delivers each valid hand-off the pass left to where its
// grant says, and removes everything the pass left there, whatever it was.
// A file that is no declared grant's hand-off, or a hand-off its grant does
// not admit, is said so and never delivered.
func (c *Controller) takeHandoffs(run string) {
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
	entries, _ := fs.ReadDir(out.FS(), ".")
	for _, entry := range entries {
		name := entry.Name()
		grant, declared := c.grantFor(name)
		err := errors.New("it is no declared grant's hand-off")
		if declared {
			err = c.deliver(out, grant)
		}
		_ = out.RemoveAll(name)
		if err != nil {
			fmt.Fprintf(c.Stderr, "The hand-off %q was refused and dropped: %v\n", name, err)
		}
	}
}

// grantFor is the grant whose hand-off file is name.
func (c *Controller) grantFor(name string) (Grant, bool) {
	for _, grant := range c.Grants {
		if grant.HandoffName() == name {
			return grant, true
		}
	}
	return Grant{}, false
}

// deliver reads grant's hand-off from out and, when it is one the grant
// admits, puts it where the grant says.
func (c *Controller) deliver(out *os.Root, grant Grant) error {
	data, err := readUntrusted(out, grant.HandoffName(), handoffLimit)
	if err != nil {
		return err
	}
	handoff, err := ParseHandoff(data)
	if err != nil {
		return err
	}
	if err := handoff.Fresh(c.Now()); err != nil {
		return err
	}
	if err := grant.admits(handoff); err != nil {
		return err
	}
	if grant.Planned {
		return spool(grant.Path, handoff)
	}
	return place(grant.Path, handoff.Runs[0].Token)
}

// place writes token alone to path, root's to read and nobody's to write, all
// at once: a reader never sees half a token.
func place(path string, token secret.Value) error {
	if err := ownDirectory(filepath.Dir(path)); err != nil {
		return err
	}
	return atomicfile.WriteFile(path, []byte(token.Reveal()), 0o400)
}

// spool writes handoff, as root re-encodes it, where only root can read it:
// the file root's run reads each token from.
func spool(dir string, handoff Handoff) error {
	data, err := jsonv2.Marshal(handoff, json.DefaultOptionsV1(), secret.Revealing)
	if err != nil {
		return err
	}
	if err := ownDirectory(dir); err != nil {
		return err
	}
	return atomicfile.WriteFile(filepath.Join(dir, SpoolName), data, 0o600)
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
