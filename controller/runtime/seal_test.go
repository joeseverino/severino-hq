package runtime

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/internal/sealing"
)

// sealVector is testdata/seal: a seed, its public key as hq-host writes it,
// and a value HQ's Python sealed to that key (README there says how).
const sealVector = "testdata/seal"

type vector struct {
	ResourceKey string `json:"resource_key"`
	Slot        string `json:"slot"`
	Info        string `json:"info"`
	Plaintext   string `json:"plaintext"`
	Sealed      string `json:"sealed"`
}

func readVector(t *testing.T) vector {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(sealVector, "vector.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vector
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func vectorOpener(t *testing.T) *Opener {
	t.Helper()
	opener, err := NewOpener(sealVector)
	if err != nil {
		t.Fatal(err)
	}
	return opener
}

func TestTheControllerOpensWhatHQSealed(t *testing.T) {
	v := readVector(t)
	plaintext, err := vectorOpener(t).Open(v.Sealed, v.ResourceKey, v.Slot)
	if err != nil {
		t.Fatalf("the vector HQ sealed does not open: %v", err)
	}
	if string(plaintext) != v.Plaintext {
		t.Errorf("the vector opens to %q", plaintext)
	}
	// The binding HQ sealed under is the one the controller opens under.
	if info, err := sealing.Info(v.ResourceKey, v.Slot); err != nil || string(info) != v.Info {
		t.Errorf("the controller binds the vector to %q, HQ to %q", info, v.Info)
	}
}

func TestTheVectorsPublicKeyIsItsSeeds(t *testing.T) {
	names, err := filepath.Glob(filepath.Join(sealVector, "*.seed"))
	if err != nil || len(names) != 1 {
		t.Fatalf("the vector holds seeds %v", names)
	}
	seed, err := os.ReadFile(names[0])
	if err != nil {
		t.Fatal(err)
	}
	key, err := sealing.NewKey(seed)
	if err != nil {
		t.Fatal(err)
	}
	published, err := os.ReadFile(filepath.Join(sealVector, "public.key"))
	if err != nil {
		t.Fatal(err)
	}
	if want := base64.StdEncoding.EncodeToString(key.PublicKey()) + "\n"; string(published) != want {
		t.Errorf("public.key is not its seed's; it is, as hq-host writes it:\n%s", want)
	}
	if filepath.Base(names[0]) != key.ID+".seed" {
		t.Errorf("the seed is named %s; its key is %s", filepath.Base(names[0]), key.ID)
	}
}

func TestASealedValueOpensOnlyWhereItWasSealedFor(t *testing.T) {
	v := readVector(t)
	opener := vectorOpener(t)
	id, ciphertext, err := sealing.Header(v.Sealed)
	if err != nil {
		t.Fatal(err)
	}
	tampered := bytes.Clone(ciphertext)
	tampered[len(tampered)-1] ^= 1
	reseal := func(id string, ciphertext []byte) string {
		return sealing.Prefix + "." + id + "." + base64.RawURLEncoding.EncodeToString(ciphertext)
	}
	for _, test := range []struct {
		name, sealed, resource, slot string
		unopened                     bool
	}{
		{"another slot", v.Sealed, v.ResourceKey, "env:EXAMPLE_TOKEN", true},
		{"another resource", v.Sealed, "example.other", v.Slot, true},
		{"a changed ciphertext", reseal(id, tampered), v.ResourceKey, v.Slot, true},
		{"a cut ciphertext", reseal(id, ciphertext[:len(ciphertext)/2]), v.ResourceKey, v.Slot, true},
		{"a key not lent", reseal("00000000", ciphertext), v.ResourceKey, v.Slot, false},
		{"no header", strings.TrimPrefix(v.Sealed, sealing.Prefix+"."), v.ResourceKey, v.Slot, false},
		{"a separator in the slot", v.Sealed, v.ResourceKey, v.Slot + "|x", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			plaintext, err := opener.Open(test.sealed, test.resource, test.slot)
			if err == nil {
				t.Fatalf("it opened, to %q", plaintext)
			}
			if got := errors.Is(err, sealing.ErrUnopened); got != test.unopened {
				t.Errorf("refused with %v", err)
			}
		})
	}
}

func TestARunLentNoKeysOpensNothing(t *testing.T) {
	v := readVector(t)
	opener, err := NewOpener("")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := opener.Open(v.Sealed, v.ResourceKey, v.Slot); err == nil {
		t.Error("a run lent no keys opened a sealed value")
	}
}

func TestASeedNamedForAnotherKeyIsRefused(t *testing.T) {
	dir := t.TempDir()
	seed, err := os.ReadFile(filepath.Join(sealVector, readVectorID(t)+".seed"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "00000000.seed"), seed, 0o400); err != nil {
		t.Fatal(err)
	}
	if _, err := NewOpener(dir); err == nil {
		t.Error("a seed under another key's name was read")
	}
}

func readVectorID(t *testing.T) string {
	t.Helper()
	id, _, err := sealing.Header(readVector(t).Sealed)
	if err != nil {
		t.Fatal(err)
	}
	return id
}
