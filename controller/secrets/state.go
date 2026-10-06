package secrets

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"io/fs"
	"log/slog"
	"os"
	"sort"
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets/install"
	"github.com/joeseverino/severino-hq/controller/secretstatus"
)

const stateName = ".render-state.json"
const stateSchemaVersion = 1

// state is what the last full render installed, kept root-only on the same
// tmpfs as the files it describes: enough to tell, without reading the vault
// item by item, that the vault has not changed and every installed file is
// still the one that render installed. It is gone after a reboot, so the first
// run of every boot is a full render.
type state struct {
	SchemaVersion    int       `json:"schema_version"`
	RenderedAt       time.Time `json:"rendered_at"`
	VaultID          string    `json:"vault_id"`
	ContentVersion   int       `json:"content_version"`
	AttributeVersion *int      `json:"attribute_version,omitzero"`
	// Salt keys every digest below: they cover secrets, so none is a bare hash.
	Salt string `json:"salt"`
	// Inputs covers the registry, the configuration and the version of the
	// connections document this renderer writes.
	Inputs string              `json:"inputs"`
	Files  []installed         `json:"files"`
	Counts secretstatus.Counts `json:"counts"`
}

// installed is one file as the render left it.
type installed struct {
	// Dir is "web", "checkout" or "runtime".
	Dir    string      `json:"dir"`
	Name   string      `json:"name"`
	UID    int         `json:"uid"`
	Mode   fs.FileMode `json:"mode"`
	Digest string      `json:"digest"`
}

func newSalt() (string, error) {
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return "", err
	}
	return hex.EncodeToString(random), nil
}

func digest(salt string, parts ...[]byte) string {
	hash := sha256.New()
	hash.Write([]byte(salt))
	for _, part := range parts {
		// Length-prefixed, so two inputs cannot be re-split into two others.
		hash.Write([]byte{byte(len(part) >> 24), byte(len(part) >> 16), byte(len(part) >> 8), byte(len(part))})
		hash.Write(part)
	}
	return hex.EncodeToString(hash.Sum(nil))
}

func (r *Runner) root(tree *install.Tree, dir string) *os.Root {
	switch dir {
	case "web":
		return tree.Web()
	case "checkout":
		return tree.Secret()
	case "runtime":
		return tree.Runtime()
	}
	return nil
}

// record reads one installed file back and notes its digest. A file that
// cannot be read back as the renderer's own was not installed.
func (r *Runner) record(tree *install.Tree, salt, dir, name string, uid int, mode fs.FileMode) (installed, bool) {
	data, ok := install.Read(r.root(tree, dir), name, uid, mode)
	if !ok {
		return installed{}, false
	}
	return installed{Dir: dir, Name: name, UID: uid, Mode: mode, Digest: digest(salt, data)}, true
}

func (r *Runner) loadState(tree *install.Tree) (state, bool) {
	data, ok := install.Read(tree.Runtime(), stateName, r.Config.Layout.RootUID, 0o600)
	if !ok {
		return state{}, false
	}
	var found state
	if err := jsonv2.Unmarshal(data, &found, jsonv2.RejectUnknownMembers(true)); err != nil || found.SchemaVersion != stateSchemaVersion {
		return state{}, false
	}
	return found, true
}

func (r *Runner) saveState(tree *install.Tree, found state) error {
	data, err := json.Marshal(found)
	if err != nil {
		return err
	}
	return tree.WriteAtomic(stateName, data, 0o600)
}

// intact reports whether every file the last render installed is still that
// file, and nothing has been added to or dropped from the identity directory.
func (r *Runner) intact(tree *install.Tree, last state) bool {
	identities := []string{}
	checkout := false
	for _, file := range last.Files {
		root := r.root(tree, file.Dir)
		if root == nil {
			return false
		}
		data, ok := install.Read(root, file.Name, file.UID, file.Mode)
		if !ok || digest(last.Salt, data) != file.Digest {
			return false
		}
		if file.Dir == "runtime" && len(file.Name) > len(install.SSHDirName)+1 && file.Name[:len(install.SSHDirName)+1] == install.SSHDirName+"/" {
			identities = append(identities, file.Name[len(install.SSHDirName)+1:])
		}
		if file.Dir == "checkout" {
			checkout = true
		}
	}
	if tree.HasCheckoutEnv() != checkout {
		return false
	}
	directory, err := tree.Runtime().Open(install.SSHDirName)
	if err != nil {
		return false
	}
	present, err := directory.Readdirnames(-1)
	directory.Close()
	if err != nil || len(present) != len(identities) {
		return false
	}
	sort.Strings(present)
	sort.Strings(identities)
	for index := range present {
		if present[index] != identities[index] {
			return false
		}
	}
	return true
}

// pendingName marks a web container that has not loaded the application
// environment on the tmpfs.
const pendingName = "web-restart-pending.json"

// pendingMark names the environment the restart is owed for, by salted digest:
// a restart onto a file that is not that one (cut short, or never written) is
// worse than no restart.
type pendingMark struct {
	Salt   string `json:"salt"`
	Digest string `json:"digest"`
}

// pending reads the mark: whether one exists, and whether it could be read.
func (r *Runner) pending(tree *install.Tree) (pendingMark, bool, bool) {
	if _, err := tree.Runtime().Lstat(pendingName); err != nil {
		return pendingMark{}, false, false
	}
	data, ok := install.Read(tree.Runtime(), pendingName, r.Config.Layout.RootUID, 0o600)
	var mark pendingMark
	if !ok || jsonv2.Unmarshal(data, &mark, jsonv2.RejectUnknownMembers(true)) != nil || mark.Salt == "" || mark.Digest == "" {
		return pendingMark{}, true, false
	}
	return mark, true, true
}

// pendingUnreadable reports a mark that exists but names no environment: only
// a full render can rewrite it, so the run is not skipped.
func (r *Runner) pendingUnreadable(tree *install.Tree) bool {
	_, exists, readable := r.pending(tree)
	return exists && !readable
}

func (r *Runner) markPending(tree *install.Tree, environment []byte) error {
	salt, err := newSalt()
	if err != nil {
		return errors.New("no randomness for the restart mark")
	}
	data, err := json.Marshal(pendingMark{Salt: salt, Digest: digest(salt, environment)})
	if err != nil {
		return err
	}
	return tree.WriteAtomic(pendingName, data, 0o600)
}

func (r *Runner) clearPending(tree *install.Tree) error {
	if err := tree.Runtime().Remove(pendingName); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return &installFailure{}
	}
	return nil
}

// settle pays an owed restart. It restarts only onto the environment the mark
// names, and clears the mark only once the container is healthy on it, or
// when there is no container to restart: one created later reads the file.
func (r *Runner) settle(ctx context.Context, tree *install.Tree, result *Result) error {
	mark, exists, readable := r.pending(tree)
	if !exists || !readable || ctx.Err() != nil {
		return nil
	}
	layout := r.Config.Layout
	installed, ok := install.Read(tree.Web(), install.AppEnvName, layout.WebUID, 0o400)
	if !ok || digest(mark.Salt, installed) != mark.Digest {
		return nil
	}
	if r.Web.Installed(ctx) {
		result.Restarted = true
		if err := r.restart(ctx); err != nil {
			return err
		}
		r.Log.Info("Severino HQ restarted on its new environment.", slog.String("event", "secrets.web.restarted"))
	}
	if ctx.Err() != nil {
		return nil
	}
	return r.clearPending(tree)
}
