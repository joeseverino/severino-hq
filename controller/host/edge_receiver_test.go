package host

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

// edgeReceiver is what EdgeReceiver wrote on h and its error, its findings
// left in h.out.
func (h *checkHost) edgeReceiver() ([]byte, error) {
	h.t.Helper()
	h.out.Reset()
	var out bytes.Buffer
	err := h.checker.EdgeReceiver(context.Background(), &out)
	return out.Bytes(), err
}

// A proven tree hands over its receiver whole, as an executable the edge
// unpacks, with the line sha256sum -c checks it by: the digest of the tree's
// own bytes.
func TestAProvenTreeHandsOverItsReceiverAndItsDigest(t *testing.T) {
	h := newCheckHost(t)
	program := []byte("\x7fELF the receiver the image built")
	if err := os.WriteFile(filepath.Join(h.checker.Lib, edgeRelative), program, 0o755); err != nil {
		t.Fatal(err)
	}

	written, err := h.edgeReceiver()
	if err != nil {
		t.Fatalf("refused: %v\n%s", err, h.out.String())
	}

	files := map[string][]byte{}
	modes := map[string]int64{}
	reader := tar.NewReader(bytes.NewReader(written))
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if header.Typeflag != tar.TypeReg {
			t.Errorf("%s is not a regular file", header.Name)
		}
		data, err := io.ReadAll(reader)
		if err != nil {
			t.Fatal(err)
		}
		files[header.Name], modes[header.Name] = data, header.Mode
	}
	if len(files) != 2 || !bytes.Equal(files[edgeReceiverName], program) {
		t.Fatalf("the tar holds %d files, the receiver's bytes %q", len(files), files[edgeReceiverName])
	}
	if modes[edgeReceiverName]&0o100 == 0 {
		t.Errorf("the receiver unpacks as mode %o, which cannot run", modes[edgeReceiverName])
	}
	sum := sha256.Sum256(program)
	if want := hex.EncodeToString(sum[:]) + "  " + edgeReceiverName + "\n"; string(files[edgeReceiverSums]) != want {
		t.Errorf("%s is %q, want %q", edgeReceiverSums, files[edgeReceiverSums], want)
	}
}

// A tree not proven to be the release HQ runs, or whose receiver another
// account could have rewritten, hands over nothing, and says why.
func TestAnUnprovenTreeHandsOverNothing(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(*checkHost)
		says    string
	}{
		"a tree that differs from the image": {func(h *checkHost) {
			h.verify, h.verifyErr = "drift: deploy/bin/hq-edge differs from the image's\n", true
		}, "FAIL    deploy/bin/hq-edge differs from the image's"},
		"a tree no verify has read since its install": {func(h *checkHost) {
			h.password = true
		}, "UNKNOWN hq-host verify needs a password here"},
		"a receiver others can write": {func(h *checkHost) {
			if err := os.Chmod(filepath.Join(h.checker.Lib, edgeRelative), 0o757); err != nil {
				h.t.Fatal(err)
			}
		}, "hq-edge is writable by its group or others"},
		"no receiver in the tree": {func(h *checkHost) {
			os.Remove(filepath.Join(h.checker.Lib, edgeRelative))
		}, "hq-edge cannot be read"},
	} {
		t.Run(name, func(t *testing.T) {
			h := newCheckHost(t)
			c.arrange(h)

			written, err := h.edgeReceiver()

			if err == nil || len(written) != 0 {
				t.Fatalf("wrote %d bytes, error %v", len(written), err)
			}
			h.says(h.out.String(), c.says)
		})
	}
}
