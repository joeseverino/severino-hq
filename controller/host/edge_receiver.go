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
	"time"
)

// The files EdgeReceiver writes, as the edge unpacks them.
const (
	edgeReceiverName = "hq-edge"
	edgeReceiverSums = "SHA256SUMS"
)

// EdgeReceiver writes the tree's edge receiver to out for an administrator
// to install on an edge (docs/Deployment.md, Remote targets): a tar of
// hq-edge and the SHA256SUMS line the edge checks it by. It writes nothing
// unless the check's own verdicts hold: the tree is the release HQ runs,
// byte for byte, and the receiver is root's alone. Its findings go to the
// checker's Out.
func (c *Checker) EdgeReceiver(ctx context.Context, out io.Writer) error {
	c.failed, c.unverified = false, false
	c.verified(ctx)
	c.rootsAlone(edgeRelative, "an edge installs %s as its root receiver")
	if c.failed || c.unverified {
		return errors.New("hq-edge is not written: the tree is not proven to be the release HQ runs")
	}
	program, err := os.ReadFile(filepath.Join(c.Lib, edgeRelative))
	if err != nil {
		return err
	}
	sum := sha256.Sum256(program)
	sums := []byte(hex.EncodeToString(sum[:]) + "  " + edgeReceiverName + "\n")
	// Built whole before the first byte goes out, so a failure leaves out
	// nothing a reader could unpack.
	var buffer bytes.Buffer
	archive := tar.NewWriter(&buffer)
	for _, file := range []struct {
		name string
		mode int64
		data []byte
	}{{edgeReceiverName, 0o755, program}, {edgeReceiverSums, 0o644, sums}} {
		header := &tar.Header{Name: file.name, Mode: file.mode, Size: int64(len(file.data)),
			Typeflag: tar.TypeReg, ModTime: time.Unix(0, 0), Format: tar.FormatUSTAR}
		if err := archive.WriteHeader(header); err != nil {
			return err
		}
		if _, err := archive.Write(file.data); err != nil {
			return err
		}
	}
	if err := archive.Close(); err != nil {
		return err
	}
	_, err = out.Write(buffer.Bytes())
	return err
}
