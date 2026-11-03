package host

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/joeseverino/severino-hq/controller/internal/sealing"
)

// prepareSeal makes sure the controller has a sealing key and the web its
// public half: on every pass that is not a glance, and in a release before
// the web restarts, so no start of HQ comes before its key. Install's
// preflight is a pass, and comes before it enables the web. Its security
// properties:
//   - A seed is written once, when the directory holds none, and never
//     overwritten or removed: every value sealed to it depends on it.
//   - The directory and seed are the controller's account's, and only a
//     pass's container mounts them, read-only, so the web, which runs as the
//     same account, never reaches them, and a pass cannot replace its key.
//   - The public key is derived from the seed and written by root, on
//     every pass where it differs, so it is always the seed's.
//
// Run as often as a pass is, it changes nothing once both are in place.
func (m *Machine) prepareSeal() error {
	if err := os.MkdirAll(m.Seal, 0o700); err != nil {
		return err
	}
	seeds, err := filepath.Glob(filepath.Join(m.Seal, "*.seed"))
	if err != nil {
		return err
	}
	var key sealing.Key
	switch len(seeds) {
	case 0:
		if key, err = m.newSeed(); err != nil {
			return err
		}
	case 1:
		data, err := os.ReadFile(seeds[0])
		if err != nil {
			return err
		}
		if key, err = sealing.NewKey(data); err != nil {
			return fmt.Errorf("the sealing key %s: %w", seeds[0], err)
		}
	default:
		return fmt.Errorf("%s holds %d sealing keys, and which one the web seals to is not recorded", m.Seal, len(seeds))
	}
	if err := m.ownTree(m.Seal); err != nil {
		return err
	}
	return m.publishSealKey(key)
}

// newSeed writes a new seed beside where it belongs and links it into place,
// which fails rather than replaces when a seed is already there.
func (m *Machine) newSeed() (sealing.Key, error) {
	seed := make([]byte, sealing.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return sealing.Key{}, err
	}
	key, err := sealing.NewKey(seed)
	if err != nil {
		return sealing.Key{}, err
	}
	file, err := os.CreateTemp(m.Seal, ".seed.")
	if err != nil {
		return sealing.Key{}, err
	}
	defer os.Remove(file.Name())
	_, err = file.Write(seed)
	if err == nil {
		err = file.Chmod(0o400)
	}
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Link(file.Name(), filepath.Join(m.Seal, key.ID+".seed"))
	}
	if err != nil {
		return sealing.Key{}, fmt.Errorf("the sealing key could not be written: %w", err)
	}
	fmt.Fprintln(m.Out, "A sealing key was made for the controller.")
	return key, nil
}

// publishSealKey writes key's public half where the web unit binds it from,
// unless it is there already.
func (m *Machine) publishSealKey(key sealing.Key) error {
	want := []byte(base64.StdEncoding.EncodeToString(key.PublicKey()) + "\n")
	if have, err := os.ReadFile(m.SealPublic); err == nil && bytes.Equal(have, want) {
		return nil
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(m.SealPublic), 0o755); err != nil {
		return err
	}
	return writeAtomic(m.SealPublic, want, 0o644)
}
