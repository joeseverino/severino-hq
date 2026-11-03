package runtime

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/sealing"
)

// sealSeedSuffix names a seed in the sealing directory: <key id>.seed, as
// hq-host writes it.
const sealSeedSuffix = ".seed"

// Opener opens what HQ's web sealed to this controller, with every key the
// launcher lent the run (Environment.SealKeyDir), each by its id. The web
// holds only the public half, so this is the one process that can read a
// sealed value.
type Opener struct {
	keys map[string]sealing.Key
}

// NewOpener reads every seed in dir. A run lent no directory opens nothing:
// each Open says no key holds it.
func NewOpener(dir string) (*Opener, error) {
	opener := &Opener{keys: map[string]sealing.Key{}}
	if dir == "" {
		return opener, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("the sealing keys could not be read: %w", err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if !entry.Type().IsRegular() || !strings.HasSuffix(name, sealSeedSuffix) {
			continue
		}
		seed, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return nil, fmt.Errorf("the sealing key %s could not be read: %w", name, err)
		}
		key, err := sealing.NewKey(seed)
		if err != nil {
			return nil, fmt.Errorf("the sealing key %s: %w", name, err)
		}
		// The name is how a person finds a key, so one that names another
		// key's id is refused rather than trusted or quietly ignored.
		if name != key.ID+sealSeedSuffix {
			return nil, fmt.Errorf("the sealing key %s is the key %s", name, key.ID)
		}
		opener.keys[key.ID] = key
	}
	return opener, nil
}

// Open is the plaintext of a sealed value, which opens only for the resource
// and slot it was sealed for.
func (o *Opener) Open(sealed, resourceKey, slot string) ([]byte, error) {
	id, ciphertext, err := sealing.Header(sealed)
	if err != nil {
		return nil, err
	}
	key, ok := o.keys[id]
	if !ok {
		return nil, fmt.Errorf("no sealing key %s was lent to this run", id)
	}
	return key.Open(ciphertext, resourceKey, slot)
}
