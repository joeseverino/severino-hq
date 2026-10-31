package host

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// The verifier a deploy checks the composition's signature with: cosign, at
// a pinned release and its published digest. It lives beside the tree, not
// in it, because the tree is replaced from the image the verifier checks.
const (
	CosignVersion = "v3.1.3"
	CosignSHA256  = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
	CosignURL     = "https://github.com/sigstore/cosign/releases/download/" + CosignVersion + "/cosign-linux-amd64"
)

// InstallVerifier puts the pinned cosign at path, unless it is there already.
// The download is checked against the pinned digest before it is made
// executable, and replaces what was there only then.
func InstallVerifier(ctx context.Context, client *http.Client, from, path string) error {
	return installVerifier(ctx, client, from, path, CosignSHA256)
}

func installVerifier(ctx context.Context, client *http.Client, from, path, pinned string) error {
	if digest, err := fileDigest(path); err == nil && digest == pinned {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	staged, err := os.CreateTemp(filepath.Dir(path), ".cosign.")
	if err != nil {
		return err
	}
	defer os.Remove(staged.Name())
	err = download(ctx, client, from, staged)
	if closeErr := staged.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("could not download cosign %s: %w", CosignVersion, err)
	}
	if digest, err := fileDigest(staged.Name()); err != nil || digest != pinned {
		return fmt.Errorf("refusing cosign %s: its digest is not the pinned one", CosignVersion)
	}
	if err := os.Chmod(staged.Name(), 0o755); err != nil {
		return err
	}
	return os.Rename(staged.Name(), path)
}

// download writes the release to file, from its start on each attempt.
func download(ctx context.Context, client *http.Client, from string, file *os.File) error {
	var last error
	for attempt := range 3 {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 2 * time.Second):
			}
		}
		if err := file.Truncate(0); err != nil {
			return err
		}
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			return err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, from, http.NoBody)
		if err != nil {
			return err
		}
		response, err := client.Do(request)
		if err != nil {
			last = err
			continue
		}
		if response.StatusCode != http.StatusOK {
			response.Body.Close()
			last = fmt.Errorf("GitHub answered %d", response.StatusCode)
			continue
		}
		_, err = io.Copy(file, io.LimitReader(response.Body, 512<<20))
		response.Body.Close()
		if err == nil {
			return nil
		}
		last = err
	}
	return last
}

func fileDigest(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
