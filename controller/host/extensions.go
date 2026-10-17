package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Where a composed image keeps the extensions it carries and the ones its
// composition retired on purpose (composition/Dockerfile). Both are inside the
// image, so the signature over its digest covers them.
const (
	ExtensionLock    = "/opt/severino/plugin-lock.json"
	RetiredExtension = "/opt/severino/plugin-retired.json"
)

// keepsExtensions refuses an image that would drop an extension the running
// release carries, unless its composition declares that extension retired:
// an extension leaves production by being named, never by being left out.
//
// running is the running release's set, nil when nothing is recorded. The
// public log gets counts only: an extension's id is private, so the ids go
// to the root-only log.
func (d *Deployer) keepsExtensions(ctx context.Context, stage, image string, running []string) error {
	if running == nil {
		return nil
	}
	next, err := d.extensionsOf(ctx, stage, image, ExtensionLock, lockedIDs)
	if err != nil {
		return fmt.Errorf("refusing to deploy %s: could not read the extensions it carries: %w", image, err)
	}
	dropped := missing(running, next)
	if len(dropped) == 0 {
		fmt.Fprintf(d.Out, "Extensions kept: %d.\n", len(running))
		return nil
	}
	// Read only when something is dropped. A declaration that is absent or
	// unreadable declares nothing, so the drop is refused rather than allowed.
	retired, _ := d.extensionsOf(ctx, stage, image, RetiredExtension, retiredIDs)
	undeclared := missing(dropped, retired)
	if len(undeclared) == 0 {
		fmt.Fprintf(d.Out, "Extensions retired by the composition: %d.\n", len(dropped))
		return nil
	}
	d.keepPrivate("extensions", "Dropped without being declared retired:\n"+strings.Join(undeclared, "\n")+"\n")
	return fmt.Errorf("refusing to deploy %s: it drops %d extension(s) the running release carries "+
		"that its composition does not declare retired", image, len(undeclared))
}

// runningExtensions is the set the recorded release carries, nil when no
// release is recorded. Read before anything is pruned, and pulled by its
// digest when the host no longer has it: the digest is what was verified when
// it was deployed, so the bytes are the same either way.
func (d *Deployer) runningExtensions(ctx context.Context, stage string) ([]string, error) {
	data, err := os.ReadFile(d.ReleasePath())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	current, err := Pinned(strings.TrimSpace(string(data)))
	if err != nil {
		return nil, err
	}
	ids, err := d.extensionsOf(ctx, stage, current, ExtensionLock, lockedIDs)
	if err == nil {
		return ids, nil
	}
	if err := d.private(ctx, "Running release pull", "pull-running", "docker", "pull", current); err != nil {
		return nil, fmt.Errorf("could not read the extensions the running release carries: %w", err)
	}
	ids, err = d.extensionsOf(ctx, stage, current, ExtensionLock, lockedIDs)
	if err != nil {
		return nil, fmt.Errorf("could not read the extensions the running release carries: %w", err)
	}
	return ids, nil
}

// extensionsOf copies one file out of image into the stage and parses it.
func (d *Deployer) extensionsOf(ctx context.Context, stage, image, from string, parse func([]byte) ([]string, error)) ([]string, error) {
	copied, err := os.MkdirTemp(stage, "extensions.")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(copied)
	to := filepath.Join(copied, filepath.Base(from))
	if err := d.copyOut(ctx, image, from, to); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(to)
	if err != nil {
		return nil, err
	}
	return parse(data)
}

// lockedIDs is the plugin ids in Cordon's merged lock: the set the runtime
// refuses to start without, so the set the release runs.
func lockedIDs(data []byte) ([]string, error) {
	var lock struct {
		Plugins *[]struct {
			Plugin string `json:"plugin"`
		} `json:"plugins"`
	}
	if err := json.Unmarshal(data, &lock); err != nil || lock.Plugins == nil {
		return nil, errors.New("the extension lock is not a lock")
	}
	ids := make([]string, 0, len(*lock.Plugins))
	for _, entry := range *lock.Plugins {
		ids = append(ids, entry.Plugin)
	}
	return checkedIDs(ids)
}

// retiredIDs is the composition's list of extensions it removed on purpose.
func retiredIDs(data []byte) ([]string, error) {
	var ids []string
	if err := json.Unmarshal(data, &ids); err != nil || ids == nil {
		return nil, errors.New("the retired extensions are not a list")
	}
	return checkedIDs(ids)
}

func checkedIDs(ids []string) ([]string, error) {
	for _, id := range ids {
		if id == "" {
			return nil, errors.New("an extension has no id")
		}
	}
	slices.Sort(ids)
	return slices.Compact(ids), nil
}

// missing is what of want is not in have.
func missing(want, have []string) []string {
	var out []string
	for _, id := range want {
		if !slices.Contains(have, id) {
			out = append(out, id)
		}
	}
	return out
}

// keepPrivate writes text to LogDir/name.log, root's alone. A failure to
// write it is said publicly and does not change the outcome it explains.
func (d *Deployer) keepPrivate(name, text string) {
	err := os.MkdirAll(d.LogDir, 0o700)
	if err == nil {
		err = os.WriteFile(filepath.Join(d.LogDir, name+".log"), []byte(text), 0o600)
	}
	if err != nil {
		fmt.Fprintf(d.Out, "Could not keep the %s log: %v\n", name, err)
		return
	}
	fmt.Fprintf(d.Out, "The private host log %s.log names them.\n", name)
}
